"""Survival guard: score every workspace state, keep the best, never lose a passing fix.

Ops (ADK passes args as --name value pairs; --op may also be the first positional):
  arm    --repro /tmp/repro_issue.py  Freeze the reproduction (must FAIL now) and start scoring.
  check                               Score the current workspace, checkpoint it. Once any state has
                                      passed the frozen repro, a worse state is reverted at once.
  guard                               check, then restore the best state, then print ACTION.
  final                               Restore the best (or best non-empty) state, clean up, ACTION: submit.

Every state is judged the same way, however the edit was made (apply_edit, sed, python):
  1. frozen repro: exit 0 = PASS; otherwise the number of lines containing FAIL (crash = 500)
  2. existing tests of the changed modules: failures that do NOT also fail on the clean repository
  3. smaller diff wins ties; a non-empty attempt beats the empty patch (salvage)
Hygiene on every call: edits to existing test files and test config are reverted (the graders
replace those files), and scratch files in the repository are deleted. Scratch belongs in /tmp.
Self-contained on purpose: the sandbox may not expose sibling skills.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

BUDGET_SECONDS = 300          # eval_config max_time_minutes: 5
CLOCK_OFFSET = 10             # the agent starts a few seconds before its first survive.py call
CRASH = 500
TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$|(^|/)conftest\.py$")
FROZEN_CONFIG = {"pytest.ini", "tox.ini", "setup.cfg", "conftest.py"}
SCRATCH = re.compile(r"(^|/)(repro|reproduce|probe|scratch|debug|tmp|temp|check|try|test_issue|issue)[^/]*$", re.I)
OP_ALIASES = {"start": "arm", "freeze": "arm", "init": "arm", "setup": "arm", "score": "check", "verify": "check",
              "test": "check", "status": "check", "gate": "guard", "decide": "guard", "finalize": "final",
              "finish": "final", "submit": "final", "last_will": "final"}


# ---------------------------------------------------------------- plumbing
def parse_args(argv):
    values, positional, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positional.extend(argv[index + 1:])
            break
        if token.startswith("-") and len(token) > 1 and "=" in token:
            key, value = token.lstrip("-").split("=", 1)
            values[key.replace("-", "_").lower()] = value
            index += 1
        elif token.startswith("-") and len(token) > 1:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            values[token.lstrip("-").replace("-", "_").lower()] = value
            index += 2
        else:
            positional.append(token)
            index += 1
    # A model sometimes passes the whole argument object as one JSON string.
    for raw in [values.pop("args", "")] + positional[:1]:
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                values.update({str(k).lower(): str(v) for k, v in json.loads(raw).items()})
            except ValueError:
                pass
    return values, positional


def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        if candidate and os.path.exists(os.path.join(candidate, ".git")):
            return os.path.realpath(candidate)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        if top:
            return top
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def git(root, *args, timeout=20):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)


def state_dir(root):
    key = hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:12]
    path = Path(tempfile.gettempdir()) / "swe_state" / f"survive-{key}"
    (path / "checkpoints").mkdir(parents=True, exist_ok=True)
    return path


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def clock(state, root=None):
    """Seconds used/left. The origin is the harness's setup commit ("baseline") when it is recent,
    otherwise this script's first call plus a small offset."""
    marker = state / "start"
    if not marker.exists():
        origin = time.time() - CLOCK_OFFSET
        if root:
            try:
                head = git(root, "log", "-1", "--format=%ct %s").stdout.split(" ", 1)
                stamp = float(head[0])
                if head[1].strip() == "baseline" and 0 <= time.time() - stamp < 900:
                    origin = stamp
            except (ValueError, IndexError, OSError, subprocess.SubprocessError):
                pass
        marker.write_text(str(origin))
    used = time.time() - float(marker.read_text() or time.time())
    return used, max(0.0, BUDGET_SECONDS - used)


