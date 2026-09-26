---
name: swe_survive
description: Survival guard. Freezes your reproduction, scores and checkpoints every workspace state, reverts anything worse than a passing fix, shows the clock, and says exactly what to do next.
---

Use run_skill_script with skill_name="swe_survive".

- file_path="scripts/edit.py", args={"file": "pkg/module.py", "old": "exact existing lines", "new": "replacement lines"}: make one checked replacement in a source file, then run the check automatically. Test files are refused.

The other operations use file_path="scripts/survive.py":

- args={"op": "arm", "repro": "/tmp/repro_issue.py"}: freeze the reproduction. It must FAIL on the untouched repository. It should print `ok: <case>` or `FAIL: <case> ...` per case and exit 1 if any case fails.
- args={"op": "check"}: run this right after EVERY edit. It scores the workspace with the frozen repro and the existing tests of the changed modules. After anything has passed, it automatically reverts to the best state when the new state is worse.
- args={"op": "guard"}: check, restore the best state, and print `ACTION:`. Obey it.
- args={"op": "final"}: restore the best state (or the best non-empty attempt), delete scratch files, print the final diff. Then call submit_patch.

Every result starts with `CLOCK:`, the time left. Edits to test files are reverted automatically, and scratch files inside the repository are deleted. Keep scratch in /tmp.
