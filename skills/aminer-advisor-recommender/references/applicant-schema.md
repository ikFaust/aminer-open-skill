# Applicant profile

Collect only fields needed for the requested recommendation.

## Core fields

```yaml
target_degree: master | phd
target_cycle: 2027
research_interests: []
undergraduate_institution: ""
major: ""
gpa: null
gpa_scale: null
rank_percentile: null
research_projects: []
publications: []
internships: []
preferred_regions: []
target_schools: []
school_tier_preferences: []
risk_preference: conservative | balanced | ambitious
```

Ask for target degree, research interests, undergraduate institution, grades or rank, and meaningful research/project experience when applicant matching is requested. Do not require irrelevant sensitive attributes.

Normalize grading scales before comparison. Treat a missing ranking, publication, or internship as unknown rather than as failure.

Use `target_schools` when the user names schools. Otherwise combine `school_tier_preferences` with region preferences and ask the user to narrow very large groups before broad retrieval.
