You are step 2 of 4 (reproduce). Commands run at the repository root. Keep your thinking short.

Issue:
{problem_description}

Localization:
{localization?}

Write a reproduction script at /tmp/repro_issue.py (outside the repository) and run it, in ONE run_command call:

cat > /tmp/repro_issue.py <<'PY'
import sys
from <package> import <ENTRY owner>
cases = [  # (label, input, expected output the issue asks for)
    ...
]
failed = 0
for label, value, expected in cases:
    actual = <call ENTRY with value>
    ok = actual == expected
    failed += not ok
    print("PASS" if ok else "FAIL", label, repr(value), "->", repr(actual), "expected", repr(expected))
sys.exit(1 if failed else 0)
PY
python /tmp/repro_issue.py

Cases: the issue's own example, then boundary variants of it (empty input, a single item, the special element at the start, at the end, repeated, and in the middle). Expected values come from the issue, never from what the code currently returns. Also include one ordinary case that already works, as a guard.

The script must print at least one FAIL on the current code. If every case passes, the cases don't capture the issue: rewrite them (at most 2 more calls).

Then stop calling tools and answer with text only:
REPRO: /tmp/repro_issue.py
FAILS WITH: <each FAIL line, copied>
