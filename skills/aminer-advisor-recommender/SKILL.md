---
name: aminer-advisor-recommender
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

For applicant matching, read [references/applicant-schema.md](references/applicant-schema.md). For ranking or collaboration analysis, read [references/scoring-rubric.md](references/scoring-rubric.md). For school-tier filters, read [references/school-tiers.md](references/school-tiers.md). Before calling AMiner, read [references/api-workflows.md](references/api-workflows.md).

## Preflight

1. Check whether `AMINER_API_KEY` exists without printing its value. Stop and direct the user to the AMiner console if it is missing.
2. Send the token unchanged in the `Authorization` header and send `X-Platform: openclaw`.
3. Use only endpoints and parameters documented in `references/api-workflows.md` or the repository's canonical API catalog. Never invent an endpoint or field.
4. Estimate cost before calling paid endpoints. Ask for confirmation before a planned chain costing at least ¥5.00.

## Collect candidates

1. Normalize the requested research direction into the user's wording plus a small set of English/Chinese aliases.
2. Resolve institutions to canonical AMiner organization IDs before resolving people. Treat department matching as unverified when AMiner lacks department-level data.
3. Search broadly with free or low-cost endpoints, then fetch deeper evidence only for a short list. Default to 10 candidates unless the user specifies otherwise.
4. Link paper authors only when their paper affiliation matches the resolved organization ID. Then require exact name and exact organization in `person_search`. If several profiles still match, return one unresolved identity with alternate IDs and no fabricated profile URL.
5. Use recent publications to verify direction fit. Prefer a five-year window unless the user requests another period.
6. Build collaboration evidence from coauthors and their affiliations. Do not infer an industry relationship from an email domain or model knowledge alone.
7. Require every candidate to have at least one non-empty, dated, direction-matched paper. Preserve source identifiers, AMiner URLs, years, and the API used for every factual claim.

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

## Control retrieval

- `--paper-limit`: papers returned per direction/school query; default 10.
- `--max-author-lookups`: distinct paper-author names resolved per school; default 30.
- `--candidate-limit`: final rows; default 10.
- `--max-schools`: school cap for tier queries; default 5.
- `--schools`: comma-separated explicit override for a tier.
- `--verify-roles N`: call paid `person_detail` for up to N shortlisted candidates per school; default 0.
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
