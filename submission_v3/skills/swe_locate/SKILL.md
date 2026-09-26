---
name: swe_locate
description: Rank the source files and lines most likely to cause an issue, with the tests near each file. Use it first when localizing a repository bug.
---

# swe_locate

Run with run_skill_script, skill_name "swe_locate". Pass arguments as an `args` object; each key becomes `--key value`. The script reads the repository in /workspace and never changes it.

## scripts/find_candidates.py
Rank source files for a set of terms and list the tests near each file.
- args: {"terms": "identifier1 identifier2 operation_word", "limit": 8}
- Use identifiers from the issue (classes, functions, parameters, options, message fragments) together with the operations that could cause the symptom (for example split, strip, join, pop, encode, close).
