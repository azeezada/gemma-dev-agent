You are step 3 of 4 (repair). Commands run at the repository root. Earlier steps found the code and wrote a failing reproduction; do not search the repository again.

Issue:
{problem_description}

Localization:
{localization?}

Reproduction (/tmp/repro_issue.py, exits 0 only when fixed):
{reproduction?}

1. Call run_skill_script(skill_name="swe_locate", file_path="scripts/inspect_symbol.py", args={"symbol": "<file>:<Class.method from SITE, or its line number for module-level code>"}). It prints the current lines and USED BY: how callers consume the result. Your change must make the caller's output match EXPECTED.
2. Edit SITE with run_skill_script(skill_name="swe_edit", file_path="scripts/apply_edit.py", args={"file": "<file>", "old": "<exact current lines>", "new": "<replacement lines>"}). Make the smallest change at SITE; keep callers unchanged unless SITE cannot express the fix. Copy old lines exactly; backslash escapes inside string literals stay exactly as in the source.
3. Immediately call run_skill_script(skill_name="swe_edit", file_path="scripts/verify.py", args={"repro": "/tmp/repro_issue.py"}).
4. The reproduction is the specification. If verify reports REPRO PASS, you are done: stop editing at once, whatever else it prints. The passing state is checkpointed, and existing tests that assert the old behaviour the issue asks to change are expected to fail. Otherwise read the FAIL lines, change your edit, and verify again. At most 3 edits in total.

Then stop calling tools and answer with text only:
CHANGED: <file:function and what changed>
VERIFY: <REPRO PASS, or the remaining FAIL lines>
