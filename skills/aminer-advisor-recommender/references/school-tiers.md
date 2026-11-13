# School-tier filters

Load machine-readable groups from `school-tiers.json`. Its `reviewed_on` date records when the bundled school lists were checked; it is never the date of a live AMiner search. Use the current script result's `as_of_date` for retrieval dates.

- Treat 华五, 985, 211, and 双一流 as user-selected filters, not timeless rankings.
- The 211 set includes 985 universities; interpret “211” as non-985 only when the user says so.
- Allow users to override a group with an explicit school list.
- Keep institution identity separate from department strength and advisor fit.
- Do not infer admissions difficulty solely from a tier label.
- Before release, review the bundled lists against the cited Ministry of Education source and record the review date in the JSON file.

“华五” is a conventional informal grouping: Fudan University, Shanghai Jiao Tong University, Nanjing University, Zhejiang University, and University of Science and Technology of China.

The 985 and 211 labels describe historical projects. Preserve them because users commonly request these filters, but explain that current policy discussions often use 双一流 classifications instead.

The 双一流 group uses the second-round (2022) list of 147 institutions: all 211 institutions (dual-campus entries listed separately), the 25 non-211 first-round discipline-construction institutions, and the 7 institutions added in the second round. It is a heterogeneous group — members that are also 985/211 keep their higher level for banding, and 双一流-only members sit between ordinary undergraduate institutions and 211.

For 华东（上海、江苏、浙江、安徽、福建、江西、山东）且“非985的211”的请求，下表是资格候选池，不是学科排名或必查清单。先根据论文和官网教师线索选方向相关学校；不要因随意截取前十所而漏掉已有直接方向证据的学校。

| 省市 | 符合条件的学校 |
| --- | --- |
| 上海 | 上海大学、东华大学、华东理工大学、上海财经大学、上海外国语大学 |
| 江苏 | 苏州大学、南京师范大学、江南大学、南京航空航天大学、南京理工大学、河海大学、中国矿业大学、南京农业大学、中国药科大学 |
| 浙江 | 无 |
| 安徽 | 安徽大学、合肥工业大学 |
| 福建 | 福州大学 |
| 江西 | 南昌大学 |
| 山东 | 中国石油大学（华东） |

在华东非985的211医学影像分割检索中，安徽大学和福州大学已有方向论文线索，可作为初始比较候选；仍须以本次同口径检索决定是否入选，不能视为预设赢家。
