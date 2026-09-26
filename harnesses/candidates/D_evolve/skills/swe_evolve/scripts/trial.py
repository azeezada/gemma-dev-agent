"""Evaluate every candidate in /tmp/d_cands against the spec and the existing tests; keep the best applied.

args: {}   (no arguments)
Each candidate is applied to the ORIGINAL code, then scored:
  1. spec (/tmp/d_spec.py) exit 0, then PASS minus FAIL checks
  2. regressions: newly failing tests outside the changed module's own test file
  3. touches the focused function
  4. newly failing tests in the changed module's own test file (these often pin the old behavior the issue changes)
  5. smaller diff
The best candidate stays applied in the workspace, so the repository always holds the best patch found so far.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

from common import (CANDS, SPEC, apply_blocks, digest, elapsed, head_text, load_state, parse_candidate, python_env,
                    run_spec, save_state, spec_bug, unified, workspace_root)

BUDGET = 45  # seconds for the whole evaluation


def select_tests(root, files, limit=3):
    dirs = [d for d in ("tests", "test", "testing") if (Path(root) / d).is_dir()]
    tests = sorted({str(p.relative_to(root)) for d in dirs for p in Path(root, d).rglob("test*.py")})
    own, related = [], []
    for rel in files:
        stem = Path(rel).stem.lstrip("_")
        module = rel[:-3].replace("/", ".").removeprefix("src.")
        own += [t for t in tests if Path(t).stem in (f"test_{stem}", f"test{stem}", f"test_{stem}s")]
        for test in tests:
            try:
                text = Path(root, test).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"\b{re.escape(module)}\b", text):
                related.append(test)
    ordered = list(dict.fromkeys(own + related))[:limit]
    return ordered, set(own)


def run_tests(root, tests, timeout):
    if not tests or timeout < 2:
        return None
    command = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=no", "--color=no", "-p", "no:cacheprovider", *tests]
    try:
        done = subprocess.run(command, cwd=root, env=python_env(root), capture_output=True, text=True, timeout=timeout)
        output = done.stdout + "\n" + done.stderr
    except subprocess.TimeoutExpired:
        return None
    failed = {}
    for line in output.splitlines():
        match = re.match(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", line.strip())
        if match:
            failed[match[2]] = (match[3] or match[1])[:200]
    return failed


def restore(root, files):
    for rel in files:
        (Path(root) / rel).write_text(head_text(root, rel), encoding="utf-8")


def rank_key(row):
    spec = row.get("spec") or {}
    if row.get("error"):
        return (-1, -99, -99, 0, -99, 0)
    return (1 if spec.get("exit") == 0 else 0, spec.get("passes", 0) - spec.get("fails", 0),
            -len(row.get("regressions", [])), 1 if row.get("primary") else 0,
            -len(row.get("own_changes", [])), -row.get("size", 0))


def label(row):
    if row.get("error"):
        return "BROKEN"
    if (row.get("spec") or {}).get("exit") != 0:
        return "RED"
    if row.get("tests_skipped"):
        return "AMBER?"
    if row.get("regressions"):
        return "ORANGE"
    return "AMBER" if row.get("own_changes") else "GREEN"


def evaluate(root, state, cid, text, deadline):
    row = {"id": cid, "hash": digest(text)}
    try:
        blocks = parse_candidate(text)
        changes = apply_blocks(root, blocks)
    except (ValueError, SyntaxError) as error:
        row["error"] = str(error)[:400]
        return row
    focus = [f.split("::") for f in state.get("focus", [])[:1]]
    row["primary"] = any(b["path"].removeprefix("./") == p and (b.get("name") or "") == n for p, n in focus for b in blocks) \
        or any(b["kind"] == "replace" and b["path"].removeprefix("./") == p for p, _ in focus for b in blocks)
    diff = unified(root, changes)
    row["size"] = sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))
    row["files"] = sorted(changes)
    row["targets"] = [f"{b['path']}::{b['name']}" if b.get("name") else b["path"] for b in blocks]
    if not row["size"]:
        row["error"] = "candidate makes no change to the original code"
        return row
    touched = set(state.get("touched", [])) | set(changes)
    state["touched"] = sorted(touched)
    try:
        for rel, new in changes.items():
            (Path(root) / rel).write_text(new, encoding="utf-8")
        row["spec"] = run_spec(root, timeout=max(2, min(10, deadline - time.time())))
        if row["spec"]["exit"] == 0:
            tests, own = select_tests(root, changes)
            key = "|".join(tests)
            baseline = state.setdefault("baseline_tests", {}).get(key)
            if baseline is None:
                restore(root, changes)
                baseline = run_tests(root, tests, deadline - time.time())
                for rel, new in changes.items():
                    (Path(root) / rel).write_text(new, encoding="utf-8")
                if baseline is not None:
                    state["baseline_tests"][key] = baseline
            failed = run_tests(root, tests, deadline - time.time()) if baseline is not None else None
            if failed is None:
                row["tests_skipped"] = True
            else:
                new_failures = {t: m for t, m in failed.items() if t not in baseline}
                row["tests"] = tests
                row["own_changes"] = [f"{t} - {m}" for t, m in new_failures.items() if t.split("::")[0] in own]
                row["regressions"] = [f"{t} - {m}" for t, m in new_failures.items() if t.split("::")[0] not in own]
    finally:
        restore(root, changes)
    return row


def report(state, rows, verbose=True, fresh=None):
    ranked = sorted(rows, key=rank_key, reverse=True)
    lines = []
    if ranked:
        best = ranked[0]
        solved = label(best) in ("GREEN", "AMBER")
        lines.append(f"STATUS: {'SOLVED' if solved else 'NOT SOLVED'} (best {best['id']} = {label(best)}; "
                     f"elapsed {elapsed(state):.0f}s since first swe_evolve call)")
    lines.append("rank  id      label   spec(pass/fail)  regress  own-test-changes  size  targets")
    for row in ranked:
        spec = row.get("spec") or {}
        lines.append(f"      {row['id']:<7} {label(row):<7} {'exit0' if spec.get('exit') == 0 else 'fails'} "
                     f"{spec.get('passes', 0)}/{spec.get('fails', 0):<10} {len(row.get('regressions', [])):<8} "
                     f"{len(row.get('own_changes', [])):<17} {row.get('size', 0):<5} {', '.join(row.get('targets', []))}")
    if verbose:
        shown = ranked[:3] + [row for row in ranked[3:] if row.get("error") and row["id"] in (fresh or ())]
        for row in shown:
            details = []
            if row.get("error"):
                details.append(f"  error: {row['error']}")
            details += [f"  {line}" for line in (row.get("spec") or {}).get("fail_lines", [])[:5]]
            if (row.get("spec") or {}).get("exit") not in (0, None) and not (row.get("spec") or {}).get("fail_lines"):
                details.append("  spec output: " + (row.get("spec") or {}).get("tail", "")[-600:].replace("\n", "\n    "))
            details += [f"  REGRESSION {t}" for t in row.get("regressions", [])[:4]]
            details += [f"  own-test change (may pin old behavior) {t}" for t in row.get("own_changes", [])[:4]]
            if details:
                lines.append(f"-- {row['id']}:")
                lines += details
    lines.append("Meaning: GREEN = spec passes, no test changes. AMBER = spec passes; only the changed module's own tests "
                 "differ (usually tests pinning the old behavior the issue changes): accepted. ORANGE = other tests broke. "
                 "RED = spec still fails. BROKEN = candidate could not be applied.")
    return "\n".join(lines)


def main(argv):
    started = time.time()
    deadline = started + BUDGET
    root = workspace_root()
    state = load_state(root)
    results = state.setdefault("results", {})
    restore(root, [rel for rel in state.get("applied_files", []) if rel])
    evaluated, fresh = 0, set()
    for path in sorted(CANDS.glob("*.py")) if CANDS.is_dir() else []:
        cid = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        if cid in results and results[cid].get("hash") == digest(text):
            continue
        if time.time() > deadline - 3:
            print(f"(time: {cid} not evaluated yet; run trial.py again)")
            continue
        results[cid] = evaluate(root, state, cid, text, deadline)
        evaluated += 1
        fresh.add(cid)
    rows = list(results.values())
    ranked = sorted(rows, key=rank_key, reverse=True)
    applied = []
    baseline = state.get("baseline_spec") or {}
    if ranked and not ranked[0].get("error"):
        best, spec = ranked[0], ranked[0].get("spec") or {}
        improves = spec.get("exit") == 0 or (spec.get("passes", 0) - spec.get("fails", 0)
                                              > baseline.get("passes", 0) - baseline.get("fails", 0))
        if not SPEC.is_file() or spec_bug(baseline):
            # No usable spec: the best applicable candidate on the focused function is still better than nothing.
            improves = bool(best.get("primary")) or len(ranked) == 1
        if improves:
            changes = apply_blocks(root, parse_candidate((CANDS / f"{best['id']}.py").read_text(encoding="utf-8")))
            for rel, new in changes.items():
                (Path(root) / rel).write_text(new, encoding="utf-8")
            applied = sorted(changes)
            state["best"] = best["id"]
    state["applied_files"] = applied
    text = report(state, rows, fresh=fresh)
    state["report"] = text
    save_state(state)
    print(f"evaluated {evaluated} new candidate(s) in {time.time() - started:.1f}s; "
          f"{'best applied to workspace: ' + state.get('best', '') if applied else 'workspace left at original code'}")
    print(text)
    if not rows:
        print("No candidates found in /tmp/d_cands/.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
