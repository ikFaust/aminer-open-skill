---
description: Discover schools and recommend prospective advisors using AMiner evidence
argument-hint: [direction, school/tier, department, collaboration preference, or applicant profile]
allowed-tools: Read, Bash, Glob, Grep
---

# /aminer-advisor-recommender

The user requested an AMiner-backed school and prospective-advisor recommendation:

```text
$ARGUMENTS
```

Follow `${CLAUDE_PLUGIN_ROOT}/SKILL.md` and preserve the user's wording.

## 1. Preflight

Check only whether the token exists; never print it:

```bash
[ -n "${AMINER_API_KEY:-}" ] && echo "AMINER_API_KEY exists" || echo "AMINER_API_KEY missing"
```

If missing, stop and direct the user to the AMiner console or the repository token-setup tool.

## 2. Choose one workflow

- `named`: the user names a school, optional department, and research direction.
- `tier`: the user gives a direction and a tier such as 华五, 985, or 211.
- `collaboration`: the user prioritizes academic or industry collaboration breadth.
- `profile`: the user supplies education, grades/rank, projects, publications, or internships and wants school/advisor matching.
- `discover`: the user gives a direction and asks which institutions are active, without preselecting schools.

Ask only for essential missing fields. For profile mode, require target degree, direction, undergraduate institution, grade or rank, and meaningful project/research experience. Target schools are optional: by default the script discovers direction-evidenced schools and expands an aspirational list downward to form a cross-tier portfolio.

For a large tier, ask for a region or smaller school set before broad retrieval. Let the script enforce `--max-cost 5`; use `--yes` only after showing the estimate and obtaining explicit confirmation.

## 3. Run

Build the shortest command described in `${CLAUDE_PLUGIN_ROOT}/SKILL.md`. Always call:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recommend.py" ...
```

For profile mode, place temporary applicant JSON outside the plugin directory and remove or retain it according to the user's request. Never persist personal data by default.

## 4. Present

Render the returned JSON as a concise Chinese comparison table. Include:

- school, department when verified, candidate name, direction fit, recent activity, collaboration evidence, confidence, and AMiner link;
- verified facts, derived assessments, and items needing official verification;
- exact API call and cost summary;
- an explicit statement that candidates are not confirmed current supervisors and reach/match/safer labels are not admission probabilities.

If the API returns `errors`, distinguish API failure from a successful empty result. For no matches, show the returned fallback suggestions. Never fabricate a scholar, affiliation, collaboration, or recruitment status, and never claim international collaboration from the current output.
