"""Guard layer: keep the best verified state in the workspace, whatever the agent does next.

Used by verify.py, apply_edit.py and finalize.py; not called by the model directly.

- arm(root)        idempotent. Copies these scripts to the per-workspace state dir, installs a small
                   .pth hook into the Python that runs the workspace's commands, and starts one detached
                   watchdog. Called by the first swe_edit script that runs in the real workspace.
- hook             `python /tmp/repro*.py` in an armed workspace (through run_command, too) runs the script,
                   records a checkpoint with its exit code, and prints one [guard] line after the output.
                   A passing state is therefore saved even when the edit came from sed or a one-liner.
- settle(root)     records the current state, ranks every checkpoint (choose), restores the winner and deletes
                   scratch files. finalize.py calls it; the watchdog calls it shortly before the time budget
                   ends, so the harness's git-diff fallback captures the best state rather than the last one.

Ranking (choose): only states whose reproduction passed; crash-type new test failures (errors, TypeError, ...)
rank below assertion-type ones, which are expected when a fix changes behavior an old test pinned; the
agent's current state wins ties; then fewer assertion failures, smaller diff, later checkpoint.

Deadline salvage happens inside calls the agent makes (apply_edit, verify, finalize, the repro hook):
once less than DEADLINE_MARGIN seconds remain, the best passing checkpoint is restored and the output
says so. The detached watchdog is OFF by default (SWE_GUARD_WATCHDOG=1 enables it for ablations only;
whether a process can outlive an official run_skill_script call is not documented).

SWE_GUARD=0 disables everything (unit tests); SWE_GUARD_BUDGET sets the time budget in seconds (default 300).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPRO_NAME = re.compile(r"^(repro|reproduce)", re.I)
SCRATCH = re.compile(r"(^|/)(repro|reproduce|probe|scratch|debug|tmp|temp|check|try|test_issue|issue)[^/]*$", re.I)
ASSERTION = re.compile(r"^(AssertionError|assert\b|Failed: DID NOT|AssertionError:)", re.I)
MAX_ASSERTION_FAILURES = 3
DEADLINE_MARGIN = 45


def enabled():
    return os.environ.get("SWE_GUARD", "1") != "0"


def state_base():
    return Path(tempfile.gettempdir()) / "swe_state"


def state_dir(root):
    """Per-checkout scratch area outside the repository (never part of the patch)."""
    key = hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:12]
    path = state_base() / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def git(root, *args, timeout=20):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)


def current_diff(root):
    git(root, "add", "-N", ".")
    return git(root, "diff", "HEAD").stdout


def diff_hash(diff):
    return hashlib.sha1(diff.encode()).hexdigest()[:16]


def diff_lines(diff):
    return sum(1 for line in diff.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def diff_sources(diff):
    names = re.findall(r"^\+\+\+ b/(\S+)", diff, re.M)
    return [name for name in dict.fromkeys(names)
            if name.endswith(".py") and not re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$", name)]


# ---------------------------------------------------------------- checkpoints

def checkpoint_dir(root):
    path = state_dir(root) / "checkpoints"
    path.mkdir(exist_ok=True)
    return path


def record(root, entry):
    """Store entry (with a "diff" key) as the next checkpoint; returns its index."""
    directory = checkpoint_dir(root)
    diff = entry.pop("diff")
    entry.setdefault("hash", diff_hash(diff))
    entry.setdefault("lines", diff_lines(diff))
    entry.setdefault("time", time.time())
    indexes = [int(p.stem) for p in directory.glob("*.json") if p.stem.isdigit()]
    index = max(indexes, default=0) + 1
    (directory / f"{index:03d}.patch").write_text(diff, encoding="utf-8")
    entry["index"] = index
    (directory / f"{index:03d}.json").write_text(json.dumps(entry), encoding="utf-8")
    return index


def load(root):
    directory = state_dir(root) / "checkpoints"
    rows = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            row = json.loads(path.read_text())
            if "hash" not in row:  # checkpoints written by an older verify.py
                patch = (directory / f"{path.stem}.patch").read_text(encoding="utf-8")
                row["hash"], row["lines"] = diff_hash(patch), diff_lines(patch)
            rows.append(row)
        except (OSError, ValueError):
            continue
    return rows


def patch_text(root, row):
    return (state_dir(root) / "checkpoints" / f"{row['index']:03d}.patch").read_text(encoding="utf-8")


def update(root, row):
    path = state_dir(root) / "checkpoints" / f"{row['index']:03d}.json"
    path.write_text(json.dumps(row), encoding="utf-8")


# ---------------------------------------------------------------- test classification

def exception_line(output, node):
    """First `E   ...` line of the node's --tb=short section; fallback when the summary has no message."""
    name = node.split("::")[-1].split("[")[0]
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^_{3,} .*\b%s\b.* _{3,}$" % re.escape(name), line):
            for follow in lines[index + 1:index + 200]:
                if re.match(r"^_{3,} ", follow):
                    break
                if follow.startswith("E "):
                    return follow[1:].strip()
    return ""


