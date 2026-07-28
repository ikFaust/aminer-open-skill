# Recommendation rubric

Use scores to organize evidence, not to manufacture precision. Report component scores and confidence.

| Component | Default weight | Evidence |
| --- | ---: | --- |
| Research-direction fit | 35 | Recent titles, abstracts, keywords, interests |
| Recent activity | 20 | Relevant output in the selected time window |
| Institution/department constraint | 15 | Resolved current affiliation |
| Academic collaboration breadth | 10 | Distinct verified academic affiliations among collaborators |
| Industry collaboration breadth | 10 | Distinct verified company affiliations among collaborators |
| Applicant-experience fit | 10 | Topic and method overlap with projects, papers, or internships |

## Confidence

- **High**: exact identity and organization are resolved, role verification succeeds, and multiple direction-matched records are available.
- **Medium**: exact identity and organization are resolved with multiple direction-matched records, but role is unverified.
- **Low**: identity is ambiguous, evidence is sparse, or major components are unavailable.

## Reach/match/safer bands

Use only as heuristic portfolio bands. Consider school preference, direction fit, applicant preparation, and evidence quality. Do not convert the bands into admission probabilities. Always explain that advisor availability, quotas, exams, funding, and the current admissions cycle require official verification.

The bundled script uses a transparent readiness heuristic from normalized GPA, class-rank percentile, research projects, publications, and internships; adjusts for research-experience overlap; and penalizes the gap between undergraduate-institution level and target-school difficulty. Unlisted universities are not silently promoted above the ordinary-undergraduate baseline. Profile mode also attempts to add lower-tier schools supported by direction-matched AMiner papers. Treat the result as a reproducible ordering aid only. Do not show the internal number as an admission score or probability.

## Advisor-status boundary

An AMiner author or scholar record is a prospective advisor candidate, not proof that the person is faculty, eligible to supervise the requested degree, or currently recruiting. Prefer a returned position when available, but always verify role, department, and recruitment on an official university page.
