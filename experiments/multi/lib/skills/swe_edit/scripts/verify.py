"""Check the current change and checkpoint it when it is green.

Arguments (ADK passes them as --name value pairs):
  --repro PATH     optional script that exits 0 only when the issue is fixed
                   (write it outside the repository, e.g. /tmp/repro_issue.py);
                   also accepted as --script/--path/--file or a positional argument
  --tests TARGETS  optional pytest targets; default = existing tests of the changed modules
  --timeout N      seconds for the whole check (default 45, max 90)
  --root PATH      optional alternative checkout (used by candidate worktrees)

Every run records a checkpoint of the current diff with its score, so a later
edit that breaks a working fix can be undone by finalize.py. New test failures are
split into crash-type (errors, TypeError, ...) and assertion-type: a fix that changes
behavior an old test pinned is expected to fail a few assertions, so those alone do
not block the SUBMIT verdict. The first call also arms guard.py (checkpoint on every
passing reproduction run, best state restored before the time budget ends).
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from apply_edit import parse_args, workspace_root
from run_tests import clean_baseline, run_pytest

try:
    import guard
    from guard import current_diff, git, record, state_dir
except ImportError:  # copies of this script in other skills may ship without guard.py
    guard = None

    def state_dir(root):
        """Per-checkout scratch area outside the repository (never part of the patch)."""
        key = hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:12]
        path = Path(tempfile.gettempdir()) / "swe_state" / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def git(root, *args, timeout=20):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)

    def current_diff(root):
        git(root, "add", "-N", ".")
        return git(root, "diff", "HEAD").stdout

    def record(root, entry):
        directory = state_dir(root) / "checkpoints"
        directory.mkdir(exist_ok=True)
        index = len(list(directory.glob("*.json"))) + 1
        (directory / f"{index:03d}.patch").write_text(entry.pop("diff"), encoding="utf-8")
        entry["index"] = index
        (directory / f"{index:03d}.json").write_text(json.dumps(entry), encoding="utf-8")
        return index

REPRO_KEYS = ("repro", "script", "repro_path", "path", "file", "reproduction")


def changed_sources(root):
    names = git(root, "diff", "--name-only", "HEAD").stdout.split()
    names += git(root, "ls-files", "--others", "--exclude-standard").stdout.split()
    return [name for name in dict.fromkeys(names)
            if name.endswith(".py") and not re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$", name)]


def select_tests(root, sources, limit=3):
    """TestPrune-style: existing test files that import or name the changed modules."""
    tests_dirs = [d for d in ("tests", "test", "testing") if (Path(root) / d).is_dir()]
    candidates = []
    for directory in tests_dirs:
        candidates += [str(p.relative_to(root)) for p in Path(root, directory).rglob("test*.py")]
    scored = []
    for source in sources:
        module = source[:-3].replace("/", ".")
        if module.startswith("src."):
            module = module[4:]
        stem = Path(source).stem.lstrip("_")
        dotted_tail = module.split(".", 1)[-1]
        for test in candidates:
            name = Path(test).stem
            score = 3 if name in (f"test_{stem}", f"test{stem}") else 0
            try:
                text = Path(root, test).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"\b{re.escape(module)}\b|from \.?{re.escape(dotted_tail)} import|\b{re.escape(stem)}\b", text):
                score += 1
            if score:
                scored.append((score, test))
    ordered = [test for _, test in sorted(scored, key=lambda row: (-row[0], row[1]))]
    return list(dict.fromkeys(ordered))[:limit]


def run_repro(root, repro, timeout):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SWE_GUARD_CHILD"] = "1"  # this run is recorded by verify itself, not by the guard hook
    try:
        result = subprocess.run([sys.executable, repro], cwd=root, env=env, capture_output=True,
                                text=True, timeout=max(1, timeout))
        return result.returncode, (result.stdout + result.stderr)[-900:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


MAX_ASSERTION_FAILURES = 3


def score(entry):
    crash = entry.get("crash") if entry.get("crash") is not None else 0
    return (entry.get("repro") == "PASS", -int(crash), -int(entry.get("new_failures") if entry.get("new_failures") is not None else 99),
            bool(entry.get("tests_ran", False)))


def best_checkpoint(root):
    directory = state_dir(root) / "checkpoints"
    rows = [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))] if directory.is_dir() else []
    rows = [row for row in rows if row.get("changed")]
    return max(rows, key=lambda row: (score(row), row["index"])) if rows else None


def split_failures(failed_new, output=""):
    """(crash, assertion) counts by exception type; see guard.classify."""
    if guard:
        return guard.classify(failed_new, output)
    return 0, len(failed_new)


def main(argv):
    args, positional = parse_args(argv)
    workspace = workspace_root()
    root = os.path.realpath(args.get("root") or workspace)
    try:
        budget = max(5.0, min(90.0, float(args.get("timeout", 45))))
    except ValueError:
        budget = 45.0
    started = time.monotonic()
    remaining = lambda: budget - (time.monotonic() - started)
    repro = next((args[key] for key in REPRO_KEYS if args.get(key)), "") or (positional[0] if positional else "")
    if repro and not os.path.isabs(repro) and not os.path.isfile(repro):
        repro = os.path.join(root, repro) if os.path.isfile(os.path.join(root, repro)) else repro
    armed = bool(guard) and root == os.path.realpath(workspace) and guard.arm(root, repro or None)
    diff = current_diff(root)
    sources = changed_sources(root)
    lines = ["changed source files: " + (", ".join(sources) if sources else "none")]
    entry = {"changed": bool(diff.strip()), "diff": diff, "time": time.time(), "source": "verify"}

    if repro:
        if not os.path.isfile(repro):
            lines.append(f"REPRO missing: {repro} (write it first; outside the repository)")
            entry["repro"] = "MISSING"
        else:
            code, tail = run_repro(root, repro, min(30, remaining() / 2))
            entry["repro"] = "PASS" if code == 0 else "FAIL"
            lines.append(f"REPRO {entry['repro']} (exit {code}): {tail.strip()[-600:]}")
    else:
        entry["repro"] = "NONE"
        lines.append("REPRO not given: pass --repro /tmp/repro_issue.py that exits 0 only when fixed.")

    raw = args.get("tests", "")
    targets = shlex.split(raw) if raw else select_tests(root, sources)
    entry["new_failures"] = 0
    new_failed, test_output = {}, ""
    if targets and remaining() > 3:
        result = run_pytest(root, targets, "", remaining() - 1)
        entry["tests_ran"] = True
        test_output = result["output"]
        lines.append(f"TESTS {' '.join(targets)}: {result['summary']}")
        if result["failed"] and remaining() > 3:
            with tempfile.TemporaryDirectory(prefix="swe-baseline-") as clean:
                clean_baseline(root, clean)
                nodes = [node for node in result["failed"] if Path(clean, node.split("::")[0]).is_file()]
                before = run_pytest(clean, nodes, "", remaining() - 1) if nodes else {"failed": {}, "timed_out": False}
                new_failed = {node: value for node, value in result["failed"].items()
                              if node not in before["failed"] and not before["timed_out"]}
        elif result["timed_out"]:
            new_failed = {"TIMEOUT": ("ERROR", "test run timed out")}
        for node, (kind, message) in list(new_failed.items())[:5]:
            lines.append(f"  NEW FAILURE {node}: {message[:200]}")
        if result["failed"] and not new_failed:
            lines.append("  (all failures also fail before your change)")
    else:
        entry["tests_ran"] = False
        lines.append("TESTS none selected (no changed source, or no matching test files)")
    crash, assertion = split_failures(new_failed, test_output)
    entry.update({"new_failures": len(new_failed), "crash": crash, "assert_fail": assertion})

    passed = entry["repro"] == "PASS" and entry["changed"]
    submit = passed and crash == 0 and assertion <= MAX_ASSERTION_FAILURES
    index = record(root, entry)
    best = best_checkpoint(root)
    green = entry["repro"] in {"PASS", "NONE"} and entry["new_failures"] == 0 and entry["changed"]
    lines.append(f"CHECKPOINT #{index}: repro={entry['repro']} new_failures={entry['new_failures']}"
                 + (f" ({crash} crash, {assertion} assertion)" if entry["new_failures"] else "")
                 + (" GREEN" if green or submit else "")
                 + (f"; best so far is #{best['index']}" if best else ""))
    clock = guard.clock_line(root) if armed else ""
    if submit and not entry["new_failures"]:
        lines.append(f"VERDICT: SUBMIT {clock}. The fix is verified. Run finalize.py next, then submit_patch. "
                     "Do not keep editing.")
    elif submit:
        lines.append(f"VERDICT: SUBMIT {clock}. The reproduction passes and the only new failures are assertions in "
                     "tests that pinned the old behavior this change is meant to alter. Do not undo the fix and do "
                     "not rewrite those tests: run finalize.py, then submit_patch.")
    elif passed:
        lines.append(f"VERDICT: FIX {clock}. The reproduction passes but the change breaks other behavior "
                     "(crashes, or many failing tests). Keep the part that fixes the issue and repair the rest; "
                     "the passing state is saved, so finalize.py can fall back to it.")
    elif entry["repro"] == "FAIL" and not entry["changed"]:
        lines.append(f"VERDICT: BUG REPRODUCED {clock}. The script fails on the unmodified code; now edit the cause.")
    elif entry["repro"] == "PASS" and not entry["changed"]:
        lines.append(f"VERDICT: REPRO TOO WEAK {clock}. It passes WITHOUT any change, so it does not detect the bug. "
                     "Make it check the exact behavior the issue expects and exit 1 when it is wrong.")
    elif entry["repro"] == "FAIL":
        passing = best and best.get("repro") == "PASS"
        lines.append(f"VERDICT: NOT FIXED {clock}. The reproduction still fails."
                     + (f" Checkpoint #{best['index']} passed; finalize.py restores it." if passing else ""))
    if armed:
        lines += guard.deadline_check(root)
    print("\n".join(lines)[-3000:])
    return 0 if green or submit else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
