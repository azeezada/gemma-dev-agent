---
name: swe_edit
description: Apply source edits reliably and run focused tests with short summaries. Use it for every edit and every test run of a repository fix.
---

# swe_edit

Run these with run_skill_script, skill_name "swe_edit". Pass arguments as an `args` object; each key becomes `--key value`. Both scripts operate on the repository in /workspace and print at most a few thousand characters.

## scripts/apply_edit.py
Replace one snippet in a file. This is the only way to change source files.
- args: {"file": "pkg/module.py", "old": "<existing consecutive lines>", "new": "<replacement lines>"}
- Optional "occurrence": N when the old text appears more than once.
- Prints the diff on success. On failure it prints the closest matching regions; copy the old text from them and retry.
- Python edits that would not compile are refused, and the reason is printed.

## scripts/run_tests.py
Run focused pytest targets and summarize failures in a few lines.
- args: {"targets": "tests/test_module.py tests/test_other.py::test_name", "baseline": "true"}
- Optional "k": pytest -k expression; "timeout": seconds (default 45).
- With baseline true, each failing test is labelled "already failing before your change" or "NEW: introduced by your change".
