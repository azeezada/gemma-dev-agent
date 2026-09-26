"""Isolated candidate checkouts so parallel agents never edit the same files.

Every call names the candidate with --id (1, 2, 3 ...). Operations (--op):
  create                      make (or reuse) the candidate checkout (every other op also does this)
  repro  --file /tmp/x.py     register the candidate's reproduction; runs it on the clean code (must fail)
  run    --cmd "..."          run a shell command inside the candidate checkout (30 s)
  read   --file F [--start N --end M | --symbol NAME]   numbered lines from the checkout (or a /tmp file)
  edit   --file F --old TEXT --new TEXT  one exact replacement; then reruns the registered repro and,
                              when it passes, verifies, checkpoints and adopts the candidate if it is the best so far
  verify [--repro PATH]       reproduction + existing tests of changed modules, in the checkout
  diff                        the candidate's patch
  judge  [--ids 1,2,3] [--repro PATH]  cross-check every candidate patch against every reproduction, adopt the best
  adopt                       apply the candidate's patch to the real workspace

Arguments are forgiving: `command`/`cmd`, `start_line`/`start`, `path`/`filepath`/`file`, positional
forms and a missing --op are all understood. Solvers have a shared deadline (SOLVER_SECONDS after the
first call of the run); after it, work ops answer TIME UP so the judge always gets its turn.
"""
import ast
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Candidate checkouts are guarded by this script's own checkpoints; keep swe_edit's workspace guard
# (hooks/watchdog acting on /workspace) out of calls made from here.
os.environ.setdefault("SWE_GUARD", "0")

from apply_edit import parse_args, single_edit, workspace_root  # noqa: E402
from finalize import SCRATCH  # noqa: E402
from verify import changed_sources, current_diff, run_repro, select_tests, state_dir  # noqa: E402
from run_tests import clean_baseline, run_pytest  # noqa: E402

FORBIDDEN = re.compile(r"\b(pip|pip3|uv)\s+install\b|\b(curl|wget|ssh)\b|/kaggle/input|\.\./")
SOLVER_SECONDS = 200
JUDGE_SECONDS = 45
WORK_OPS = {"create", "repro", "run", "read", "edit"}
ALIASES = {
    "op": ("op", "operation", "action", "mode"),
    "id": ("id", "candidate", "candidate_id", "cid", "solver", "solver_id", "n"),
    "cmd": ("cmd", "command", "commands", "shell", "bash", "script_cmd"),
    "file": ("file", "path", "filepath", "file_path", "filename", "target_file"),
    "start": ("start", "start_line", "line", "from", "first", "offset", "line_start"),
    "end": ("end", "end_line", "to", "last", "line_end"),
    "old": ("old", "old_string", "old_text", "search", "find"),
    "new": ("new", "new_string", "new_text", "replace", "replacement"),
    "repro": ("repro", "repro_file", "repro_path", "reproduction", "script"),
    "ids": ("ids", "candidates", "candidate_ids"),
    "symbol": ("symbol", "function", "func", "name", "def"),
}
OP_ALIASES = {"exec": "run", "shell": "run", "bash": "run", "command": "run", "view": "read", "cat": "read",
              "show": "read", "open": "read", "replace": "edit", "apply": "edit", "apply_edit": "edit",
              "test": "verify", "check": "verify", "patch": "diff", "status": "diff", "init": "create",
              "new": "create", "select": "judge", "choose": "judge", "register": "repro", "reproduce": "repro"}
BROKEN_REPRO = re.compile(r"\b(SyntaxError|IndentationError|ModuleNotFoundError|ImportError|NameError|FileNotFoundError)\b")


