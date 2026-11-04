---
name: aminer-advisor-recommender
metadata:
  version: 1.0.0
  author: AMiner
  contact: report@aminer.cn
description: >
  Discover Chinese institutions by research direction and recommend prospective advisors using AMiner evidence and an applicant profile. Use when a user asks which institutions are active in a direction; asks for advisors in a named university, school, or direction; filters by tiers such as 华五/985/211/双一流; compares publication-based academic or industry collaboration breadth; or wants heuristic reach/match/safer school and advisor suggestions based on undergraduate institution, grades, research, projects, publications, internships, degree target, and location preferences. Also triggers on Chinese requests such as 考研/保研/申博择校、推荐导师、找导师、选导师、某方向哪些学校强.
---

# AMiner Advisor Recommender

Use AMiner Open Platform REST APIs as the academic-data layer. Plan searches, normalize evidence, score candidates locally, and present explainable recommendations. Never treat a model's prior knowledge as current evidence.

## Route the request

Choose one workflow:

1. **Direction discovery**: direction -> institutions with matched-paper evidence.
2. **Named institution**: school + school/department + direction -> prospective advisor candidates.
3. **Tier and direction**: direction + 华五/985/211/双一流 or a user-provided school set -> institution and prospective advisor candidates.
4. **Collaboration breadth**: direction/institution -> candidates -> coauthors and affiliations -> academic/industry collaboration comparison.
5. **Applicant matching**: applicant profile -> direction-based school discovery -> cross-tier heuristic portfolio -> prospective advisor candidates.
6. **Person deep-dive**: one or more scholar names -> disambiguated profile(s) with position, interests, recent papers, citations (and comparison table when several names are given). Use this whenever the user names specific advisor(s) and asks what they work on / how they compare / to help draft an outreach (套磁) letter — this is the most common real request and is the inverse of the direction-based modes above. Do NOT answer a named-scholar question by running direction search.

For applicant matching, read [references/applicant-schema.md](references/applicant-schema.md). For ranking or collaboration analysis, read [references/scoring-rubric.md](references/scoring-rubric.md). For school-tier filters, read [references/school-tiers.md](references/school-tiers.md). Before calling AMiner, read [references/api-workflows.md](references/api-workflows.md).

## Preflight

1. Check whether `AMINER_API_KEY` exists without printing its value. Stop and direct the user to the AMiner console if it is missing.
2. Send the token unchanged in the `Authorization` header and send `X-Platform: openclaw`.
3. Use only endpoints and parameters documented in `references/api-workflows.md` and `scripts/aminer_client.py`. Never invent an endpoint or field.
4. Estimate cost before calling paid endpoints. Ask for confirmation before a planned chain costing at least ¥5.00.

## Collect candidates

1. **Expand the research direction before searching (critical for recall).** AMiner is an English-first corpus, so a bare Chinese or single-phrase direction under-recalls badly (e.g. 具身智能 alone returned 1 wrong candidate; `robot learning` + embodied/manipulation/navigation/legged recovered the core PIs 王越/熊蓉). Pass `--aliases` as a deliberate query expansion covering: (a) the English name(s) of the direction, (b) the Chinese name, (c) 2–5 subfield/near-synonym terms actually used in paper titles and scholar interests. Examples: 具身智能 → `embodied AI,embodied intelligence,robot learning,manipulation,navigation,legged`; 大模型 → `large language model,LLM,language modeling,pretraining,instruction tuning`; 多模态 → `multimodal,vision-language,MLLM,image-text`. Use your own domain knowledge to build this set — do not rely on the user to list every term. Prefer the English name as the primary `--direction` for niche/emerging topics.
2. Resolve the **first-level university ID** before paper search. For a combined school/department string, use `org_disambiguate_pro` to separate `一级ID` from `二级ID`; if that fails, extract the explicit university name and resolve it separately. Never pass a department ID as the school ID. In `paper_qa_search`, send `org_id` with `query`, without `org_terms`: combining `org_id` and `org_terms` widens results by OR, not AND. Treat department matching as unverified until an official department page confirms it.
3. Search broadly with free or low-cost endpoints, then fetch deeper evidence only for a short list. Default to 10 candidates unless the user specifies otherwise.
4. Link paper authors only when their paper affiliation matches the resolved university ID or names the university despite a department-level author ID. Then require exact name and university in `person_search`. If several profiles still match, return one unresolved identity with alternate IDs and no fabricated profile URL.
5. Use recent publications to verify direction fit. Prefer a five-year window unless the user requests another period.
6. Build collaboration evidence from coauthors and their affiliations. Do not infer an industry relationship from an email domain or model knowledge alone.
7. Require every candidate to have at least one non-empty, dated, direction-matched paper. Preserve source identifiers, AMiner URLs, years, and the API used for every factual claim.
8. In multi-school advisor tasks, run the bounded search once. If the resulting shortlist has `role_unverified: true`, verify only the finalists' person IDs with `person_detail` (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/aminer_client.py" --api person_detail --params '{"id":"PERSON_ID"}'`) and check the official faculty/admissions page for department and supervisor eligibility. The shared `call_aminer_api` wrapper, when used, takes `person_id` instead of the raw HTTP `id` parameter. Do not rerun all schools with larger paper, author, and candidate limits just to verify a few roles; a broad retry can exceed the script time limit. A faculty title alone does not prove current graduate-supervisor eligibility.