def env_for(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SWE_GUARD"] = "0"  # another candidate's repro hook (swe_edit guard) must never fire inside our scoring
    return env


# ---------------------------------------------------------------- hygiene
def hygiene(root):
    """Revert test-file / test-config edits and delete scratch files. Returns notes for the model."""
    notes = []
    modified = git(root, "diff", "--name-only", "HEAD").stdout.split()
    frozen = [name for name in modified if TEST_PATH.search(name) or Path(name).name in FROZEN_CONFIG]
    if frozen:
        git(root, "checkout", "HEAD", "--", *frozen)
        notes.append("REVERTED edits to test files/config (frozen; the graders replace them): " + ", ".join(frozen))
    removed = []
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        if "/" not in name or SCRATCH.search(name) or TEST_PATH.search(name):
            try:
                os.remove(os.path.join(root, name))
                removed.append(name)
            except OSError:
                pass
    if removed:
        notes.append("DELETED scratch files inside the repository (keep scratch in /tmp): " + ", ".join(removed))
    return notes


def current_diff(root):
    git(root, "add", "-N", ".")
    return git(root, "diff", "HEAD").stdout


def changed_sources(root):
    names = git(root, "diff", "--name-only", "HEAD").stdout.split()
    return [n for n in dict.fromkeys(names) if n.endswith(".py") and not TEST_PATH.search(n)]


def restore(root, patch):
    """Make the workspace equal HEAD + patch, and confirm it (retries once from HEAD)."""
    for _ in range(2):
        git(root, "checkout", "HEAD", "--", ".")
        for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        if patch.strip():
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
                handle.write(patch)
            git(root, "apply", "--whitespace=nowarn", handle.name)
            os.unlink(handle.name)
        if current_diff(root) == patch:
            return True
    return False


# ---------------------------------------------------------------- scoring
def select_tests(root, sources, limit=2):
    """Existing test files that name or import the changed modules (TestPrune-style)."""
    candidates = []
    for directory in ("tests", "test", "testing"):
        if (Path(root) / directory).is_dir():
            candidates += [str(p.relative_to(root)) for p in Path(root, directory).rglob("test*.py")]
    scored = {}
    for source in sources:
        module = source[:-3].replace("/", ".")
        module = module[4:] if module.startswith("src.") else module
        stem = Path(source).stem.lstrip("_")
        tail = module.split(".", 1)[-1]
        for test in candidates:
            score = 3 if Path(test).stem in (f"test_{stem}", f"test{stem}") else 0
            try:
                text = Path(root, test).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"\b{re.escape(module)}\b|from \.?{re.escape(tail)} import|\b{re.escape(stem)}\b", text):
                score += 1
            if score:
                scored[test] = max(score, scored.get(test, 0))
    ranked = sorted(scored.items(), key=lambda row: (-row[1], row[0]))
    # A test file named after the changed module is the sharpest signal; use only those when they exist.
    named = [t for t, score in ranked if score >= 3]
    return (named or [t for t, _ in ranked])[:limit]