def normalize(argv):
    """Canonical args from whatever shape the model sent."""
    raw, positional = parse_args(argv)
    for nested in ("args", "arguments", "params"):
        # A dict nested one level too deep arrives as its Python/JSON repr.
        if nested in raw:
            try:
                inner = ast.literal_eval(raw[nested])
            except (ValueError, SyntaxError):
                inner = None
            if isinstance(inner, dict):
                raw.pop(nested)
                raw = {**{str(k): str(v) for k, v in inner.items()}, **raw}
    if "positional_args" in raw:
        value = raw.pop("positional_args")
        try:
            parsed = ast.literal_eval(value)
            positional = [str(v) for v in (parsed if isinstance(parsed, (list, tuple)) else [parsed])] + positional
        except (ValueError, SyntaxError):
            positional = [value] + positional
    args = {}
    for canonical, names in ALIASES.items():
        for name in names:
            if name in raw and str(raw[name]).strip() != "":
                args[canonical] = str(raw[name])
                break
    op = OP_ALIASES.get(args.get("op", "").strip().lower(), args.get("op", "").strip().lower())
    if op not in {"create", "repro", "run", "read", "edit", "verify", "diff", "judge", "adopt"}:
        # Infer the operation from the other arguments.
        op = ("edit" if "old" in args else "run" if "cmd" in args else "read" if "file" in args or "symbol" in args
              else "judge" if "ids" in args else "verify" if "repro" in args else op)
    if positional:
        if op == "run" and "cmd" not in args:
            args["cmd"] = positional[0] if len(positional) == 1 else shlex.join(positional)
        elif op in {"read", "repro", "edit"} and "file" not in args:
            args["file"] = positional[0]
    args["op"] = op
    if "id" in args:
        args["id"] = re.sub(r"[^A-Za-z0-9_-]", "", args["id"])[:12]
    return args


@contextlib.contextmanager
def locked(main_root):
    with open(state_dir(main_root) / "wt.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load(main_root, name, default):
    path = state_dir(main_root) / name
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(main_root, name, value):
    path = state_dir(main_root) / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def elapsed(main_root):
    with locked(main_root):
        run = load(main_root, "wt_run.json", {})
        if "start" not in run:
            run["start"] = time.time()
            save(main_root, "wt_run.json", run)
    return time.time() - run["start"]


def checkout(main_root, ident):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", ident or ""):
        raise ValueError('give your candidate id, e.g. args={"op": "read", "id": "1", ...}')
    path = state_dir(main_root) / "wt" / ident
    if not (path / ".git").exists():
        with locked(main_root):
            if not (path / ".git").exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "worktree", "prune"], cwd=main_root, capture_output=True, timeout=20)
                made = subprocess.run(["git", "worktree", "add", "--detach", str(path), "HEAD"],
                                      cwd=main_root, capture_output=True, text=True, timeout=60)
                if made.returncode:
                    raise RuntimeError(made.stderr[-400:])
    return str(path)