def is_assertion(kind, message, output="", node=""):
    if kind == "ERROR":
        return False
    message = message.strip() or exception_line(output, node)
    return bool(ASSERTION.search(message)) or not message  # no evidence of a crash: count as assertion


def classify(failed_new, output=""):
    """failed_new: {node: (FAILED|ERROR, message)} -> (crash_count, assertion_count), by exception type."""
    crash = sum(1 for node, (kind, message) in failed_new.items() if not is_assertion(kind, message, output, node))
    return crash, len(failed_new) - crash


def evaluate_patch(root, diff, deadline):
    """Run the tests of the modules a patch changes, in a clean copy, and count new failures by kind."""
    from run_tests import clean_baseline, run_pytest
    from verify import select_tests
    sources = diff_sources(diff)
    if not sources:
        return {"crash": 0, "assert_fail": 0, "new_failures": 0, "tests_ran": False}
    with tempfile.TemporaryDirectory(prefix="swe-eval-") as work:
        clean_baseline(root, work)
        targets = select_tests(work, sources)
        if not targets:
            return {"crash": 0, "assert_fail": 0, "new_failures": 0, "tests_ran": False}
        before = run_pytest(work, targets, "", max(1, min(40, deadline - time.monotonic())))
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
            handle.write(diff)
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", handle.name], cwd=work,
                                 capture_output=True, text=True, timeout=20)
        os.unlink(handle.name)
        if applied.returncode:
            return None
        after = run_pytest(work, targets, "", max(1, min(40, deadline - time.monotonic())))
    if after["timed_out"] or before["timed_out"]:
        return None
    new = {node: value for node, value in after["failed"].items() if node not in before["failed"]}
    crash, assertion = classify(new, after["output"])
    return {"crash": crash, "assert_fail": assertion, "new_failures": len(new), "tests_ran": True}


# ---------------------------------------------------------------- ranking and settling

def known(row):
    return row.get("crash") is not None and row.get("assert_fail") is not None


def choose(rows, current_hash):
    """Best repro-passing checkpoint (see module docstring), or None."""
    passing = {}
    for row in rows:  # one entry per distinct diff; the latest record wins, test results are merged
        if row.get("repro") != "PASS" or not row.get("changed"):
            continue
        merged = dict(passing.get(row["hash"], {}))
        merged.update({k: v for k, v in row.items() if v is not None})
        passing[row["hash"]] = merged
    if not passing:
        return None

    def key(row):
        crash = row.get("crash") if known(row) else 0
        assertion = row.get("assert_fail") if known(row) else MAX_ASSERTION_FAILURES + 1
        return (crash, assertion)

    best = min(passing.values(), key=lambda row: (*key(row), row.get("lines", 0), -row["index"]))
    current = passing.get(current_hash)
    if current and key(current) <= key(best):
        return current
    return best


def repro_path(root):
    armed = read_armed(root)
    path = armed.get("repro") if armed else None
    return path if path and os.path.isfile(path) else None


