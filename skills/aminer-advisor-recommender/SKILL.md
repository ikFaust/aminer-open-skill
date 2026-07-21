---
name: aminer-advisor-recommender
description: Recommend Chinese universities, schools/departments, and prospective advisors using AMiner evidence and an applicant profile. Use when a user asks for advisors in a named university, school, or research direction; filters by university tier such as 华五/985/211/双一流; compares advisors by academic, international, or industry collaboration breadth; or wants reach/match/safer school and advisor suggestions based on undergraduate institution, grades, research, projects, publications, internships, degree target, and location preferences.
---

# AMiner Advisor Recommender

Use AMiner Open Platform REST APIs as the academic-data layer. Plan searches, normalize evidence, score candidates locally, and present explainable recommendations. Never treat a model's prior knowledge as current evidence.

## Route the request

Choose one workflow:

1. **Named institution**: school + school/department + direction -> advisor candidates.
2. **Tier and direction**: direction + 华五/985/211/双一流 or a user-provided school set -> institution and advisor candidates.
3. **Collaboration breadth**: direction/institution -> candidates -> coauthors and affiliations -> academic/industry/international collaboration comparison.
4. **Applicant matching**: applicant profile -> reach/match/safer school groups -> advisor candidates in each group.

For applicant matching, read [references/applicant-schema.md](references/applicant-schema.md). For ranking or collaboration analysis, read [references/scoring-rubric.md](references/scoring-rubric.md). For school-tier filters, read [references/school-tiers.md](references/school-tiers.md). Before calling AMiner, read [references/api-workflows.md](references/api-workflows.md).

## Preflight

1. Check whether `AMINER_API_KEY` exists without printing its value. Stop and direct the user to the AMiner console if it is missing.
2. Send the token unchanged in the `Authorization` header and send `X-Platform: openclaw`.
3. Use only endpoints and parameters documented in `references/api-workflows.md` or the repository's canonical API catalog. Never invent an endpoint or field.
4. Estimate cost before calling paid endpoints. Ask for confirmation before a planned chain costing at least ¥5.00.

## Collect candidates

1. Normalize the requested research direction into the user's wording plus a small set of English/Chinese aliases.
2. Resolve institutions and departments before resolving people.
3. Search broadly with free or low-cost endpoints, then fetch deeper evidence only for a short list. Default to 10 candidates unless the user specifies otherwise.
4. Disambiguate scholars by institution, department, research interests, and recent publications. Call every result a prospective advisor candidate until an official page confirms faculty role and supervision eligibility.
5. Use recent publications to verify direction fit. Prefer a five-year window unless the user requests another period.
6. Build collaboration evidence from coauthors and their affiliations. Do not infer an industry relationship from an email domain or model knowledge alone.
7. Preserve source identifiers, AMiner URLs, years, and the API used for every factual claim.

## Run workflows

Run commands from the skill directory. Save JSON when the result must be reused; otherwise read stdout and render the required user-facing table.

### 1. Named school, department, and direction

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

### 2. Direction within a school tier

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode tier \
  --tier "华五" \
  --direction "多模态大模型" \
  --aliases "multimodal large language model,MLLM" \
  --max-schools 5
```

For a large group such as 211, ask for region, explicit schools, or a smaller maximum before broad retrieval. Never silently imply that the first truncated schools represent the whole tier.

### 3. Collaboration breadth

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode collaboration \
  --school "清华大学" \
  --direction "computer vision" \
  --collaboration-type industry \
  --collaboration-papers 10
```

Collaboration evidence comes from paper coauthors' returned organization IDs. `paper_detail` and `org_detail` are paid but low-cost. Report the declared paper window and the number of inspected papers. A coauthored paper is evidence of publication collaboration, not proof of a formal partnership.

### 4. Applicant-profile matching

Collect the fields in `references/applicant-schema.md`, create a temporary JSON file, and run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" \
  --mode profile \
  --profile "/path/to/applicant.json" \
  --direction "robot learning" \
  --candidate-limit 10
```

Do not store the applicant file inside the skill unless the user explicitly asks. If target schools or a tier are missing, ask the user to provide them; AMiner academic data alone cannot infer a complete admissions portfolio.

## Control retrieval

- `--paper-limit`: papers returned per direction/school query; default 10.
- `--max-author-lookups`: distinct paper-author names resolved per school; default 30.
- `--candidate-limit`: final rows; default 10.
- `--collaboration-papers`: paid paper details inspected across candidates; default 0 except collaboration mode.
- `--max-schools`: school cap for tier queries; default 5.
- `--schools`: comma-separated explicit override for a tier.

Estimate the maximum paid cost before increasing these limits. The script returns an exact cost ledger after execution.

## Rank and classify

Apply the rubric in `references/scoring-rubric.md`. Keep raw evidence separate from derived scores.

- Use school tiers only as filters or user preferences, not as a universal quality ranking.
- Label reach/match/safer classifications as heuristic fit bands, never admission probabilities.
- Do not penalize missing data as if it were negative evidence. Mark the component `unknown` and lower confidence.
- Explain every score with observable evidence.

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
- Do not expose tokens or include them in logs, generated files, commands, or error messages.
- Minimize personal data. Do not persist an applicant profile unless explicitly requested.
- Report empty or partial results honestly; never fabricate advisors, papers, affiliations, collaborations, or rankings.
- Ask before a paid or high-volume call sequence when API pricing or limits indicate material cost.
