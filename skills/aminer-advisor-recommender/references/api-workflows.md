# AMiner API workflows

Base URL: `https://datacenter.aminer.cn/gateway/open_platform`.

Send `Authorization: ${AMINER_API_KEY}` and `X-Platform: openclaw`. For POST requests, also send `Content-Type: application/json;charset=utf-8`.

## Candidate discovery

1. Resolve institutions with `org_search` (free).
2. Use `person_search` (free) when a scholar name or institution is known.
3. Use `paper_search_pro` (¥0.01) or `paper_qa_search` (¥0.05) to discover scholars by research direction when names are unknown.
4. Deduplicate candidates by AMiner person ID and disambiguate by affiliation and recent papers.

## Evidence enrichment

- Use free result fields first.
- Use `person_figure` (¥0.50) for research interests and work-history enrichment when needed.
- Use `person_detail` (¥1.00) only for shortlisted candidates requiring deeper profile evidence.
- Use `person_paper_relation` (¥1.50) only when free/low-cost paper searches cannot establish recent direction fit.
- Use `paper_info` (free) to normalize a small batch of known paper IDs.

## Institution expansion

Use `organization_person_relation` (¥0.50) only after resolving a canonical organization ID. Pagination increases cost; estimate total calls before expanding.

## Collaboration analysis

No dedicated collaboration endpoint is assumed. Derive collaboration evidence from verified paper authors and affiliations returned by paper endpoints. Count distinct academic, industry, and international organizations in a declared time window. If affiliation fields are absent, mark collaboration breadth as unavailable rather than inferring it.

`paper_info` returns author names but not author organization IDs. Use it for free candidate discovery. Use `paper_detail` (¥0.01 per inspected paper) to obtain author `org` and `orgid`, then batch `org_detail` (¥0.01 per call) to verify organization type when available. Exclude the candidate's own organization by ID, not by translated name alone.

## Cost guardrails

Default to at most 10 candidates. Fetch paid details only for a shortlist. Before any chain estimated at ¥5.00 or more, show the planned APIs, calls, unit prices, and total, then request confirmation.
