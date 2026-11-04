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

**Main-list contract for applicant recommendations:** For an applicant's shortlist within named schools, the final recommended mentors must be exactly the `approved` rows returned by `scripts/shortlist_gate.py` (workflow below). If it approves zero, state that no verified mentor is available yet and give only provisional leads; never put a rejected person in a numbered recommendation or main table. `最多5位` is a ceiling, not a target. Copy canonical names from official pages or exact AMiner IDs, and do not assign individual “冲刺/匹配/稳妥” bands without admissions evidence. Do not infer a required graduate college from the applicant's undergraduate major; if the user specifies only universities and a research direction, consider relevant advisors across colleges and state each candidate's actual degree program.

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
   If the user has already authorized this batch and its cost, pass `--yes` to `recommend.py` for estimates at or above the default threshold; do not silently skip the needed verification. Keep the estimate and actual ledger in the answer.

## Collect candidates

1. **Expand the research direction before searching (critical for recall).** AMiner is an English-first corpus, so a bare Chinese or single-phrase direction under-recalls badly (e.g. 具身智能 alone returned 1 wrong candidate; `robot learning` + embodied/manipulation/navigation/legged recovered the core PIs 王越/熊蓉). Pass `--aliases` as a deliberate query expansion covering: (a) the English name(s) of the direction, (b) the Chinese name, (c) 2–5 subfield/near-synonym terms actually used in paper titles and scholar interests. Examples: 具身智能 → `embodied AI,embodied intelligence,robot learning,manipulation,navigation,legged`; 大模型 → `large language model,LLM,language modeling,pretraining,instruction tuning`; 多模态 → `multimodal,vision-language,MLLM,image-text`. Use your own domain knowledge to build this set — do not rely on the user to list every term. Prefer the English name as the primary `--direction` for niche/emerging topics.
   Keep aliases inside the user's requested branch, not merely in the same broad discipline. For 光纤通信/光通信系统, coherent optical communication, space-division multiplexing, OAM transmission and WDM are useful search terms; pure fiber sensing is adjacent and must not become a positive communication match just because it contains “fiber.”
2. Resolve the **first-level university ID** before paper search. For a combined school/department string, use `org_disambiguate_pro` to separate `一级ID` from `二级ID`; if that fails, extract the explicit university name and resolve it separately. Never pass a department ID as the school ID. In `paper_qa_search`, send `org_id` with `query`, without `org_terms`: combining `org_id` and `org_terms` widens results by OR, not AND. Treat department matching as unverified until an official department page confirms it.
   When the user names a **specific department**, inspect that department's official faculty directory early. Follow its navigation to the actual 教师/师资 listing, rather than stopping at a landing page or an empty category; if HTTPS fails, retry HTTP on the same official hostname. Use the directory to establish current faculty names, roles, and research themes, then use AMiner to verify each promising person's recent direction papers. The bounded paper-to-author search can add leads, but it cannot establish that the department has only those authors. If the directory is unavailable, keep department membership and supervisor status unverified rather than filling the list with school-wide paper coauthors.
   Stop official-directory URL guessing after HTTPS/HTTP for the same known directory and one alternate official faculty path fail. Mark that school's current-role evidence unavailable, keep its scholars as leads, and spend the remaining searches on reachable schools and the final shortlist gate. Repeated guesses at renamed hostnames do not strengthen a recommendation.
3. Search broadly with free or low-cost endpoints, then fetch deeper evidence only for a short list. Default to 10 candidates unless the user specifies otherwise.
4. Link paper authors only when their paper affiliation matches the resolved university ID or names the university despite a department-level author ID. Then require exact name and university in `person_search`. If several profiles still match, return one unresolved identity with alternate IDs and no fabricated profile URL.
5. Use recent publications to verify direction fit. Prefer a five-year window unless the user requests another period.
6. Build collaboration evidence from coauthors and their affiliations. Do not infer an industry relationship from an email domain or model knowledge alone.
7. Require every candidate to have at least one non-empty, dated, direction-matched paper. Preserve source identifiers, AMiner URLs, years, and the API used for every factual claim.
   A team title is not an individual's research direction. When paper-to-author search surfaces students, postdocs, or unresolved profiles, pivot to the relevant official faculty roster and inspect individual pages; the directory helper below can shortlist faculty by their own stated topics. Re-check those names with `person` mode. If only two people meet the evidence standard, recommend two even when the user says “最多5位.” Put official faculty with no individually verified direction paper under leads rather than padding the recommendation list.
   In both recommendations and excluded-person notes, copy the person's name from the exact AMiner ID or official page. Keep a Latin-script name in Latin script unless that same person ID or official page supplies the Chinese spelling; do not reconstruct Han characters from pinyin or memory.
   For a requested master's program, prioritize official profiles that explicitly say **硕士生导师** (or explicitly invite master's applicants) and list a dated direction paper. An individual official faculty page can establish both the person's identity and their own paper even if AMiner has no usable profile; do not discard such a person solely because AMiner name search fails. Conversely, a doctoral-supervisor title alone is not proof of master's-program eligibility.
