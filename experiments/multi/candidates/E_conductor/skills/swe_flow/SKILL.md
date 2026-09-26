---
name: swe_flow
description: Start here. Ranks where the issue's terms live, installs the /tmp/flow.py controller, and starts the clock. After this first call, drive everything with run_command.
---

First and only skill call: run_skill_script(skill_name="swe_flow", file_path="scripts/flow.py", args={"issue": "<the issue text>"}).

Then use run_command only:
- `python3 /tmp/flow.py`: registers /tmp/spec.py (it must FAIL on the original code), checks your current change, and tells you the next step.
- `python3 /tmp/flow.py show FILE::NAME`: one function or class with line numbers and the lines that call it.
- `python3 /tmp/flow.py put FILE::NAME`: replace that whole function or class with the code you wrote to /tmp/new.py, then check.
- `python3 /tmp/flow.py put FILE:START-END`: replace those lines with /tmp/new.py, then check.

Write files with heredocs: `cat > /tmp/new.py <<'EOF'` ... `EOF`.