## Run workflows

Run commands from the skill directory. Save JSON when the result must be reused; otherwise read stdout and render the required user-facing table.

### 1. Discover institutions by direction

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode discover \
  --direction "具身智能" \
  --aliases "embodied intelligence,embodied AI" \
  --paper-limit 10
```

Treat the output as AMiner coverage within the inspected paper sample, not a national university ranking. Feed selected institutions into `named`, `collaboration`, or `profile` mode.

For a question comparing which schools are active, use the first pass only to form a candidate set. Include plausible schools from the user's scope and field knowledge even when the global sample missed them. Then rerun `discover` with `--schools "清华大学,上海交通大学,浙江大学,北京大学,北京航空航天大学"` (replace with the actual candidates), the same direction aliases and paper limit. The second pass resolves each school's first-level ID, applies the same query terms and five-year window, and checks author affiliations for at most eight papers per school sampled across aliases. Report the schools examined, inspected counts, evidence papers and omissions. These counts describe the inspected sample; do not call them a national ranking or infer that an unexamined school is inactive.

### 2. Named school, department, and direction

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode named \
  --school "浙江大学" \
  --department "计算机科学与技术学院" \
  --direction "具身智能" \
  --aliases "embodied intelligence,embodied AI" \
  --candidate-limit 10
```

Treat the department as a requested constraint. If AMiner does not return department-level affiliation, label it unverified and direct the user to the official school page.

### 3. Direction within a school tier

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode tier \
  --tier "华五" \
  --direction "多模态大模型" \
  --aliases "multimodal large language model,MLLM" \
  --max-schools 5
```

For a large group such as 211, ask for region, explicit schools, or a smaller maximum before broad retrieval. Never silently imply that the first truncated schools represent the whole tier.

### 4. Collaboration breadth

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode collaboration \
  --school "清华大学" \
  --direction "computer vision" \
  --collaboration-type industry \
  --paper-limit 10
```

Collaboration evidence comes from paper coauthors' returned organization IDs. `paper_detail` and `org_detail` are paid but low-cost. Report the declared paper window and the number of inspected papers. A coauthored paper is evidence of publication collaboration, not proof of a formal partnership. This mode sees co-authorship breadth only: AMiner paper data cannot verify employment history (for example a past industry position), so when the user asks for career-history depth, state this limitation and direct them to the person's official homepage instead of inferring. When one candidate's collaboration count is dominated by a single many-author paper (such as a survey), report that concentration instead of presenting it as broad collaboration.

### 5. Applicant-profile matching

Collect the fields in `references/applicant-schema.md`, create a temporary JSON file, and run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode profile \
  --profile "/path/to/applicant.json" \
  --direction "robot learning" \
  --candidate-limit 10