8. In multi-school advisor tasks, run the bounded search once. If the resulting shortlist has `role_unverified: true`, verify only the finalists' person IDs with `person_detail` (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/aminer_client.py" --api person_detail --params '{"id":"PERSON_ID"}'`) and check the official faculty/admissions page for department and supervisor eligibility. The shared `call_aminer_api` wrapper, when used, takes `person_id` instead of the raw HTTP `id` parameter. Do not rerun all schools with larger paper, author, and candidate limits just to verify a few roles; a broad retry can exceed the script time limit. A faculty title alone does not prove current graduate-supervisor eligibility.
   In particular, keep a lecturer or assistant professor with no confirmed graduate-supervisor eligibility under **leads**, not the main mentor recommendation list. Do not promote them because their paper topic fits or because they seem easier to approach.

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

For a question comparing which schools are active, use the first pass only to form a candidate set. Include plausible schools from the user's scope and field knowledge even when the global sample missed them. For an open nationwide top-five question, compare a broad enough candidate set (about 12–20 institutions, batched if needed) to cover the field's distinct communities; in medical imaging, this means clinical universities and hospitals as well as engineering/computer-science groups. Then rerun `discover` with `--schools "清华大学,上海交通大学,浙江大学,北京大学,北京航空航天大学"` (replace with the actual candidates), the same direction aliases and paper limit. The second pass resolves each school's first-level ID, applies up to six identical query terms and a five-year window, and checks author affiliation text for at most 16 papers per school sampled from **both newest and citation-ranked results**. An `orgid` that contradicts the author's stated institution is not positive affiliation evidence; report the `affiliation_conflicts` count. Title/keyword direction matches count as evidence; an incidental abstract mention is reported separately. For a compound topic such as 多模态大模型, require evidence of the **whole topic**: a generic “multimodal” hardware paper or a pure language-model paper with a generic multimodal keyword is adjacent, not a core result. Compare the actual `representative_papers` and the additional recent `evidence_papers` across schools; these lists are compact excerpts while `matched_paper_count` includes all verified distinct titles within the inspected sample. Do not choose the first five by that count or claim the bounded sample is a national ranking. Explain relevant subfields and omissions. Report the schools examined, inspected counts, evidence papers and limitations.

Before excluding a plausible school with many `search_hit_count` hits but few affiliation-verified papers, check whether the 16-paper round-robin sample stopped before its influential direction papers. Use `paper_qa_search` directly with that school's resolved `org_id`, a direction-specific `query`, `use_topic: true`, and `force_citation_sort: true`; inspect several *later* relevant hits with `paper_detail` and add only papers whose author affiliation names the school. Repeating `discover` with a larger `--paper-limit` does **not** deepen this check because its detail cap remains 16 per school. Apply the same follow-up rule to similarly affected contenders; never rank by raw hits or count unchecked papers.

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
For a department-specific recommendation, first check the official teacher list and individual faculty pages. A relevant paper's first author or last author is not automatically a current faculty member or graduate supervisor; confirm that role on the official page. Use `--mode person --names ... --school ... --direction ...` to check direction papers for promising roster names that the bounded reverse-paper search missed. Do not conclude that no suitable advisor exists until both the roster and paper-based paths have been attempted where available. Preserve the official Chinese spelling of names: `Chao Xu` could represent several different surnames and must not be converted to a Chinese name from pinyin or a same-school homonym alone.

When the official teacher list contains many names but little research detail, use `scripts/faculty_directory.py --url "OFFICIAL_FACULTY_LIST_URL" --terms "机器人,具身,操作,模仿学习" --max-results 15` with terms adapted to the user's actual focus. It follows faculty navigation, professor/supervisor categories, and bounded pagination, reads at most 150 individual profiles under the same university's `*.edu.cn` domain by default, and returns page URLs with exact topic snippets. The `roster` field lists **all directory links**, while `matches` contains only the top keyword hits; absence from `matches` says nothing about roster membership. The list is a **lead filter**: check the most specific matches first (for robot manipulation, “操作/模仿学习” before generic “机器人”), then verify recent papers with AMiner. Report `unscanned_links` and `unvisited_directory_pages` when the directory exceeds a cap. Do not treat a keyword hit as proof of current admissions or as a scored publication.

