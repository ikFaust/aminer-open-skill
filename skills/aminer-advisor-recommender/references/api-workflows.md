# AMiner API workflows

Base URL: `https://datacenter.aminer.cn/gateway/open_platform`.

Send `Authorization: ${AMINER_API_KEY}` and `X-Platform: openclaw`. For POST requests, also send `Content-Type: application/json;charset=utf-8`.

## Candidate discovery

1. Resolve the university's first-level ID with free `org_search`. For combined university/department strings, call `org_disambiguate_pro` (¥0.05) and take `一级ID`/`一级`, not `二级ID`; if it fails, resolve the explicitly extracted university name separately. If free search returns a translated or fuzzy name without an exact name/alias match, confirm the ID with `org_disambiguate_pro` before use.
2. Use `person_search` (free) when a scholar name or institution is known.
3. For a named university and direction, first use `paper_qa_search` (¥0.05) with `org_id: [first-level ID]`, `use_topic: true`, and `query: "direction term"`. Do not add `org_terms`: the two institution filters combine by OR, so the result would no longer be school-ID restricted. Search up to three direction/alias terms separately. If the ID-filtered pass is empty, use `paper_search_pro` (¥0.01) with `org` and `query`. Inspect a bounded, round-robin paper sample across aliases; only if no candidate survives identity checks use one bounded global `query` pass and check affiliations afterward. For institution discovery without a known school, use global `paper_search_pro` for seed schools, then `discover --schools` for first-level org-ID comparison under the same queries and recent-year window.
4. `paper_info` author entries contain names only; `paper_detail` supplies author `org` and `orgid`, but neither endpoint supplies a person ID.
5. Constrain paper authors by the target university `orgid` or explicit university text in their affiliation (paper authors often have department-level `orgid`). Then accept only exact-name profiles at the university in `person_search`. Keep multiple matches unresolved instead of merging or guessing. A requested department remains unverified until its official faculty page confirms it.
6. Use `discover` when no institution is known: aggregate organization IDs from direction-matched paper details as a seed sample. For a school comparison, include plausible schools that this seed missed, then query each selected first-level org ID with identical terms, paper limit and five-year window. Count only papers whose detailed author affiliation matches that school. Return zero or unresolved schools visibly; label the result a bounded comparison, not a global ranking.

## Evidence enrichment

- Use free result fields first.
- `person_detail` (¥1.00) is available through `--verify-roles N` for per-school role checks or directly via `scripts/aminer_client.py --api person_detail --params '{"id":"PERSON_ID"}'` for a final cross-school shortlist. The shared `call_aminer_api` wrapper, when present, instead takes `person_id` and maps it to the HTTP `id` parameter; the same distinction applies to `person_paper_relation`. Do not rerun broad school discovery to verify positions; current department and actual supervisor eligibility still need official sources.
- `person_figure` (¥0.50) is opt-in for structured career evidence. `person_paper_relation` (¥1.50) is enabled as a named-person fallback when current-organization name search returns no usable recent direction paper.
- Use `paper_info` (free) to normalize a small batch of known paper IDs.

## Institution expansion

`organization_person_relation` (¥0.50) is reserved in the client but not enabled by this workflow.

## Named scholar and focus

For a named scholar, use `person_search` to establish the profile and university first. With a specified research focus, search recent papers by `author` plus the **extracted first-level university name** in `org` using `paper_search_pro`'s `query` field (title and keyword coverage); a profile's full institute/department/university affiliation can make the API return empty. Include English title-language aliases where appropriate. The controlled `code` case retrieved Zhang Ming's MARCO paper with `query`, while `keyword` missed it in two repeated calls. Retrieve `paper_detail` for the bounded result set and include a paper only when a same-name author has the selected university's organization ID or explicit university affiliation. If this returns no usable recent focus paper, request `person_paper_relation` with the HTTP parameter `id` set to the selected person ID, filter its returned `author_id` to that ID, and verify actual years and title/abstract focus using batched `paper_info`. Return relevant papers first; label other ID-linked recent works `topic_match: false` so they are context, not proof of the requested focus. The relation can recover works signed at an earlier institution, but does not verify the original author affiliation or final publication status; label that provenance and check primary sources for important claims. If no university profile matches the user's explicit school, return disambiguation instead of selecting a high-citation namesake elsewhere. Verify current appointment, department, and research direction with official pages before optional collaboration analysis.

## Collaboration analysis

No dedicated collaboration endpoint is assumed. Derive collaboration evidence from verified paper authors and affiliations returned by paper endpoints. Count distinct academic and industry organizations in the inspected paper sample. If affiliation fields are absent, mark collaboration breadth as unavailable rather than inferring it. Country metadata is not verified, so do not claim international collaboration.

`paper_info` returns author names but not author organization IDs. Use it for free candidate discovery. Use `paper_detail` (¥0.01 per inspected paper) to obtain author `org` and `orgid`, then batch `org_detail` (¥0.01 per call) to verify organization type when available. Exclude the candidate's own organization by ID, not by translated name alone.

## Cost guardrails

Default to at most 10 candidates. The CLI computes a per-school worst-case estimate before any API request. At or above `--max-cost` (default ¥5.00), show the estimate and stop; pass `--yes` only after explicit confirmation. API errors surface structurally and failed calls are excluded from the successful-call cost ledger.