def run_repro(root, repro, timeout):
    env = dict(os.environ, SWE_GUARD_CHILD="1", PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root)])
    try:
        result = subprocess.run([sys.executable, repro], cwd=root, env=env, capture_output=True,
                                text=True, timeout=max(1, timeout))
        return result.returncode, (result.stdout + result.stderr)[-900:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def restore(root, diff, keep=()):
    git(root, "reset", "-q")  # drop intent-to-add entries so new files can be re-created by the patch
    git(root, "checkout", "--", ".")
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        if name not in keep:
            os.remove(os.path.join(root, name))
    if not diff.strip():
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
        handle.write(diff)
    applied = git(root, "apply", "--whitespace=nowarn", handle.name)
    os.unlink(handle.name)
    return applied.stderr[:300] if applied.returncode else None


def remove_scratch(root, keep=()):
    removed = []
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        if name in keep:
            continue
        if "/" not in name or SCRATCH.search(name):
            os.remove(os.path.join(root, name))
            removed.append(name)
    return removed


def settle(root, seconds=20.0, keep=(), reason="finalize"):
    """Record the current state, restore the best passing checkpoint if it differs; returns report lines."""
    deadline = time.monotonic() + seconds
    lines = []
    diff = current_diff(root)
    rows = load(root)
    current = diff_hash(diff)
    repro = repro_path(root)
    if diff.strip() and repro and not any(row["hash"] == current and row.get("repro") in {"PASS", "FAIL"}
                                          for row in rows):
        code, _ = run_repro(root, repro, min(15, max(1, deadline - time.monotonic() - 2)))
        record(root, {"changed": True, "diff": diff, "repro": "PASS" if code == 0 else "FAIL",
                      "source": reason, "crash": None, "assert_fail": None})
        rows = load(root)
    # Fill in missing test results, current state first, then newest.
    for row in sorted((r for r in rows if r.get("repro") == "PASS" and r.get("changed") and not known(r)),
                      key=lambda r: (r["hash"] != current, -r["index"])):
        if deadline - time.monotonic() < 4:
            break
        same = next((r for r in rows if r["hash"] == row["hash"] and known(r)), None)
        result = {k: same[k] for k in ("crash", "assert_fail", "new_failures", "tests_ran")} if same else \
            evaluate_patch(root, patch_text(root, row), deadline - 1)
        row.update(result or {"eval_failed": True})
        update(root, row)
    best = choose(load(root), current)
    if best is None:
        lines.append("No checkpoint where the reproduction passed; keeping the current state.")
    elif best["hash"] != current:
        error = restore(root, patch_text(root, best), keep)
        if error:
            lines.append("WARNING: could not restore checkpoint #%d: %s" % (best["index"], error))
        else:
            lines.append("Restored checkpoint #%d (repro PASS, new test failures: %s); the later state was worse "
                         "or unverified." % (best["index"], describe_failures(best)))
    else:
        lines.append("Keeping the current state: it is the best checkpoint (#%d, repro PASS, new test failures: %s)."
                     % (best["index"], describe_failures(best)))
    removed = remove_scratch(root, keep)
    if removed:
        lines.append("Removed scratch files: " + ", ".join(removed))
    return lines


def describe_failures(row):
    if not known(row):
        return "not measured"
    return "%d crash, %d assertion" % (row["crash"], row["assert_fail"])


# ---------------------------------------------------------------- arming, clock, hook, watchdog

def read_armed(root):
    try:
        return json.loads((state_dir(root) / "armed.json").read_text())
    except (OSError, ValueError):
        return {}


def write_armed(root, data):
    path = state_dir(root) / "armed.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def budget_seconds():
    try:
        return float(os.environ.get("SWE_GUARD_BUDGET", "300"))
    except ValueError:
        return 300.0


def clock_origin(root):
    """The harness commits the workspace ("baseline") right before the agent starts; use that time."""
    try:
        committed = int(git(root, "log", "-1", "--format=%ct", "HEAD").stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        committed = 0
    now = time.time()
    return committed if 0 <= now - committed <= 900 else now - 15


def clock_line(root):
    armed = read_armed(root)
    if not armed:
        return ""
    return "[t≈%ds of %ds]" % (time.time() - armed["origin"], armed["budget"])


HOOK = r'''
def _swe_guard_hook():
    import os, sys, re, hashlib
    if os.environ.get("SWE_GUARD_CHILD") or os.environ.get("SWE_GUARD") == "0":
        return
    argv = list(getattr(sys, "argv", None) or [])
    if not argv or not argv[0].endswith(".py") or not re.match(r"(repro|reproduce)", os.path.basename(argv[0]), re.I):
        return
    base = BASE
    root = os.getcwd()
    while root != os.path.dirname(root) and not os.path.exists(os.path.join(root, ".git")):
        root = os.path.dirname(root)
    if not os.path.exists(os.path.join(root, ".git")):
        try:
            root = open(os.path.join(base, "last_root")).read().strip()
        except OSError:
            return
    state = os.path.join(base, hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:12])
    if not os.path.exists(os.path.join(state, "armed.json")):
        return
    import subprocess
    code = subprocess.call([sys.executable, *argv], env=dict(os.environ, SWE_GUARD_CHILD="1"))
    try:
        sys.stdout.flush()
        sys.path.insert(0, os.path.join(state, "bin"))
        import guard
        print(guard.on_repro_exit(root, os.path.abspath(argv[0]), code), flush=True)
    except Exception:
        pass
    os._exit(code)
_swe_guard_hook()
'''


def pth_line(hook_path):
    body = "try:\n p=%r\n if os.path.exists(p): exec(compile(open(p).read(), p, 'exec'), {'__name__': 'swe_guard_hook'})\nexcept Exception:\n pass" % str(hook_path)
    return "import os; exec(%r)\n" % body


def site_dirs():
    dirs = []
    probe = "import site, sys; print('\\n'.join(site.getsitepackages()))"
    for python in dict.fromkeys([sys.executable, shutil.which("python3") or "", shutil.which("python") or ""]):
        if not python:
            continue
        try:
            out = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        dirs += [line.strip() for line in out.splitlines() if line.strip()]
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def install_hook():
    base = state_base()
    base.mkdir(parents=True, exist_ok=True)
    hook = base / "hook.py"
    hook.write_text(HOOK.replace("BASE", repr(str(base))), encoding="utf-8")
    installed = []
    for directory in site_dirs():
        try:
            Path(directory, "swe_guard.pth").write_text(pth_line(hook), encoding="utf-8")
            installed.append(directory)
        except OSError:
            continue
    return installed


def arm(root, repro=None):
    """Idempotent. Returns True when the guard is active for root."""
    if not enabled():
        return False
    try:
        armed = read_armed(root)
        if repro and os.path.isfile(repro) and armed.get("repro") != os.path.abspath(repro):
            armed["repro"] = os.path.abspath(repro)
            if armed.get("origin"):
                write_armed(root, armed)
        if armed.get("origin"):
            return True
        bin_dir = state_dir(root) / "bin"
        bin_dir.mkdir(exist_ok=True)
        here = Path(__file__).resolve().parent
        for script in here.glob("*.py"):
            shutil.copy2(script, bin_dir / script.name)
        armed.update({"root": os.path.realpath(root), "origin": clock_origin(root), "budget": budget_seconds(),
                      "armed_at": time.time()})
        armed["hook_sites"] = install_hook()
        (state_base() / "last_root").write_text(os.path.realpath(root))
        if os.environ.get("SWE_GUARD_WATCHDOG", "0") == "1":
            process = subprocess.Popen([sys.executable, str(bin_dir / "guard.py"), "watch", os.path.realpath(root)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            armed["watchdog_pid"] = process.pid
        write_armed(root, armed)
        return True
    except Exception:  # the guard must never break the script that armed it
        return False


def deadline_check(root):
    """Inside an agent's call: when time is nearly up, put the best passing state back. Returns output lines."""
    armed = read_armed(root)
    if not armed or time.time() < armed["origin"] + armed["budget"] - DEADLINE_MARGIN:
        return []
    if not any(row.get("repro") == "PASS" and row.get("changed") for row in load(root)):
        return []
    lines = settle(root, seconds=12, reason="deadline")
    return ["[guard] %s time is almost up. %s Call finalize.py, then submit_patch now." % (clock_line(root), " ".join(lines))]


def on_repro_exit(root, repro, code):
    """Called by the hook after a reproduction script ran in an armed workspace."""
    armed = read_armed(root)
    if armed.get("repro") != repro:
        armed["repro"] = repro
        write_armed(root, armed)
    diff = current_diff(root)
    clock = clock_line(root)
    if not diff.strip():
        if code == 0:
            return ("[guard] %s this script exits 0 on the UNMODIFIED code, so it does not detect the bug yet. "
                    "Make it check the behavior the issue expects and exit 1 when it is wrong." % clock)
        return "[guard] %s bug reproduced on the unmodified code (exit %d)." % (clock, code)
    rows = load(root)
    passed_before = [row for row in rows if row.get("repro") == "PASS" and row.get("changed")]
    index = record(root, {"changed": True, "diff": diff, "repro": "PASS" if code == 0 else "FAIL",
                          "source": "hook", "crash": None, "assert_fail": None})
    late = deadline_check(root)
    if late:
        return late[0]
    if code == 0:
        return ("[guard] %s the reproduction PASSES with your change: saved as checkpoint #%d. Next: verify.py "
                "(existing tests), then finalize.py and submit_patch. Do not extend a passing fix to inputs the "
                "issue does not mention." % (clock, index))
    if passed_before:
        return ("[guard] %s the reproduction FAILS in this state; checkpoint #%d passed. finalize.py restores "
                "the best passing state." % (clock, passed_before[-1]["index"]))
    return "[guard] %s the reproduction still fails (exit %d), checkpoint #%d." % (clock, code, index)


def watch(root):
    """Detached: evaluate new passing checkpoints while idle; settle twice just before the budget ends."""
    armed = read_armed(root)
    if not armed:
        return
    log = state_dir(root) / "watchdog.log"
    shots = [armed["origin"] + armed["budget"] - 28, armed["origin"] + armed["budget"] - 13]
    while shots:
        if not os.path.isdir(root) or not (state_dir(root) / "armed.json").exists():
            return
        if time.time() >= shots[0]:
            shots.pop(0)
            try:
                lines = settle(root, seconds=10, reason="deadline")
            except Exception as error:
                lines = ["error: %r" % error]
            with open(log, "a") as handle:
                handle.write("%.0f %s\n" % (time.time() - armed["origin"], " | ".join(lines)))
            continue
        try:  # use idle time to measure tests of passing checkpoints, so the deadline shot is instant
            for row in load(root):
                if row.get("repro") == "PASS" and row.get("changed") and not known(row) \
                        and not row.get("eval_failed") and time.time() < shots[0] - 12:
                    result = evaluate_patch(root, patch_text(root, row), time.monotonic() + 10)
                    row.update(result or {"eval_failed": True})
                    update(root, row)
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "watch":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        watch(sys.argv[2])
