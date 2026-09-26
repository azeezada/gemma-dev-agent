import sys
from rich.text import Text
cases = [("empty", "", ""), ("plain", "line", "line"), ("end", "line\n", "line\n"), ("start", "\nline", "\nline"),
         ("repeated", "line\n\n", "line\n\n"), ("middle", "a\nb\n", "a\nb\n")]
failed = 0
for label, value, expected in cases:
    actual = Text.from_ansi(value).plain
    ok = actual == expected
    failed += not ok
    print("PASS" if ok else "FAIL", label, repr(value), "->", repr(actual), "expected", repr(expected))
sys.exit(1 if failed else 0)
