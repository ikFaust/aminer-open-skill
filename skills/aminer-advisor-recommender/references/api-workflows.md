# AMiner API workflows

Base URL: `https://datacenter.aminer.cn/gateway/open_platform`.

Send `Authorization: ${AMINER_API_KEY}` and `X-Platform: openclaw`. For POST requests, also send `Content-Type: application/json;charset=utf-8`.

## Candidate discovery

1. Resolve institutions with `org_search` (free).
2. Use `person_search` (free) when a scholar name or institution is known.
3. Use `paper_search_pro` (¥0.01) or `paper_qa_search` (¥0.05) to discover scholars by research direction when names are unknown.
4. `paper_info` author entries contain names only; `paper_detail` supplies author `org` and `orgid`, but neither endpoint supplies a person ID.
5. Constrain paper authors by exact target `orgid`, then accept only exact-name, exact-organization `person_search` matches. Keep multiple matches unresolved instead of merging or guessing.
6. Use `discover` when no institution is known: aggregate organization IDs from direction-matched paper details and report paper evidence, not a global ranking.

## Evidence enrichment

- Use free result fields first.
- `person_detail` (¥1.00) is enabled only through `--verify-roles N` for optional shortlisted role checks.
- `person_figure` (¥0.50) and `person_paper_relation` (¥1.50) are reserved in the client but not enabled by this workflow.
- Use `paper_info` (free) to normalize a small batch of known paper IDs.

## Institution expansion

`organization_person_relation` (¥0.50) is reserved in the client but not enabled by this workflow.

## Collaboration analysis

No dedicated collaboration endpoint is assumed. Derive collaboration evidence from verified paper authors and affiliations returned by paper endpoints. Count distinct academic and industry organizations in the inspected paper sample. If affiliation fields are absent, mark collaboration breadth as unavailable rather than inferring it. Country metadata is not verified, so do not claim international collaboration.

`paper_info` returns author names but not author organization IDs. Use it for free candidate discovery. Use `paper_detail` (¥0.01 per inspected paper) to obtain author `org` and `orgid`, then batch `org_detail` (¥0.01 per call) to verify organization type when available. Exclude the candidate's own organization by ID, not by translated name alone.

## Cost guardrails

Default to at most 10 candidates. The CLI computes a per-school worst-case estimate before any API request. At or above `--max-cost` (default ¥5.00), show the estimate and stop; pass `--yes` only after explicit confirmation. API errors surface structurally and failed calls are excluded from the successful-call cost ledger.
