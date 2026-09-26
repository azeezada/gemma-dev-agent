---
name: swe_locate
description: Return a compact source map, alternative operation sites, actual function bodies, callers and test nodes without a separate research agent. Use first when investigating a bug.
---

Use run_skill_script with skill_name="swe_locate". The scripts discover the repository root and never modify it.

- scripts/find_candidates.py: args={"query":"full issue text", "terms":"optional identifiers", "limit":8}. Exact text and general operation families are searched independently. Ranked candidates and the operation map are hypotheses, not proof. Inspect distinct mechanisms when a terse issue is ambiguous.
- scripts/inspect_symbol.py: args={"file":"pkg/module.py", "symbol":"Class.method"}. Returns numbered executable body lines, callers, and test node names. Omit symbol to get a compact symbol outline. Use this instead of reading a long function signature or scrolling a file from its first line.
