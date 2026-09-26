"""Everything a proposer needs, in one call: spec + its failures, focused code, last trial report, best candidate.

args: {"id": "g1a"}   (your candidate id; optional)
"""
import sys

from common import CANDS, SPEC, elapsed, git, load_state, parse_args, workspace_root
from focus import show


def main(argv):
    values, positional = parse_args(argv)
    cid = (values.get("id") or (positional[0] if positional else "") or "ID").strip().strip("'\"")
    root = workspace_root()
    state = load_state(root)
    spent = elapsed(state)
    out = [f"TIME: {spent:.0f}s since the first swe_evolve call (the whole task has about 280s)."]
    if spent > 200:
        out.append("TIME LOW: if STATUS below is SOLVED, reply SOLVED. Otherwise write one short candidate now.")
    base = state.get("baseline_spec") or {}
    if SPEC.is_file():
        spec_lines = SPEC.read_text(encoding="utf-8", errors="replace").splitlines()
        out.append(f"\n== SPEC /tmp/d_spec.py (on the original code: exit {base.get('exit')}, "
                   f"{base.get('passes', '?')} PASS / {base.get('fails', '?')} FAIL)")
        out += spec_lines[:70] + (["# ... (truncated)"] if len(spec_lines) > 70 else [])
        if base.get("fail_lines"):
            out.append("-- failing on the original code:")
            out += [f"  {line}" for line in base["fail_lines"]]
    else:
        out.append("\n(no spec was written; aim at the issue text)")
    for target in state.get("focus", [])[:2]:
        path, _, name = target.partition("::")
        if not name:
            source = git(root, "show", f"HEAD:{path}").stdout.split("\n")
            out.append(f"\n== FOCUSED FILE (original, module level): {path}")
            out += [f"{n:5d}  {line}" for n, line in enumerate(source[:120], 1)]
            continue
        view = show(root, path, name)
        if view:
            out.append("\n== FOCUSED CODE (original)\n" + view)
    if state.get("report"):
        out.append("\n== LAST TRIAL REPORT\n" + state["report"])
        best = state.get("best")
        if best and (CANDS / f"{best}.py").is_file():
            out.append(f"\n== BEST CANDIDATE SO FAR: /tmp/d_cands/{best}.py\n" + (CANDS / f"{best}.py").read_text())
    focus = (state.get("focus") or ["pkg/module.py::Class.method"])[0]
    if focus.endswith("::"):
        out.append(f"""
== HOW TO ANSWER
Write ONE candidate with ONE run_command call. Copy the old lines exactly from the FOCUSED FILE, without line numbers:
mkdir -p /tmp/d_cands && cat > /tmp/d_cands/{cid}.py <<'CAND_EOF'
# replace: {focus[:-2]}
<<<<<<< OLD
exact old lines
=======
new lines
>>>>>>> NEW
CAND_EOF
For a function instead, use '# target: path.py::Qual.name' followed by the complete new def.""")
        print("\n".join(out))
        return 0
    out.append(f"""
== HOW TO ANSWER
Write ONE candidate with ONE run_command call (a complete replacement of the function, same indentation rules as a
normal top-level def: the script re-indents it into the class):
mkdir -p /tmp/d_cands && cat > /tmp/d_cands/{cid}.py <<'CAND_EOF'
# target: {focus}
def ...complete new definition with its signature (the docstring may be omitted: the original is kept)...
CAND_EOF
For several functions, repeat the '# target: path.py::Qual.name' header before each definition.
Do not edit repository files directly; trial.py applies and scores candidates.""")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
