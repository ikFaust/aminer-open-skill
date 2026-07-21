# School-tier filters

Load machine-readable groups from `school-tiers.json`.

- Treat 华五, 985, 211, and 双一流 as user-selected filters, not timeless rankings.
- Allow users to override a group with an explicit school list.
- Keep institution identity separate from department strength and advisor fit.
- Do not infer admissions difficulty solely from a tier label.
- Before release, review the bundled lists against the cited Ministry of Education source and record the review date in the JSON file.

“华五” is a conventional informal grouping: Fudan University, Shanghai Jiao Tong University, Nanjing University, Zhejiang University, and University of Science and Technology of China.

The 985 and 211 labels describe historical projects. Preserve them because users commonly request these filters, but explain that current policy discussions often use 双一流 classifications instead.
