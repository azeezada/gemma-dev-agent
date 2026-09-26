---
name: swe_locate
description: Rank hypotheses for where the bug is, trace each suspect function to the public API a user calls, and show real function bodies with how callers use them. Read-only.
---

Use run_skill_script with skill_name="swe_locate". Scripts find the repository root and never modify it.

- scripts/find_candidates.py: args={"query":"full issue text"}. Starts with HYPOTHESES H1..Hn: an operation that can lose or change data, where it is, and the public entry point that reaches it. Then ranked files and related tests. Add "detail": 2 to also see the code at the top 2 sites and how their callers use the result.
- scripts/entry_points.py: args={"file":"pkg/module.py", "symbol":"Class.method"}. Caller chains up to public functions; call those with realistic input to reproduce.
- scripts/inspect_symbol.py: args={"symbol":"pkg/module.py:Class.method"}. The function body with line numbers, USED BY (the caller lines that consume its result), and test names. "file" + "symbol" separately, a symbol alone, or "pkg/module.py:LINE" also work.
