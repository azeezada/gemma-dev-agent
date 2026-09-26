---
name: swe_edit
description: Make one checked source edit, verify it against a reproduction script and existing tests (automatic checkpoint), and prepare the workspace for submission.
---

Use run_skill_script with skill_name="swe_edit" and the script as file_path. Put arguments in args.

- scripts/apply_edit.py: args={"file":"pkg/module.py", "old":"exact existing lines", "new":"replacement lines"}. One replacement per call; copy old text from read_file output without line numbers. Refused if the file would not compile.
- scripts/verify.py: args={"repro":"/tmp/repro_issue.py"}. Runs the reproduction (must exit 0 only when fixed) and the existing tests of the changed modules, saves a checkpoint and ends with a VERDICT line. VERDICT: SUBMIT means stop editing: run finalize.py, then submit_patch.
- scripts/finalize.py: args={}. Restores the best checkpoint if later edits were worse, deletes scratch files, prints the final diff. Run it right before submit_patch.
- scripts/run_tests.py: args={"targets":"tests/test_x.py::test_name"}. Focused pytest run.

Every run of a /tmp/repro*.py script is checkpointed automatically once any of these scripts has run, and the output ends with a [guard] line giving the elapsed time.