For an applicant seeking a specific degree, add `--degree master` or `--degree doctor` to that directory command. Use `official_role_paper_leads` **first**: these names already have an explicit degree role and a dated paper excerpt on their individual official pages. Check paper topic and exact title/year, then pass them to `shortlist_gate.py` before investigating other names. The field is a lead filter, not certification; do not demote a verified official-page mentor merely because AMiner name search found no profile or paper.

### 3. Direction within a school tier

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode tier \
  --tier "华五" \
  --direction "多模态大模型" \
  --aliases "multimodal large language model,MLLM" \
  --max-schools 5
```

For a large group such as 211, ask for region, explicit schools, or a smaller maximum before broad retrieval. A 211 request includes schools that are also 985 unless the user explicitly asks for non-985 211 schools. Build the bounded school set from directional relevance and any region preference, not the tier file's first entries or a convenient subset. When the user asks you to **select** five schools rather than providing them, first form a plausible regional shortlist of up to ten tier-eligible schools, run `--mode discover --schools ... --max-schools ...` under the same expanded direction queries, and choose the five with the clearest direction evidence before running tier mentor search. A global newest-paper seed may miss major schools; do not take its five returned institutions as the whole shortlist. If a selected school yields no verified mentor, keep the search gap visible rather than silently substituting an unexamined school. Never imply that the bounded sample represents the whole tier. Only verified faculty belong in the final mentor recommendations; put unverified paper authors in a separate leads section, even when fewer than the requested maximum survive.

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
The bounded newest-paper sample can be dominated by students and miss senior faculty's earlier industry work. Collaboration mode mixes a recent-year slice with citation-ranked direction papers; if it still finds few eligible mentors, seed a small second shortlist from the official faculty roster or identified established scholars, verify current roles, and inspect author-filtered papers. For an established verified candidate whose company side remains missing, use a few **author + company-organization** `paper_search_pro` queries as a lead-finding pass (e.g. exact English author name with plausible enterprise names from a credible profile or field knowledge), including older papers within the stated window; then inspect `paper_detail`. Do not add a generic `query`/`keyword` filter to this first lead pass: it can hide otherwise valid cross-topic joint papers. This search may match a company anywhere in the paper and cannot by itself prove the candidate collaborated with it. For each named enterprise, cite either (a) a specific `paper_detail` record containing both the target same-name author with the requested university affiliation and a coauthor with that enterprise's affiliation, or (b) an official university-hosted page explicitly linking the candidate to a named company joint laboratory or research project. Label these as **paper coauthorship** versus **official joint-lab/project evidence**; a joint laboratory does not prove the company coauthored a paper. Awards, alumni destinations, a company's code repository, and an incidental employment mention do not establish collaboration. Compare distinct verified enterprises and independent papers for publication breadth, not raw search-hit counts. A person with neither verified company coauthorship nor explicit official joint-lab/project evidence is outside an “industry collaboration” recommendation even when their research direction fits; return fewer than the requested maximum rather than padding the list.
If the request requires **both an enterprise and another university**, check both conditions for each person before putting them in the main list. The enterprise side needs verified company coauthorship or explicit official joint-lab/project evidence as above. The university side needs a verified coauthor whose affiliation actually names an **external university** (`is_university: true` in the script output), or an official university page explicitly documenting a project with another university. A hospital or school within the target university, a research institute, or an AI laboratory alone is not another university. One paper may establish both kinds of coauthorship, but do not call that breadth across independent projects. For broad AI requests, after the first paper-derived shortlist, inspect a bounded set of official faculty/lab pages for named company collaborations that paper affiliations cannot expose; then verify an external-university relationship separately. Put people missing either side under leads and return fewer than the requested maximum.

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

Profile mode mixes newest-year and citation-ranked direction papers to avoid a recent student-author slice hiding established faculty, and excludes AMiner papers dated after the current year from candidate evidence. Check whether each candidate can supervise the requested degree in the requested program; a mathematically or medically adjacent paper alone is not program fit. Before writing application steps, read the output's `as_of_date`, `application_timing`, and calendar `method_notes`. The timing field is conditional on the national master's exam route and uses typical windows, so verify exact dates with official notices; never describe a window marked passed as a future action or claim long preparation time when `entrance_exam_near` is true. Do not infer admission chances from school tier or applicant résumé alone. A “young” or junior-ranked faculty member is **not** evidence of more time, closer guidance, easier admission, or available slots; leave guidance style and recruitment unverified unless an official source or the mentor directly confirms them.

For a master's applicant restricted to named schools, give main mentor slots only to people whose **current faculty and degree-supervisor role** are supported by an official page or supervisor roster plus recent direction work; an AMiner `person_detail` title by itself remains a provisional lead. Require a specific college or program only when the user explicitly requested it. If the user did request a computer-science program, a mathematics or medical-school professor needs an official cross-program admissions listing before occupying a main slot. Use the applicant's actual application cycle when interpreting `as_of_date`: in November before a December entrance exam, check registration immediately and describe preparation time as short, not ample. Never describe one school as easier for a second-tier undergraduate or infer “更稳/上岸概率” from its tier alone; published exam and admissions rules are needed for such a comparison.

Before finalizing that main list, check each row against **both** gates: current faculty in the requested program on an official page, and at least one dated, individually attributed direction paper. Move anyone missing either gate to labeled leads even if that leaves fewer than the user's maximum; “最多5位” is a ceiling, not a quota. If you state a national 985/211/双一流 tier for any school in the advice, first check [school-tiers.json](references/school-tiers.json); a provincial first-class discipline does not imply national 双一流 status. Without published admissions evidence, do not attach “冲刺/匹配/稳妥” labels to individual mentors merely from school tier or the applicant's undergraduate school.

For an applicant's **final mentor shortlist within named schools**, run `scripts/shortlist_gate.py` on the proposed main rows before writing the answer. Pass `--degree master|doctor`, `--allowed-domains` with the named schools' `edu.cn` domains, and `--candidates-json` as a JSON array. Pass `--program-terms` with exact official program/college names **only if the user specified that program or college**; otherwise omit it and state each approved person's actual college and degree track. Each row needs `name`, `school_domain` (for example `suda.edu.cn`), `official_profile_url`, and `paper: {title, year}`; add `person_id` and `paper.aminer_id` when using AMiner paper attribution, plus `school_names` for its author-affiliation check. If a separate official supervisor roster establishes the degree role, add `official_supervisor_url`. The script re-fetches official pages and exact AMiner IDs, returns `approved` and `leads`, and rejects individual admission bands. Use only `approved` rows in the main list, copy its canonical name exactly, and put rejected rows under leads with the missing evidence stated. An inaccessible page fails closed; the script does not certify research fit or current openings, which still require the ordinary source review. Do not silently omit the gate or override a failure to fill a quota.

The gate approves a **candidate with the exact paper submitted in that row**, not every other paper later found for that person. If a paper failed author or affiliation checks in an earlier gate run, do not cite it as verified work or claim its author institution was checked just because a different paper later passed. Any supplemental paper needs its own author and affiliation evidence, or an explicit "未核实" label.

**Order for an applicant request within named schools:** (1) Run one `profile` search and scan the relevant official faculty directories with `faculty_directory.py --degree ...`; when the user did not name a college, use AMiner's university-wide leads to identify relevant colleges rather than silently scanning only the applicant's undergraduate major. (2) From `official_role_paper_leads`, choose 2–5 people with direction-matched papers; fetch individual pages if needed to copy exact titles/years. People lacking a dated paper stay under leads for now. (3) **Make the next tool call `shortlist_gate.py` on those rows, before any `person` deep dive, further school search, or final answer.** (4) Compare the first approved list with `official_role_without_dated_paper` and other identified faculty whose official topic is an especially close match to the user's project. If a close match is missing while the main list is filled with adjacent directions, do one targeted AMiner paper check, rerun the gate, and replace a weaker approved row only when the close match passes. Do not fill all slots merely because they are available. Official faculty pages can supply paper attribution without AMiner. If no eligible row exists, the gate is unnecessary and the answer must say no verified recommendations. This sequence reserves tool capacity for the check that defines the main list.


### 6. Person deep-dive and comparison

When the user names specific scholar(s), deep-dive the person(s) directly. If they also name a research focus, include that focus and its title-language aliases so the paper search does not stop at a generic by-name list:

`--school` applies to every name in one `person` call. For scholars at different schools, run one call per scholar with that scholar's school; do not comma-join distinct schools in `--school` (that fails identity resolution). For a broad comparison of **recent** work, check whether the bounded focus and newest-paper samples cover the main model families or subfields visible in each person's profile. If a prominent line appears only in older papers, use a small author-and-title/subfield follow-up search and verify its latest papers with `paper_detail` before concluding that the line has no recent work. Keep this follow-up symmetric for the people compared.

Treat a named model series as a **family**, not a single base paper: for each scholar, choose at most two prominent series named in their verified work or official homepage, search the series stem plus recent variants in paper titles (for example `MiniCPM` also warrants checking `MiniCPM-V`), and verify the exact target author and affiliation in `paper_detail`. A broad newest/citation sample can omit a major branch while still returning the older base model. If this bounded follow-up finds no verified recent variant, say the series coverage is uncertain; do not imply the scholar lacks that branch.

Keep official-page lookup bounded: try a known personal/department page and at most one alternate official faculty-directory path per person. If both fail or only a general homepage loads, mark current affiliation or role unverified and spend the remaining tool turns on recent paper evidence; repeated guesses at personal URLs do not improve the research comparison. An AMiner profile can still support provisional identity and publication analysis, while official current employment and admissions stay explicitly unresolved.

A student's own major or background is context for the fit assessment, not automatically a narrow paper-search filter. For a request to map a scholar's overall research, first identify the scholar's broad field from the verified profile or official page, then search its main subdirections and use `--broad-context` to cover recent and influential work outside the primary term. One interest label must not stand in for a whole research portfolio. If no reliable field is known yet, begin with name and school, then refine after inspecting the profile.

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
2. A comparison table with institution, department, advisor, direction fit, recent activity, academic collaboration, industry collaboration, confidence, and evidence links. Include a reach/match/safer band only when the user has supplied an applicant profile; a faculty title or school name alone cannot establish application difficulty.
3. Two to four evidence-backed reasons per candidate.
   Link the official personal page whenever it is the basis for a current faculty title, department, or supervisor role claim; an unlinked statement that a page was checked is not reviewable.
4. Risks and items requiring verification, especially current affiliation, current recruitment status, degree eligibility, and admissions deadlines.
5. A cost summary listing APIs called, calls per API, unit price, total price, and unavailable evidence.

If a returned person's `role_unverified` is true, call them a **researcher lead (学者线索)**, not a confirmed advisor; do not count them as a verified supervisor or advise immediate outreach. A paper affiliation establishes where that paper was signed, not current employment or graduate-supervisor eligibility. If no verified advisor remains, say so plainly and point to the official faculty/admissions roster for the missing check.
Across every workflow, a professor or associate-professor title does not establish master's or doctoral supervision eligibility, current openings, or the student's admission chances. Never turn an unverified degree-specific status into wording such as “硕士招生无虞”. Do not claim a junior faculty member, a less selective school, or a lower school tier means easier admission or closer guidance without current official admissions evidence.
Do not add "skipped paper-author entries" warnings across passes and call the sum distinct scholars; the same author or paper may be inspected twice.

Use these labels consistently:

- **Verified fact**: directly supported by returned AMiner data or an official source.
- **Derived assessment**: computed from cited facts using the rubric.
- **Needs verification**: unavailable, stale, ambiguous, or only found in a non-authoritative source.

## Guardrails

- Never claim guaranteed admission or provide a numerical admission probability without an appropriate validated admissions dataset.
- Never claim an advisor is recruiting from publication activity alone. Direct users to the official department/advisor page.
- If an AMiner paper year is later than the current date, treat the record as provisional until the publisher or preprint source confirms its date and status. Do not use it as established recent output. Do not infer a scholar's age, supervision availability, or admissions odds from title or citation count.
- Never call unresolved name collisions verified people or emit an AMiner profile URL for them.
- Keep Latin-script names in Latin script when an exact person ID or official page does not supply a Chinese spelling, including in excluded-person notes. Pinyin alone cannot distinguish Chinese surnames or given names.
- Do not claim international collaboration: the current implementation classifies academic/industry affiliation only and has no verified country dimension.
- Never present co-authorship breadth as employment history: AMiner paper data cannot verify that a scholar held a position at a collaborating organization.
- Never claim a scholar is a recently hired advisor without positive evidence; use `--hired-since`, report its `no_hire_evidence` bucket honestly, and web-verify those entries against official sources when web tools are available.
- Reputation content from the web is anonymous opinion: present it attributed and unverified, never as fact, and never let it alter evidence-based scores.
- Do not expose tokens or include them in logs, generated files, commands, or error messages.
- Minimize personal data. Do not persist an applicant profile unless explicitly requested.
- Report empty or partial results honestly; never fabricate advisors, papers, affiliations, collaborations, or rankings.
- Ask before a paid or high-volume call sequence when API pricing or limits indicate material cost.
