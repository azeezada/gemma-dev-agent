---
name: swe_branch
description: Work in an isolated candidate checkout (repro, read, run, edit, verify, diff) and judge or adopt candidates. Parallel agents must each use their own id.
---

Use run_skill_script with skill_name="swe_branch" and file_path="scripts/wt.py". Every call passes "op" and your candidate "id". The checkout is created on first use. Every result shows the time used; after the solver deadline, work ops answer TIME UP.

- args={"op":"repro", "id":"1", "file":"/tmp/repro_1.py"}: register your reproduction; it runs on the clean code and must fail there.
- args={"op":"read", "id":"1", "file":"pkg/module.py", "symbol":"function_name"}: that function with line numbers (or "start"/"end" for a line range).
- args={"op":"run", "id":"1", "cmd":"python /tmp/repro_1.py"}: run a command inside your checkout.
- args={"op":"edit", "id":"1", "file":"pkg/module.py", "old":"exact lines", "new":"replacement"}: one checked replacement. It reruns your reproduction; when it passes it checkpoints and keeps the best candidate in the workspace.
- args={"op":"verify", "id":"1"}: reproduction plus existing tests of the changed modules.
- args={"op":"diff", "id":"1"}: your patch.
- args={"op":"judge"}: test every candidate against every reproduction and put the best into the workspace.
- args={"op":"adopt", "id":"2"}: copy that candidate's patch into the real workspace.