```

Do not store the applicant file inside the skill unless the user explicitly asks. If target schools or a tier are missing, ask the user to provide them; AMiner academic data alone cannot infer a complete admissions portfolio.

By default, profile mode uses AMiner direction evidence to add lower-tier institutions when capacity remains under `--max-schools`. It then reserves output space for every available reach/match/safer band, instead of allowing high-scoring reach candidates to fill the whole result. Use `--no-auto-expand-profile` only when the user explicitly wants a closed school list. Automatic additions are candidate schools from the inspected AMiner sample, not admissions guarantees or a complete national ranking; if a band has no evidence-backed candidate, report it as missing rather than fabricating one.

### 6. Person deep-dive and comparison

When the user names specific scholar(s), deep-dive the person(s) directly. If they also name a research focus, include that focus and its title-language aliases so the paper search does not stop at a generic by-name list:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode person \
  --names "刘付成" \
  --school "上海交通大学" \
  --direction "attitude control" \
  --aliases "spacecraft attitude control,卫星姿态控制" \
  --deep
```

- `--names` takes one name, or several comma-separated for a **comparison** (`--names "孙栩,张铭,李戈"`), each returned as its own profile block.
- `--school` is optional and only aids disambiguation.
- `--direction` and `--aliases` are optional in person mode. With a focus, the script extracts the first-level university from the scholar's possibly long affiliation, queries up to six focus terms, and samples papers round-robin. The primary term gets both recent-year and citation-order searches inside the same five-year window, so newer papers and influential foundations both have a chance to enter the bounded sample. It checks each result's same-name author against the university affiliation in `paper_detail`. When fewer than five recent focus papers survive (or fewer than `--paper-limit` if smaller), it also samples the selected person's `person_paper_relation`, verifies years through `paper_info`, and merges distinct papers. Matching titles anywhere in a long relation receive priority over a simple newest-ID sample; returned evidence spans multiple recent years where available, and duplicate titles count once. This can recover work signed at a previous institution. ID-linked papers outside the stated focus are retained only as broader recent work with `topic_match: false`, never as evidence of the focus. An exact scholar-name match is required before choosing a profile: a fuzzy result such as 李戈辉 cannot be silently used for 李戈. The relation is person-ID evidence, but publication status and original affiliation still need source checking.
- For a **broad field portrait or comparison** (for example, two scholars' overall 大模型 research), add `--broad-context`. A citation-ranked, no-query five-year pass returns up to six additional, author-and-affiliation-verified works per scholar in `broad_context_papers`; it can reveal model families and representative work omitted by ten focus-query slots. Treat `topic_match: false` there as adjacent context requiring interpretation, not direct proof of the stated focus. Compare both scholars with this same setting, and check official homepages for themes. Never infer that a scholar lacks a model family or research line merely because a bounded sample omitted it. **Do not use `--broad-context` for a narrowly specified subfield such as 大模型代码生成**; the focused search and ID-linked fallback are the evidence path for that question, and a broad pass adds off-topic papers and can bury the cost ledger in oversized output.
- The same `--broad-context` pass also returns up to ten author-and-affiliation-verified `recent_context_papers` from an independent newest-year search. Use these to check emerging work that has not yet accrued citations. They are broader context, not automatic proof of the focus: inspect titles, `topic_match`, and primary sources before choosing examples. Do not mistake high citations for recency or a bounded newest-year sample for full coverage.
- `recent_papers` is a bounded sample (`recent_papers_sampling.exhaustive: false`), not a scholar's complete publication count. Never rank people by selected counts such as 10:2:1 or claim that a topic has only one paper solely from this sample. Compare the actual themes and verified examples, and state where coverage is incomplete.
- Keep numerical claims as returned by the source. A model name alone does not verify its parameter count, venue, award, or performance; do not convert a suffix such as `130B` into a Chinese magnitude label without a source-backed calculation (130B is 1300 亿, not 百亿级).
- Include subfield aliases that would change the answer, and distinguish direct work from adjacent work. For 大模型代码生成, search code generation, program synthesis, code reasoning, code completion, and coding agents; do not treat a generic LLM or watermarking paper as coding evidence. For a person comparison, apply the same focus and recent-year window to every named scholar, then make missing coverage explicit.
- In a named-person comparison, give a full `https://www.aminer.cn/pub/...` link for every paper used as a key reason, including at least one focus paper per scholar when one exists. A title without its link is hard to verify. Distinguish a paper's AMiner year from confirmed publication status; do not infer that a 2026 entry is a preprint or unpublished without a source.
- `--deep` adds `person_detail` (¥1/person) for position/education/honors. The ID-linked paper supplement can add ¥1.50/person; `--max-cost` applies to person mode and the cost estimate covers it.
- In general by-name searches, papers with low overlap against the scholar's interests are flagged `topic_match: false` (possible same-name mixin). For a focused search, papers whose author affiliation does not match the selected university are excluded; `topic_match` requires the distinctive terms of the requested focus (for example `code reasoning`, not just `LLM`). Broad aliases improve recall but do not establish direction fit. Lead with verified focus papers and treat any flagged general-search papers as needs-verification.
- AMiner has no admissions/employment-status data: always tell the user to verify current recruiting and affiliation on the official page.

**Drafting an outreach (套磁) letter**: this is a natural follow-up, not a separate API. Use only the real evidence from the person deep-dive (the scholar's genuine interests and `topic_match: true` recent papers) plus the applicant's own background. Ground every advisor-specific claim in the profile (cite a real interest or paper), never invent results, exclude flagged mixin papers, use placeholders for applicant facts the user must fill, and close by advising the user to confirm the advisor's current recruiting status.

## Control retrieval

- `--paper-limit`: papers returned per direction/school query; default 10.
- School-specific paper search first uses university-ID-filtered `paper_qa_search`, then one name-filtered `paper_search_pro` fallback when empty. Details are capped per school and sampled across aliases. Only if no candidate survives does a bounded global direction pass inspect further papers, with author affiliations checked. Stop after these passes and report remaining coverage gaps instead of repeatedly widening retrieval.
- `--max-author-lookups`: distinct paper-author names resolved per school; default 12. Independent author and paper-detail lookups use at most four concurrent requests.
- `--candidate-limit`: final rows; default 10.
- `--max-schools`: school cap for tier queries; default 5.
- `--schools`: comma-separated explicit schools for tier recommendations or a same-query discover comparison; capped by `--max-schools`.
- `--verify-roles N`: call paid `person_detail` for up to N shortlisted candidates **per school**; default 0. On a multi-school result, prefer checking only final person IDs directly after the bounded first pass so verification cost and latency scale with the actual shortlist.
- `--hired-since YEAR`: screen for likely-recent hires (e.g. 新引进导师 since 2020). Implies `person_detail` verification for the shortlist. Only candidates with positive evidence (bio join year in the window, doctorate finished near it, or a junior title) enter the main list; everyone else is reported under `hire_screening.no_hire_evidence` because AMiner has no hire-date field and established scholars hired recently are undetectable. Never present a `no_hire_evidence` scholar as a confirmed new hire — direct the user to official department announcements.
- `--max-cost`: reject a worst-case estimate at or above this amount; default ¥5.00.
- `--rank-by`: ranking dimension — `overall` (default), `citation` (established high-citation PIs), `recent` (most recent direction activity), `rising` (heuristic rising stars: recent direction evidence with a smaller citation base).
- `--roster-pages N` (opt-in, default 0): after paper-reverse recall, page the free `person_search` org listing (up to N pages) and add interest-matched in-faculty scholars missed by the paper sample, tagged `identity_resolution: roster`. This is a **best-effort bonus, not a general head-recall fix**: the free listing returns only about the top ~30 highest-cited scholars per institution across all disciplines, so it recovers a direction's PIs only when they are among the school's most-cited overall (worked for Tsinghua NLP: 刘知远/唐杰/孙茂松). For most directions the target PIs are not in that top set. Roster candidates have no sampled direction paper yet — treat their direction as Needs verification.
- `--yes`: proceed above `--max-cost` only after the user explicitly confirms the estimate.
- `--allow-name-fallback` and `--allow-cross-discipline`: relax identity or discipline filters only when the user accepts the added noise.
- `--no-auto-expand-profile`: disable the default cross-tier school expansion for a closed-list comparison.

The script estimates worst-case cost before any API call and returns the successful-call ledger afterward. Failed calls are reported as errors and are not counted as successful paid calls.

## Rank and classify

Apply the rubric in `references/scoring-rubric.md`. Keep raw evidence separate from derived scores.

- Use school tiers only as filters or user preferences, not as a universal quality ranking.
- Default ranking favors established high-citation scholars, whose supervision capacity is often limited. When the user asks for young advisors, rising stars, or alternatives to famous PIs — or when every top row is a mega-cited PI — rerun or re-rank with `--rank-by rising` (or `recent`) and present both views. Label the rising view as a heuristic, not an official rising-star index.
- Label reach/match/safer classifications as heuristic fit bands, never admission probabilities.
- Do not penalize missing data as if it were negative evidence. Mark the component `unknown` and lower confidence.
- Explain every score with observable evidence.

## Verify on the web

When the host provides web search or fetch tools, extend AMiner evidence with web verification. Never substitute model memory for either task below.

1. **Hire dates**: for hire-screening results the user cares about — especially every relevant `no_hire_evidence` entry — search official sources: department faculty pages, 人才引进/新进教师 announcements, personal homepages (query patterns like `<学校> <学院> <姓名> 入职|加入|人才引进`). Prefer official domains (edu.cn, department sites). Attach the source URL and stated year to the candidate. Senior scholars confirmed this way are exactly the recent hires AMiner data cannot detect. If nothing authoritative is found, keep the candidate under needs-verification instead of guessing; a faculty-page listing alone confirms current affiliation, not the hire date.
2. **Reputation (风评)**: only when the user explicitly asks. Search advisor-review discussions (导师评价、知乎、论坛等). Treat everything found as anonymous, unverifiable opinion: attribute it as such (「网络匿名评价称…」), cover both positive and negative signals with links, never restate allegations as established fact, and never fold reputation into the computed scores — present it as a separate needs-verification section alongside the evidence-based results.

## Present results

Return:

1. A brief restatement of constraints and missing information.
2. A comparison table with institution, department, advisor, direction fit, recent activity, academic collaboration, industry collaboration, recommendation band, confidence, and evidence links.
3. Two to four evidence-backed reasons per candidate.
4. Risks and items requiring verification, especially current affiliation, current recruitment status, degree eligibility, and admissions deadlines.
5. A cost summary listing APIs called, calls per API, unit price, total price, and unavailable evidence.

If a returned person's `role_unverified` is true, call them a **researcher lead (学者线索)**, not a confirmed advisor; do not count them as a verified supervisor or advise immediate outreach. A paper affiliation establishes where that paper was signed, not current employment or graduate-supervisor eligibility. If no verified advisor remains, say so plainly and point to the official faculty/admissions roster for the missing check.
Do not add "skipped paper-author entries" warnings across passes and call the sum distinct scholars; the same author or paper may be inspected twice.

Use these labels consistently:

- **Verified fact**: directly supported by returned AMiner data or an official source.
- **Derived assessment**: computed from cited facts using the rubric.
- **Needs verification**: unavailable, stale, ambiguous, or only found in a non-authoritative source.

## Guardrails

- Never claim guaranteed admission or provide a numerical admission probability without an appropriate validated admissions dataset.
- Never claim an advisor is recruiting from publication activity alone. Direct users to the official department/advisor page.
- Never call unresolved name collisions verified people or emit an AMiner profile URL for them.
- Do not claim international collaboration: the current implementation classifies academic/industry affiliation only and has no verified country dimension.
- Never present co-authorship breadth as employment history: AMiner paper data cannot verify that a scholar held a position at a collaborating organization.
- Never claim a scholar is a recently hired advisor without positive evidence; use `--hired-since`, report its `no_hire_evidence` bucket honestly, and web-verify those entries against official sources when web tools are available.
- Reputation content from the web is anonymous opinion: present it attributed and unverified, never as fact, and never let it alter evidence-based scores.
- Do not expose tokens or include them in logs, generated files, commands, or error messages.
- Minimize personal data. Do not persist an applicant profile unless explicitly requested.
- Report empty or partial results honestly; never fabricate advisors, papers, affiliations, collaborations, or rankings.
- Ask before a paid or high-volume call sequence when API pricing or limits indicate material cost.
