"""Check that /tmp/d_spec.py is a DISCRIMINATING specification: it must FAIL on the original code.

args: {}   (no arguments)
A spec that already passes on the unfixed code proves nothing; rewrite it until at least one check FAILS.
"""
import sys

from common import SPEC, clean_copy, dirty, load_state, run_spec, save_state, scout_clock, spec_bug, workspace_root


def main(argv):
    root = workspace_root()
    state = load_state(root)
    if not SPEC.is_file():
        print("ERROR: /tmp/d_spec.py does not exist. Write it first with run_command (cat > /tmp/d_spec.py <<'SPEC_EOF' ... SPEC_EOF).")
        return 2
    where = clean_copy(root) if dirty(root) else root
    result = run_spec(where)
    state["baseline_spec"] = result
    save_state(state)
    print(scout_clock(state))
    print(f"spec on ORIGINAL code: exit {result['exit']}, {result['passes']} PASS / {result['fails']} FAIL")
    print(result["tail"])
    if spec_bug(result):
        print("\nVERDICT: SPEC ERROR. The spec itself crashed before any check ran; fix the spec (imports, names).")
        return 1
    if result["exit"] == 0:
        print("\nVERDICT: NOT DISCRIMINATING. It passes on the unfixed code, so it cannot tell a fix from no fix. "
              "Add checks of the exact behavior the issue says is wrong (the issue's own example first) until one FAILS.")
        return 1
    if result["fails"] == 0:
        print("\nVERDICT: DISCRIMINATING but no PASS/FAIL lines. Use check(label, got, want) so partial progress is visible.")
        return 0
    print("\nVERDICT: DISCRIMINATING. Good: the spec fails now and should pass once fixed. Finish with your brief.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