def env_for(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def candidate_patch(root):
    diff = current_diff(root)
    # Keep only files that belong to the fix; drop scratch files at the checkout root.
    blocks = re.split(r"(?m)^(?=diff --git )", diff)
    kept = []
    for block in blocks:
        match = re.match(r"diff --git a/(\S+)", block)
        if match and ("/" not in match[1] or SCRATCH.search(match[1])) and "new file mode" in block:
            continue
        kept.append(block)
    return "".join(kept)


def patch_lines(patch):
    body = [line for line in patch.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return sum(line.startswith("+") for line in body), len(body)


def patch_key(patch):
    """Normalized content for voting: which lines are removed/added in which file, ignoring whitespace."""
    items, current = [], ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
        elif line[:1] in "+-" and not line.startswith(("+++", "---")) and line[1:].strip():
            items.append((current, line[0], re.sub(r"\s+", "", line[1:])))
    return hashlib.sha1(json.dumps(sorted(items)).encode()).hexdigest()


def classify(result):
    """Split test failures into crash-type (hard) and assertion-type (soft: may encode the old behavior)."""
    hard, soft = [], []
    for node, (kind, message) in result["failed"].items():
        text = (message or "").strip()
        if kind == "ERROR" or (text and not re.match(r"(AssertionError|assert\b|Failed:|DID NOT RAISE)", text)):
            hard.append(node)
        else:
            soft.append(node)
    return hard, soft


def run_tests_vs_clean(root, timeout):
    """New failures of the existing tests for the modules this checkout changed, split hard/soft."""
    targets = select_tests(root, changed_sources(root))
    if not targets or timeout < 3:
        return {"tests": "none selected" if not targets else "skipped (time)", "hard": [], "soft": []}
    started = time.monotonic()
    result = run_pytest(root, targets, "", timeout)
    if result["timed_out"]:
        return {"tests": result["summary"], "hard": ["TIMEOUT"], "soft": []}
    new = dict(result["failed"])
    if new:
        with tempfile.TemporaryDirectory(prefix="swe-baseline-") as clean:
            clean_baseline(root, clean)
            nodes = [node for node in new if Path(clean, node.split("::")[0]).is_file()]
            left = timeout - (time.monotonic() - started)
            before = run_pytest(clean, nodes, "", left) if nodes and left > 1 else {"failed": {}}
        new = {node: value for node, value in new.items() if node not in before["failed"]}
    hard, soft = classify({"failed": new})
    return {"tests": result["summary"], "hard": hard, "soft": soft}


def repro_status(root, repro, timeout=20):
    code, tail = run_repro(root, repro, timeout)
    return ("PASS" if code == 0 else "FAIL"), tail.strip()


def board_key(row, patch):
    added, total = patch_lines(patch)
    return [int(row["repro"] == "PASS"), -len(row["hard"]), -added, -total, -len(row["soft"])]


def adopt_patch(main_root, patch):
    """Make /workspace hold exactly this patch (plus nothing scratch)."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
        handle.write(patch)
    try:
        subprocess.run(["git", "checkout", "--", "."], cwd=main_root, capture_output=True, timeout=20)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=main_root,
                                   capture_output=True, text=True, timeout=20).stdout.split()
        for name in untracked:
            if "/" not in name or SCRATCH.search(name):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(main_root, name))
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", handle.name], cwd=main_root,
                                 capture_output=True, text=True, timeout=20)
    finally:
        os.unlink(handle.name)
    return applied.returncode == 0, applied.stderr[-400:]


def checkpoint_and_maybe_adopt(main_root, ident, root, row):
    """Record the candidate's verified state; adopt it into /workspace when it beats the adopted one."""
    patch = candidate_patch(root)
    if not patch.strip():
        return "no changes to checkpoint"
    key = board_key(row, patch)
    with locked(main_root):
        points = load(main_root, f"wt_points_{ident}.json", [])
        points.append({"key": key, "patch": patch, "repro": row["repro"]})
        save(main_root, f"wt_points_{ident}.json", points[-6:])
        board = load(main_root, "wt_board.json", {})
        # An unverified edit is adopted only while the workspace holds nothing; any passing one replaces it.
        if board and board.get("key") >= key:
            return f"checkpoint saved; workspace keeps candidate {board.get('id')} (scored at least as well)"
        ok, error = adopt_patch(main_root, patch)
        if not ok:
            return "checkpoint saved; adopting failed: " + error
        save(main_root, "wt_board.json", {"id": ident, "key": key})
    return ("checkpoint saved and ADOPTED into the workspace (best so far)" if row["repro"] == "PASS"
            else "checkpoint saved (reproduction still fails)")


def evaluate_candidate(main_root, ident, root, repro, budget):
    started = time.monotonic()
    row = {"repro": "NONE", "tail": "", "hard": [], "soft": [], "tests": ""}
    if repro:
        row["repro"], row["tail"] = repro_status(root, repro, min(20, budget / 2))
    row.update(run_tests_vs_clean(root, budget - (time.monotonic() - started)))
    return row


def describe(row):
    parts = [f"repro={row['repro']}"]
    parts.append(f"tests: {row['tests']}")
    if row["hard"]:
        parts.append("CRASH-TYPE NEW FAILURES (likely a real regression): " + ", ".join(row["hard"][:3]))
    if row["soft"]:
        parts.append("assertion-type new failures (may be tests of the old behavior the issue changes): "
                     + ", ".join(n[:90] for n in row["soft"][:3]))
    return "; ".join(parts)


def registered_repro(main_root, ident):
    """The candidate's own reproduction, else any other solver's (a shared check beats none)."""
    repros = load(main_root, "wt_repros.json", {})
    return repros.get(ident) or next(iter(repros.values()), "")


def op_repro(main_root, ident, root, args):
    path = args.get("file") or args.get("repro") or ""
    if not path or not os.path.isfile(path):
        return 2, (f"ERROR: repro file not found: {path or '(none)'}. Write it first with run_command "
                   "(cat > /tmp/repro_<id>.py <<'PY' ... PY), then pass its path as file.")
    clean = checkout(main_root, "base")
    status, tail = repro_status(clean, path)
    with locked(main_root):
        repros = load(main_root, "wt_repros.json", {})
        repros[ident] = path
        save(main_root, "wt_repros.json", repros)
    lines = [f"registered {path} as candidate {ident}'s reproduction.",
             f"ON THE CLEAN CODE: {status}\n{tail[-700:]}"]
    if status == "PASS":
        lines.append("WARNING: it passes on the unfixed code, so it does not show the bug. Make its checks assert the "
                     "behavior the issue expects (exit 1 when wrong), rewrite it and register again.")
    elif BROKEN_REPRO.search(tail):
        lines.append("WARNING: the script itself looks broken (import/name/syntax error). Fix the script, not the repo.")
    else:
        lines.append("Good: it fails before the fix. Now read the suspect function (op=read with symbol) and edit.")
    return 0, "\n".join(lines)


def op_read(root, args):
    target = args.get("file", "")
    if target.startswith("/workspace/"):
        target = target[len("/workspace/"):]
    real_tmp = os.path.realpath(tempfile.gettempdir())
    if os.path.isabs(target) and os.path.realpath(target).startswith(real_tmp + os.sep) and os.path.isfile(target):
        path = Path(target)
    else:
        path = Path(root, target.lstrip("/")).resolve()
        if not str(path).startswith(os.path.realpath(root) + os.sep) or not path.is_file():
            return 2, "ERROR: --file must be a repository file (e.g. pkg/module.py) or a /tmp script"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    symbol = args.get("symbol", "").split(".")[-1].split(":")[-1].strip("() ")
    try:
        start = max(1, int(float(args.get("start", 1) or 1)))
    except ValueError:
        start = 1
    if symbol:
        found = next((i for i, line in enumerate(lines, 1)
                      if re.match(rf"\s*(async\s+def|def|class)\s+{re.escape(symbol)}\b", line)), None)
        if found is None:
            return 1, f"ERROR: no def/class {symbol} in {target}"
        indent = len(lines[found - 1]) - len(lines[found - 1].lstrip())
        stop = found
        while stop < len(lines) and (not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip()) > indent):
            stop += 1
        start, end = found, min(stop, found + 149)
    else:
        try:
            end = int(float(args.get("end", start + 79) or start + 79))
        except ValueError:
            end = start + 79
        end = min(len(lines), end, start + 149)
    return 0, "\n".join(f"{number:>5}: {lines[number - 1]}" for number in range(start, end + 1))


def op_edit(main_root, ident, root, args, left):
    os.environ["SWE_WORKSPACE"] = root
    rest = [part for key in ("file", "old", "new") if key in args for part in ("--" + key, args[key])]
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = single_edit(rest)
    out = captured.getvalue().strip()
    if code:
        return code, out
    repro = registered_repro(main_root, ident)
    if not repro:
        return 0, out + "\nNo reproduction registered: run op=repro with your /tmp script so edits are checked."
    row = evaluate_candidate(main_root, ident, root, repro, max(5, min(40, left)))
    note = checkpoint_and_maybe_adopt(main_root, ident, root, row)
    verdict = ("GREEN: your reproduction passes. Stop editing and answer with your CANDIDATE line."
               if row["repro"] == "PASS" and not row["hard"] else
               "Reproduction passes but crash-type test failures appeared; fix them or stop." if row["repro"] == "PASS"
               else "Reproduction still fails: " + row["tail"][-400:])
    return 0, f"{out[:1500]}\nCHECK {describe(row)}\n{note}\n{verdict}"


def judge(main_root, args):
    """Cross-check every candidate patch against every discriminating reproduction and adopt the best."""
    started = time.monotonic()
    left = lambda: JUDGE_SECONDS - (time.monotonic() - started)
    folder = state_dir(main_root) / "wt"
    ids = [item for item in re.split(r"[,\s]+", args.get("ids", "")) if item] or sorted(
        p.name for p in folder.iterdir() if p.is_dir() and p.name not in {"base", "judge"}) if folder.is_dir() else []
    repros = dict(load(main_root, "wt_repros.json", {}))
    if args.get("repro"):
        repros.setdefault("given", args["repro"])
    # Candidate patches: current state and best checkpoint of each solver.
    patches = []
    for ident in ids:
        if not (folder / ident / ".git").exists():
            continue
        seen = set()
        current = candidate_patch(str(folder / ident))
        points = sorted(load(main_root, f"wt_points_{ident}.json", []), key=lambda p: p["key"], reverse=True)
        for label, patch in [(ident, current)] + [(f"{ident}@best", p["patch"]) for p in points[:1]]:
            if patch.strip() and patch not in seen:
                seen.add(patch)
                patches.append((label, patch))
    if not patches:
        board = load(main_root, "wt_board.json", {})
        return 0, ("No candidate produced a patch." + (f" Workspace holds candidate {board['id']}." if board else "")
                   + " Call submit_patch now.")
    base = checkout(main_root, "base")
    useful = {}
    for owner, path in repros.items():
        if os.path.isfile(path):
            status, tail = repro_status(base, path, 15)
            if status == "FAIL" and not BROKEN_REPRO.search(tail):
                useful[owner] = path
    work = checkout(main_root, "judge")
    votes = {}
    for _, patch in patches:
        votes[patch_key(patch)] = votes.get(patch_key(patch), 0) + 1
    rows = []
    for label, patch in patches:
        subprocess.run(["git", "checkout", "--", "."], cwd=work, capture_output=True, timeout=20)
        subprocess.run(["git", "clean", "-fdq"], cwd=work, capture_output=True, timeout=20)
        ok, _ = adopt_patch(work, patch)
        if not ok:
            continue
        passed = [owner for owner, path in useful.items()
                  if left() > 5 and repro_status(work, path, 15)[0] == "PASS"]
        tests = run_tests_vs_clean(work, min(25, left() - 3)) if left() > 8 else {"tests": "skipped (time)", "hard": [], "soft": []}
        added, total = patch_lines(patch)
        key = (len(passed), -len(tests["hard"]), votes[patch_key(patch)], -added, -total, -len(tests["soft"]))
        rows.append((key, label, patch, passed, tests))
    if not rows:
        return 1, "ERROR: no candidate patch applied cleanly."
    rows.sort(key=lambda row: row[0], reverse=True)
    lines = [f"{len(useful)} reproduction(s) fail on the clean code and are used as checks: "
             + (", ".join(f"{o}:{p}" for o, p in useful.items()) or "none")]
    for key, label, patch, passed, tests in rows:
        lines.append(f"candidate {label}: passes {len(passed)}/{len(useful)} repros; crash-type new failures "
                     f"{len(tests['hard'])}; identical patches {key[2]}; +{-key[3]} lines; assertion-type new "
                     f"failures {len(tests['soft'])} ({tests['tests']})")
    best = rows[0]
    ok, error = adopt_patch(main_root, best[2])
    if ok:
        save(main_root, "wt_board.json", {"id": best[1], "key": list(best[0]), "judged": True})
        lines.append(f"RECOMMENDED: adopt candidate {best[1]}")
        lines.append(f"ADOPTED candidate {best[1]} into the workspace. Next: call submit_patch.")
    else:
        lines.append("ERROR adopting the best candidate: " + error)
    return 0, "\n".join(lines)


def main(argv):
    args = normalize(argv)
    op = args["op"]
    main_root = os.path.realpath(workspace_root())
    try:
        spent = elapsed(main_root)
        if op == "judge":
            code, text = judge(main_root, args)
            print(text[:3500])
            return code
        ident = args.get("id", "")
        left = SOLVER_SECONDS - spent
        if op in WORK_OPS and load(main_root, "wt_board.json", {}).get("judged"):
            # A root re-run after a nudge restarts the solvers; the judge already decided, so don't redo work.
            print("DONE: the judge already chose the final patch. Answer with your CANDIDATE line (text only).")
            return 0
        if op in WORK_OPS and left <= 0:
            print(f"TIME UP ({spent:.0f}s used): the solver deadline passed. Stop now and answer with your "
                  "CANDIDATE line (text only, no tool call). Your best verified state is already saved.")
            return 0
        root = checkout(main_root, ident)
        with locked(main_root):
            calls = load(main_root, "wt_calls.json", {})
            calls[ident] = calls.get(ident, 0) + 1
            save(main_root, "wt_calls.json", calls)
        footer = f"\n[{spent:.0f}s of {SOLVER_SECONDS}s used; call {calls[ident]} by candidate {ident}]"
        code, text = 0, ""
        if op == "create":
            text = f"candidate {ident} checkout ready (clean repository)."
        elif op == "repro":
            code, text = op_repro(main_root, ident, root, args)
        elif op == "run":
            command = args.get("cmd", "")
            if not command.strip() or FORBIDDEN.search(command):
                code, text = 2, 'ERROR: give "cmd" with a shell command; no installs, network, or paths outside the checkout'
            else:
                done = subprocess.run(["/bin/bash", "-c", command], cwd=root, env=env_for(root),
                                      capture_output=True, text=True, timeout=30)
                text = f"exit {done.returncode}\n{(done.stdout + done.stderr)[-2500:]}"
        elif op == "read":
            code, text = op_read(root, args)
        elif op == "edit":
            if not args.get("file") or "old" not in args or "new" not in args:
                code, text = 2, 'ERROR: edit needs "file", "old" (exact current lines) and "new"'
            else:
                code, text = op_edit(main_root, ident, root, args, left)
        elif op == "verify":
            repro = args.get("repro") or registered_repro(main_root, ident)
            row = evaluate_candidate(main_root, ident, root, repro, 40)
            note = checkpoint_and_maybe_adopt(main_root, ident, root, row)
            text = f"candidate {ident}: {describe(row)}\n{row['tail'][-500:]}\n{note}"
        elif op == "diff":
            text = candidate_patch(root)[:3000] or "(no changes)"
        elif op == "adopt":
            patch = candidate_patch(root)
            if not patch.strip():
                code, text = 1, "ERROR: candidate has no changes to adopt"
            else:
                ok, error = adopt_patch(main_root, patch)
                stat = subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=main_root,
                                      capture_output=True, text=True, timeout=20).stdout
                code, text = (0, f"Adopted candidate {ident} into the workspace:\n{stat}\nNext: call submit_patch.") \
                    if ok else (1, "ERROR: patch did not apply: " + error)
        else:
            code, text = 2, "ERROR: op must be create, repro, run, read, edit, verify, diff, judge or adopt"
        print(text[:3500] + footer)
        return code
    except subprocess.TimeoutExpired:
        print("ERROR: command exceeded its time limit")
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
