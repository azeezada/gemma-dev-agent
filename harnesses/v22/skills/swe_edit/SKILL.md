---
name: swe_edit
description: Apply checked source edits and run bounded focused tests without modifying the working tree during baseline comparison.
---

Use run_skill_script with skill_name="swe_edit". Scripts discover the workspace root.

- scripts/apply_edit.py: args={"file":"pkg/module.py", "old":"exact old lines", "new":"replacement"}. Optional occurrence selects an ambiguous match. For several coherent changes use args={"edits":[{"file":...,"old":...,"new":...}, ...]}; all replacements and syntax checks succeed before files are written. Never include line-number prefixes in old/new. Copy the source text without extra escaping. On error, use the printed nearby text.
- scripts/run_tests.py: args={"targets":"tests/test_module.py::test_name", "baseline":"true", "timeout":20}. Optional k is a pytest name expression. Default 20-second budget covers testing plus any baseline comparison; never begin with the full suite. Baseline runs in a separate clean copy and never stashes or changes your patch. A test whose old expectation is deliberately changed by the issue needs interpretation; do not revert a correct fix solely for that reason. Timeout, collection failure and no tests selected are never passes.
