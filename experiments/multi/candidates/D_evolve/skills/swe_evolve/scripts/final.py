"""Prepare the workspace for submit_patch: exactly the best candidate on top of the original code, no scratch files.

args: {}   (no arguments)
If no candidate improved on the original, the workspace is left as it is (minus scratch files).
"""
import sys
from pathlib import Path

from common import CANDS, apply_blocks, git, load_state, parse_candidate, save_state, untracked, workspace_root


def main(argv):
    root = workspace_root()
    state = load_state(root)
    best = state.get("best")
    if best and (CANDS / f"{best}.py").is_file():
        changes = apply_blocks(root, parse_candidate((CANDS / f"{best}.py").read_text(encoding="utf-8")))
        git(root, "checkout", "--", ".")
        for rel, new in changes.items():
            (Path(root) / rel).write_text(new, encoding="utf-8")
        state["applied_files"] = sorted(changes)
        print(f"workspace = original + best candidate {best}")
    else:
        print("no candidate improved on the original code; workspace left as is")
    keep = set(state.get("untracked_at_start", []))
    removed = []
    for rel in sorted(untracked(root) - keep):
        path = Path(root) / rel
        if path.is_file():
            path.unlink()
            removed.append(rel)
    if removed:
        print("removed scratch files:", ", ".join(removed))
    save_state(state)
    diff = git(root, "diff", "HEAD").stdout
    print(diff[-4000:] if diff else "(empty diff)")
    print("Now call submit_patch." if diff else "Nothing to submit yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
