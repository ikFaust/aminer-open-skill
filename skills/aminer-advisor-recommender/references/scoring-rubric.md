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

- **High**: identity and current affiliation resolved; multiple recent records support the direction; collaboration affiliations are available.
- **Medium**: identity is resolved but one major component is sparse or stale.
- **Low**: affiliation is ambiguous, evidence is old, or major components are unavailable.

## Reach/match/safer bands

Use only as heuristic portfolio bands. Consider school preference, direction fit, applicant preparation, and evidence quality. Do not convert the bands into admission probabilities. Always explain that advisor availability, quotas, exams, funding, and the current admissions cycle require official verification.

The bundled script uses a transparent readiness heuristic from normalized GPA, class-rank percentile, research projects, publications, and internships, then adjusts it slightly for research-experience overlap. Treat the result as a reproducible ordering aid only. Do not show the internal number as an admission score or probability.

## Advisor-status boundary

An AMiner author or scholar record is a prospective advisor candidate, not proof that the person is faculty, eligible to supervise the requested degree, or currently recruiting. Prefer a returned position when available, but always verify role, department, and recruitment on an official university page.
