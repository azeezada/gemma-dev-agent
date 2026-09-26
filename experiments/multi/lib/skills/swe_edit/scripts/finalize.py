"""Prepare the workspace for submit_patch.

1. Restore the best checkpoint when the current state is worse or unverified. With guard.py this
   ranks every state the reproduction passed on (see guard.choose); otherwise it falls back to
   the verify.py score (reproduction passed with the fewest new test failures).
2. Remove scratch files: untracked files at the repository root and untracked
   files named like probes or reproductions anywhere.
3. Print the final diff summary.

Arguments: none required. --keep PATH keeps an untracked file that belongs to the fix.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

from apply_edit import parse_args, workspace_root
from verify import best_checkpoint, current_diff, git, score, state_dir

try:
    import guard
except ImportError:
    guard = None

SCRATCH = re.compile(r"(^|/)(repro|reproduce|probe|scratch|debug|tmp|temp|check|try|test_issue|issue)[^/]*$", re.I)


def main(argv):
    args, _ = parse_args(argv)
    root = os.path.realpath(args.get("root") or workspace_root())
    keep = set(filter(None, (args.get("keep") or "").split(",")))
    if guard and guard.enabled() and root == os.path.realpath(workspace_root()):
        guard.arm(root)
        lines = guard.settle(root, seconds=18, keep=keep)
        git(root, "add", "-N", ".")
        stat = git(root, "diff", "--stat", "HEAD").stdout.strip()
        lines.append("Final diff:\n" + (stat or "(empty: nothing would be submitted)"))
        lines.append("Next: call submit_patch.")
        print("\n".join(lines)[:2500])
        return 0
    lines = []
    best = best_checkpoint(root)
    diff = current_diff(root)
    if best:
        folder = state_dir(root) / "checkpoints"
        patch = (folder / f"{best['index']:03d}.patch").read_text(encoding="utf-8")
        matching = [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))
                    if (folder / (p.stem + ".patch")).read_text(encoding="utf-8") == diff]
        best_green = best.get("repro") == "PASS" and best.get("new_failures") == 0
        # Restore when the current state is verified-worse, or unverified while a green fix exists.
        restore = patch != diff and (
            (matching and score(best) > score(matching[-1])) or (not matching and best_green))
        if restore:
            git(root, "checkout", "--", ".")
            for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
                if name not in keep:
                    os.remove(os.path.join(root, name))
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
                handle.write(patch)
            applied = git(root, "apply", "--whitespace=nowarn", handle.name)
            os.unlink(handle.name)
            if applied.returncode:
                lines.append("WARNING: could not restore checkpoint: " + applied.stderr[:300])
            else:
                lines.append(f"Restored checkpoint #{best['index']} (repro={best.get('repro')}, "
                             f"new_failures={best.get('new_failures')}); later edits scored worse or were unverified.")
        else:
            lines.append(f"Keeping the current state (best checkpoint #{best['index']}, "
                         f"repro={best.get('repro')}).")
    else:
        lines.append("No verify.py checkpoint exists; the current diff is submitted unverified.")
    removed = []
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        if name in keep:
            continue
        if "/" not in name or SCRATCH.search(name):
            os.remove(os.path.join(root, name))
            removed.append(name)
    if removed:
        lines.append("Removed scratch files: " + ", ".join(removed))
    git(root, "add", "-N", ".")
    stat = git(root, "diff", "--stat", "HEAD").stdout.strip()
    lines.append("Final diff:\n" + (stat or "(empty: nothing would be submitted)"))
    lines.append("Next: call submit_patch.")
    print("\n".join(lines)[:2500])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