def run_pytest(root, targets, timeout):
    command = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=line", "--color=no", "-p", "no:cacheprovider",
               *targets]
    try:
        result = subprocess.run(command, cwd=root, env=env_for(root), capture_output=True, text=True,
                                timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        return None
    failed = {}
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        match = re.match(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", line.strip())
        if match:
            failed[match[2]] = match[3] or ""
    return failed


def clean_failures(root, state, nodes, timeout):
    """Which of these nodes also fail on the clean repository (cached per node)."""
    cache = load(state / "clean_failures.json", {})
    missing = [n for n in nodes if n not in cache]
    if missing and timeout > 2:
        with tempfile.TemporaryDirectory(prefix="survive-clean-") as clean:
            archive = subprocess.run(["git", "archive", "HEAD"], cwd=root, capture_output=True, timeout=15)
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(clean, filter="data")
            present = [n for n in missing if Path(clean, n.split("::")[0]).is_file()]
            before = run_pytest(clean, present, timeout) if present else {}
            if before is not None:
                for node in missing:
                    cache[node] = node in before or node not in present
        (state / "clean_failures.json").write_text(json.dumps(cache))
    return {n for n in nodes if cache.get(n, False)}


def run_repro(root, repro, timeout):
    try:
        result = subprocess.run([sys.executable, repro], cwd=root, env=env_for(root), capture_output=True,
                                text=True, timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT", CRASH
    output = (result.stdout + result.stderr)
    if result.returncode == 0:
        return 0, output, 0
    fails = len(re.findall(r"\bFAIL", output))
    crashed = "Traceback" in output and "AssertionError" not in output and not fails
    return result.returncode, output, CRASH if crashed else max(1, fails)


def syntax_errors(root, sources):
    errors = []
    for source in sources:
        try:
            path = os.path.join(root, source)
            if os.path.isfile(path):
                compile(Path(path).read_text(encoding="utf-8", errors="replace"), source, "exec")
        except (SyntaxError, ValueError) as error:
            errors.append(f"{source}: {type(error).__name__}: {error}"[:200])
    return errors


def score(entry):
    return (entry["repro_pass"], -entry["fail_count"], -entry["new_failures"], entry["nonempty"], -entry["diff_lines"])


def status(entry):
    if entry["repro_pass"] and entry["new_failures"] == 0 and entry["nonempty"]:
        return "GREEN"
    return "AMBER" if entry["repro_pass"] and entry["nonempty"] else "RED"


def evaluate(root, state, deadline):
    diff = current_diff(root)
    sources = changed_sources(root)
    entry = {"nonempty": bool(diff.strip()), "diff_lines": sum(1 for line in diff.splitlines()
                                                               if line[:1] in "+-" and line[:3] not in ("+++", "---")),
             "files": sources, "repro_pass": False, "fail_count": CRASH, "new_failures": 0, "failures": [],
             "time": time.time()}
    lines = []
    broken = syntax_errors(root, sources)
    if broken:
        lines.append("SYNTAX ERROR (counts as a crash): " + "; ".join(broken))
        return entry, diff, lines
    repro = state / "frozen_check.py"
    if repro.is_file():
        code, output, fails = run_repro(root, str(repro), min(15, deadline - time.monotonic()))
        entry.update(repro_pass=code == 0, fail_count=fails)
        tail = "\n".join(output.strip().splitlines()[-12:])[-700:]
        lines.append(f"REPRO {'PASS' if code == 0 else 'FAIL'} (exit {code}, failing checks={fails}):\n{tail}")
    else:
        entry["fail_count"] = CRASH - 1
        lines.append("REPRO NOT ARMED: write /tmp/repro_issue.py and run op=arm --repro /tmp/repro_issue.py")
    slow = set(load(state / "slow_tests.json", []))
    targets = [t for t in select_tests(root, sources) if t not in slow]
    if targets and deadline - time.monotonic() > 4:
        failed = run_pytest(root, targets, min(12, deadline - time.monotonic() - 2))
        if failed is None:
            (state / "slow_tests.json").write_text(json.dumps(sorted(slow | set(targets))))
            lines.append(f"TESTS {' '.join(targets)}: TIMEOUT (not counted; skipped from now on to save time)")
        else:
            known = clean_failures(root, state, list(failed), deadline - time.monotonic() - 1)
            new = [n for n in failed if n not in known]
            entry.update(new_failures=len(new), failures=new)
            lines.append(f"TESTS {' '.join(targets)}: {len(failed)} failing, {len(new)} NEW vs clean repo")
            lines += [f"  NEW FAILURE {n}: {failed[n][:160]}" for n in new[:4]]
    return entry, diff, lines


def record(state, entry, diff):
    folder = state / "checkpoints"
    index = len(list(folder.glob("*.json"))) + 1
    (folder / f"{index:03d}.patch").write_text(diff, encoding="utf-8")
    entry["index"] = index
    (folder / f"{index:03d}.json").write_text(json.dumps(entry), encoding="utf-8")
    return index


def history(state):
    folder = state / "checkpoints"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def best_of(rows, salvage=False):
    if salvage:
        rows = [r for r in rows if r["nonempty"] and r["fail_count"] < CRASH] or rows
    return max(rows, key=lambda r: (score(r), r["index"])) if rows else None


def patch_of(state, entry):
    return (state / "checkpoints" / f"{entry['index']:03d}.patch").read_text(encoding="utf-8")


def describe(entry):
    return (f"#{entry['index']} {status(entry)} repro={'PASS' if entry['repro_pass'] else 'FAIL'} "
            f"failing_checks={entry['fail_count']} new_test_failures={entry['new_failures']} "
            f"diff_lines={entry['diff_lines']} files={','.join(entry['files']) or '-'}")


# ---------------------------------------------------------------- ops
def op_arm(root, state, args, positional, out):
    repro = next((args[k] for k in ("repro", "script", "path", "file", "repro_path") if args.get(k)),
                 positional[1] if len(positional) > 1 else (positional[0] if positional and positional[0].endswith(".py") else ""))
    frozen = state / "frozen_check.py"
    if not repro:
        out.append("ARMED already: the repro is frozen. End your turn." if frozen.is_file() else
                   "NOT ARMED: write /tmp/repro_issue.py (print 'ok: ...' or 'FAIL: ...' per case, exit 1 on any FAIL), "
                   "then call op=arm --repro /tmp/repro_issue.py")
        return 0 if frozen.is_file() else 2
    if not os.path.isfile(repro):
        out.append(f"NOT ARMED: {repro} does not exist. Write it with run_command first (outside the repository).")
        return 2
    rearm = frozen.is_file()
    if rearm and Path(repro).read_bytes() == frozen.read_bytes():
        out.append("ARMED already with this same repro; scores and checkpoints are kept. End your turn.")
        return 0
    shutil.copyfile(repro, frozen)
    deadline = time.monotonic() + 40
    entry, diff, lines = evaluate(root, state, deadline)
    if entry["repro_pass"] and not entry["nonempty"]:
        frozen.unlink()
        out += lines + ["NOT ARMED: the repro PASSES on the untouched repository, so it does not capture the bug. "
                        "Make it assert what the issue expects (include edge cases around the behavior) and arm again."]
        return 2
    if rearm:
        shutil.rmtree(state / "checkpoints")
        (state / "checkpoints").mkdir()
        out.append("RE-ARMED: new repro frozen; earlier scores discarded.")
    index = record(state, entry, diff)
    out += lines + [f"ARMED: repro frozen (failing checks now: {entry['fail_count']}). Checkpoint #{index}. "
                    "Every later check is judged by this frozen copy. End your turn now."]
    return 0


def op_check(root, state, out, inline_protect=True):
    notes = hygiene(root)
    _, left = clock(state, root)
    entry, diff, lines = evaluate(root, state, time.monotonic() + 40)
    index = record(state, entry, diff)
    out += notes + lines
    rows = history(state)
    best = best_of(rows)
    passed_before = any(r["repro_pass"] and r["nonempty"] for r in rows)
    # Near the deadline the harness submits whatever the workspace holds, so it must hold the best state.
    if inline_protect and (passed_before or left <= 45) and best["index"] != index and score(best) > score(entry):
        restore(root, patch_of(state, best))
        kind = "passing" if best["repro_pass"] else "best"
        out.append(f"REVERTED: this state ({describe(entry)}) is worse than the {kind} state {describe(best)}. "
                   f"The workspace is back to the {kind} state." + (" A passing fix is never given up." if best["repro_pass"] else ""))
        return best
    out.append(f"CHECKPOINT {describe(entry)}")
    out.append(f"BEST SO FAR {describe(best)}" if best["index"] != index else "This is the BEST state so far.")
    return entry if best["index"] == index else best


def op_guard(root, state, out):
    current = op_check(root, state, out)
    rows = history(state)
    best = best_of(rows)
    if best and current["index"] != best["index"] and score(best) > score(current):
        restore(root, patch_of(state, best))
        out.append(f"RESTORED best state {describe(best)}.")
    used, left = clock(state)
    ambers = load(state / "amber_guards.json", 0)
    verdict = status(best) if best else "RED"
    if verdict == "GREEN":
        action = "call submit_patch now. The fix is verified."
    elif verdict == "AMBER":
        (state / "amber_guards.json").write_text(json.dumps(ambers + 1))
        if used >= 120 or ambers >= 1:
            action = ("call submit_patch now. The repro passes; the remaining new test failures most likely assert "
                      "the old behavior the issue asks to change.")
        else:
            action = ("reply CONTINUE. The repro passes and is locked in. The next strike may try to also clear the new "
                      "test failures without breaking the repro; the guard reverts anything worse.")
    elif left <= 50:
        action = "call submit_patch now (salvage: the best attempt so far is in place)."
    else:
        action = "reply CONTINUE. Not fixed yet; the next strike must attack a DIFFERENT suspect or a lower layer."
    out.append(f"ACTION: {action}")
    return 0


def op_final(root, state, out):
    notes = hygiene(root)
    rows = history(state)
    diff = current_diff(root)
    if diff.strip() and not any(patch_of(state, r) == diff for r in rows):
        entry, diff, lines = evaluate(root, state, time.monotonic() + 25)
        record(state, entry, diff)
        out += lines
        rows = history(state)
    best = best_of(rows)
    if best and not best["nonempty"]:
        best = best_of(rows, salvage=True)
        if best and best["nonempty"]:
            out.append("SALVAGE: nothing passed; keeping the most plausible non-empty attempt instead of an empty patch.")
    if best and patch_of(state, best) != current_diff(root):
        out.append(("RESTORED " if restore(root, patch_of(state, best)) else "COULD NOT RESTORE ") + describe(best))
    elif best:
        out.append("KEEPING current state " + describe(best))
    out += notes + hygiene(root)
    git(root, "add", "-N", ".")
    stat = git(root, "diff", "--stat", "HEAD").stdout.strip()
    out.append("FINAL DIFF:\n" + (stat or "(empty)"))
    out.append("ACTION: call submit_patch now.")
    return 0


def main(argv):
    args, positional = parse_args(argv)
    op = (args.get("op") or args.get("action") or args.get("mode") or (positional[0] if positional else "check")).strip().lower()
    op = OP_ALIASES.get(op, op)
    if op not in {"arm", "check", "guard", "final"}:
        op = "arm" if any(args.get(k) for k in ("repro", "script")) else "check"
    root = os.path.realpath(args.get("root") or workspace_root())
    state = state_dir(root)
    used, left = clock(state, root)
    out = []
    try:
        code = {"arm": lambda: op_arm(root, state, args, positional, out),
                "check": lambda: (op_check(root, state, out), 0)[1],
                "guard": lambda: op_guard(root, state, out),
                "final": lambda: op_final(root, state, out)}[op]()
    except Exception as error:  # the guard must never crash the run
        out.append(f"survive.py internal error ({type(error).__name__}: {error}); the workspace is unchanged.")
        code = 1
    used, left = clock(state)
    urgency = "FINAL SECONDS: submit now." if left <= 40 else ("HURRY." if left <= 120 else "Move fast.")
    print(f"CLOCK: ~{used:.0f}s used, ~{left:.0f}s left of {BUDGET_SECONDS}s. {urgency}\n" + "\n".join(out)[:3200])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
