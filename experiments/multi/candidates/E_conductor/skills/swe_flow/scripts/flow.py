"""swe_flow controller: one command that tells the agent what to do next and does the bookkeeping.

First call (skill):  run_skill_script(skill_name="swe_flow", file_path="scripts/flow.py", args={"issue": "..."})
  -> ranks likely code sites, installs /tmp/flow.py, starts the clock.
Later calls (shell):
  python3 /tmp/flow.py                  check: register /tmp/spec.py, evaluate the current change, say what is next
  python3 /tmp/flow.py show FILE::NAME  numbered source of one function/class and the lines that call it
  python3 /tmp/flow.py put FILE::NAME   replace that function/class with the code in /tmp/new.py, then check
  python3 /tmp/flow.py put FILE:A-B     replace lines A..B with /tmp/new.py, then check

What it enforces (so the agent does not have to remember rules):
- the spec must FAIL on the original code before it counts (a spec that passes proves nothing);
- a call trace shows what the repository's functions received and returned for the spec's inputs;
- every change is compiled, the spec is run, related tests are compared with the original code;
- the first change that makes the spec pass without crash-type test failures is saved and LOCKED:
  later edits are reverted automatically and the agent is told to submit;
- near the time limit the best saved state is restored so the captured diff is the best one;
- scratch files and edits to test files are removed from the workspace diff.
Every call exits 0 and prints at most ~4500 characters (the shell tool truncates at 5000).
"""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_ROOT = Path(os.environ.get("FLOW_STATE_ROOT", "/tmp/swe_state/flow"))
SHIM = Path(os.environ.get("FLOW_SHIM", "/tmp/flow.py"))
SPEC = Path(os.environ.get("FLOW_SPEC", "/tmp/spec.py"))
NEW = Path(os.environ.get("FLOW_NEW", "/tmp/new.py"))
SOFT_DEADLINE = float(os.environ.get("FLOW_SOFT_DEADLINE", "190"))   # seconds after the first call
HARD_DEADLINE = float(os.environ.get("FLOW_HARD_DEADLINE", "235"))
CALL_BUDGET = float(os.environ.get("FLOW_CALL_BUDGET", "24"))         # the shell tool times out at 30 s
OUT_LIMIT = 4500
TEST_DIRS = ("tests", "test", "testing")
SCRATCH = re.compile(r"(^|/)(repro|reproduce|probe|scratch|debug|tmp|check|verify|spec|test_fix|test_issue)[\w.-]*\.py$")
ASSERTION = re.compile(r"^(AssertionError|assert\b|Failed: DID NOT RAISE|AssertionError:)|^$")
_started = time.monotonic()


# ----------------------------------------------------------------------------- basics
def out(text):
    text = str(text)
    if len(text) > OUT_LIMIT:
        text = text[:OUT_LIMIT - 200] + "\n...[cut]...\n" + text[-180:]
    print(text)


def budget_left():
    return CALL_BUDGET - (time.monotonic() - _started)


def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        if candidate and os.path.isdir(os.path.join(candidate, ".git")):
            return Path(os.path.realpath(candidate))
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        if top:
            return Path(os.path.realpath(top))
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def git(root, *args, timeout=20, stdin=None):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, input=stdin)


def state_dir(root):
    key = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    path = STATE_ROOT / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(root):
    path = state_dir(root) / "state.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def save(root, state):
    (state_dir(root) / "state.json").write_text(json.dumps(state, indent=1))


def elapsed(state):
    return time.time() - state.get("t0", time.time())


def clock(state):
    t = elapsed(state)
    tail = " | LOCKED fix saved: call submit_patch" if state.get("locked") else ""
    return "[t={:.0f}s since start, submit by ~{:.0f}s{}]".format(t, HARD_DEADLINE, tail)


def is_test_path(path):
    parts = path.split("/")
    return bool(set(parts[:-1]) & set(TEST_DIRS)) or parts[-1].startswith("test_") or parts[-1] == "conftest.py"


def current_diff(root):
    git(root, "add", "-N", ".")
    return git(root, "diff", "HEAD").stdout


def changed_files(root):
    names = git(root, "diff", "--name-only", "HEAD").stdout.split()
    names += git(root, "ls-files", "--others", "--exclude-standard").stdout.split()
    return list(dict.fromkeys(names))


def env_for(tree):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(tree) / "src"), str(tree)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONSAFEPATH", None)
    return env


def run(cmd, cwd, env, timeout):
    try:
        done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=max(1, timeout))
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "", "TIMEOUT"


# ----------------------------------------------------------------------------- hygiene
def clean_workspace(root):
    """Remove scratch files and revert edits to test files; returns what was removed."""
    removed = []
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        top_level = "/" not in name
        if SCRATCH.search(name) or name.startswith("tmp/") or is_test_path(name) or (top_level and name.endswith(".py")):
            try:
                os.remove(root / name)
                removed.append(name)
            except OSError:
                pass
    for name in git(root, "diff", "--name-only", "HEAD").stdout.split():
        if is_test_path(name):
            git(root, "checkout", "HEAD", "--", name)
            removed.append(name + " (test edit reverted)")
    return removed


def restore(root, patch_text):
    """Make the workspace exactly HEAD + patch_text (index reset too, so intent-to-add files go away)."""
    git(root, "reset", "-q")
    git(root, "checkout", "HEAD", "--", ".")
    for name in git(root, "ls-files", "--others", "--exclude-standard").stdout.split():
        try:
            os.remove(root / name)
        except OSError:
            pass
    if patch_text.strip():
        git(root, "apply", "--whitespace=nowarn", "-", stdin=patch_text if patch_text.endswith("\n") else patch_text + "\n")
    return current_diff(root) == patch_text


def clean_copy(root):
    """The original code (HEAD) for comparisons, extracted once per task."""
    target = state_dir(root) / "original"
    if not (target / ".ok").is_file():
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", "HEAD"], cwd=root, capture_output=True, timeout=60).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
            handle.extractall(target)
        (target / ".ok").write_text("1")
    return target


# ----------------------------------------------------------------------------- spec + trace
def run_spec(tree, timeout=10):
    code, stdout, stderr = run([sys.executable, str(SPEC)], cwd=tree, env=env_for(tree), timeout=timeout)
    text = (stdout + stderr).strip()
    lines = text.splitlines()
    passes = sum(1 for line in lines if re.match(r"\s*(PASS|OK)\b", line))
    fails = sum(1 for line in lines if re.match(r"\s*(FAIL|MISMATCH|WRONG)\b", line))
    broken = bool(re.search(r'File "{}".*\n(.*\n)*?(SyntaxError|NameError|IndentationError|ModuleNotFoundError|ImportError)'
                            .format(re.escape(str(SPEC))), text)) and passes + fails == 0
    return {"exit": code, "passes": passes, "fails": fails, "text": text[-1400:], "broken": broken,
            "ok": code == 0}


def failing_inputs(spec_text):
    """The first quoted value on each FAIL line (the spec template prints the input first)."""
    found = []
    for line in spec_text.splitlines():
        if re.match(r"\s*(FAIL|MISMATCH|WRONG)\b", line):
            match = re.search(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|b'(?:[^'\\]|\\.)*')""", line)
            if match:
                found.append(match.group(1))
    return "\n".join(found)


def trace(tree, focus="", chars=1500):
    env = env_for(tree)
    env["FLOW_ROOT"] = str(tree)
    env["FLOW_FOCUS"] = failing_inputs(focus)
    env["FLOW_TRACE_CHARS"] = str(chars)
    tracer = STATE_ROOT / "bin" / "tracer.py"
    if not tracer.is_file():
        tracer = HERE / "tracer.py"
    code, stdout, stderr = run([sys.executable, str(tracer), str(SPEC)], cwd=tree, env=env,
                               timeout=min(10, max(2, budget_left() - 12)))
    return stdout.strip() or ("(no trace: " + stderr.strip()[-200:] + ")")


# ----------------------------------------------------------------------------- tests
def select_tests(root, sources, limit=2):
    tests = []
    for directory in TEST_DIRS:
        if (root / directory).is_dir():
            tests += [str(p.relative_to(root)) for p in (root / directory).rglob("test*.py")]
    scored = []
    for source in sources:
        module = source[:-3].replace("/", ".")
        module = module[4:] if module.startswith("src.") else module
        stem = Path(source).stem.lstrip("_")
        for test in tests:
            score = 3 if Path(test).stem in ("test_" + stem, "test" + stem) else 0
            try:
                text = (root / test).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"\b{}\b".format(re.escape(module)), text):
                score += 1
            if score:
                scored.append((score, test))
    ordered = sorted(scored, key=lambda r: (-r[0], r[1]))
    named = [t for score, t in ordered if score >= 3]  # tests/test_<module>.py: the focused, fast choice
    return list(dict.fromkeys(named or [t for _, t in ordered]))[:limit]


def pytest_failures(tree, targets, timeout):
    code, stdout, stderr = run([sys.executable, "-m", "pytest", *targets, "-q", "-rfE", "--tb=no",
                                "-p", "no:cacheprovider", "-o", "addopts="], cwd=tree, env=env_for(tree),
                               timeout=timeout)
    if code == 124:
        return None, "timeout"
    failures = {}
    for line in stdout.splitlines():
        match = re.match(r"^(FAILED|ERROR) (.*)$", line)
        if not match:
            continue
        kind, rest = match.groups()
        node, message = rest, ""
        # parametrized ids may contain " - "; split only after the closing bracket of the id
        cut = rest.find(" - ", rest.rfind("]") if "[" in rest.split(" - ")[0] else 0)
        if cut > 0:
            node, message = rest[:cut], rest[cut + 3:]
        failures[node] = (kind, message.strip())
    summary = (stdout.strip().splitlines() or [""])[-1]
    return failures, summary


def compare_tests(root, sources):
    targets = select_tests(root, sources)
    if not targets:
        return {"targets": [], "note": "no existing tests found for the changed files"}
    left = budget_left()
    now, summary = pytest_failures(root, targets, timeout=min(12, left - 8))
    if now is None:
        return {"targets": targets, "note": "tests timed out; not compared"}
    before = {}
    if now:
        before, _ = pytest_failures(clean_copy(root), targets, timeout=min(12, budget_left() - 3))
        before = before or {}
    new = {node: info for node, info in now.items() if node not in before}
    crash = [n for n, (kind, msg) in new.items() if kind == "ERROR" or not ASSERTION.match(msg)]
    assertion = [n for n in new if n not in crash]
    return {"targets": targets, "summary": summary, "crash": crash, "assertion": assertion,
            "messages": {n: new[n][1][:120] for n in new}}


# ----------------------------------------------------------------------------- edits
def parse_target(text):
    text = str(text).strip().strip("`'\"")
    text = re.sub(r"^(/workspace/|\./)", "", text)
    match = re.match(r"^(?P<file>[\w./-]+?\.py)(?:(?:::|:|#L?|\s+)(?P<rest>.+))?$", text)
    if not match:
        return None, None, None
    rest = (match.group("rest") or "").strip().replace("::", ".").rstrip("()")
    lines = re.fullmatch(r"L?(\d+)\s*[-:,]\s*L?(\d+)", rest) or re.fullmatch(r"L?(\d+)", rest)
    if lines:
        start = int(lines.group(1))
        end = int(lines.group(2)) if lines.lastindex and lines.lastindex > 1 else start
        return match.group("file"), None, (start, end)
    return match.group("file"), rest, None


def find_node(source, name):
    """(start_line incl. decorators, end_line, indent) of Class.method / function / class, or None."""
    defs = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + node.name
                defs.append((qualname, node))
                if isinstance(node, ast.ClassDef):
                    walk(node.body, qualname + ".")

    walk(ast.parse(source).body, "")
    name = name.strip(".")
    leaf = name.split(".")[-1]
    for test in (lambda q: q == name, lambda q: q.endswith("." + name), lambda q: q.split(".")[-1] == leaf):
        matches = [node for qualname, node in defs if test(qualname)]
        if matches:
            node = matches[0]
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return start, node.end_lineno, node.col_offset
    return None


def names_in(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node.name)
    return found


def put(root, state, target):
    file, name, span = parse_target(target)
    if not file or not (root / file).is_file():
        return "PUT REFUSED: '{}' is not an existing FILE::NAME or FILE:START-END in the repository.".format(target)
    if not NEW.is_file() or not NEW.read_text().strip():
        return "PUT REFUSED: write the replacement code to {} first (cat > {} <<'EOF' ... EOF).".format(NEW, NEW)
    path = root / file
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    new_code = textwrap.dedent(NEW.read_text(encoding="utf-8").strip("\n")) + "\n"
    if span:
        start, end = span
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip()) if start - 1 < len(lines) else 0
        label = "lines {}-{}".format(start, end)
    else:
        try:
            found = find_node(source, name)
        except SyntaxError as exc:
            return "PUT REFUSED: {} does not parse ({}).".format(file, exc)
        if not found:
            close = [n for n in names_in(source) if name.split(".")[-1].lower() in n.lower()][:8]
            return "PUT REFUSED: no def/class '{}' in {}. Similar names: {}".format(name, file, ", ".join(close) or "-")
        start, end, indent = found
        first = new_code.lstrip().splitlines()[0] if new_code.strip() else ""
        # keep the original decorators when the new code starts at the def itself
        if not first.startswith("@"):
            while start < end and lines[start - 1].lstrip().startswith("@"):
                start += 1
        label = name
    replacement = textwrap.indent(new_code, " " * indent, lambda line: line.strip() != "")
    updated = "".join(lines[:start - 1]) + replacement + "".join(lines[end:])
    try:
        compile(updated, file, "exec")
    except SyntaxError as exc:
        return ("PUT REFUSED: the result would not compile ({} line {}). Nothing was changed. Fix {} and rerun put."
                .format(exc.msg, exc.lineno, NEW))
    path.write_text(updated, encoding="utf-8")
    state.setdefault("edits", 0)
    state["edits"] += 1
    return "PUT OK: replaced {} in {} ({} lines -> {} lines).".format(label, file, end - start + 1,
                                                                    len(replacement.splitlines()))


def show(root, target):
    file, name, span = parse_target(target)
    if not file or not (root / file).is_file():
        return "SHOW: '{}' is not FILE::NAME or FILE:START-END.".format(target)
    source = (root / file).read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    if span:
        start, end = max(1, span[0] - 3), min(len(lines), max(span[1], span[0]) + 3)
    else:
        try:
            found = find_node(source, name)
        except SyntaxError:
            found = None
        if not found:
            return "SHOW: no def/class '{}' in {}. Names there: {}".format(name, file, ", ".join(names_in(source)[:30]))
        start, end = found[0], min(found[1], found[0] + 80)
    body = "\n".join("{:5d}  {}".format(i, lines[i - 1]) for i in range(start, end + 1))
    callers = ""
    if name:
        leaf = name.split(".")[-1]
        grep = git(root, "grep", "-n", "-E", r"(^|[^A-Za-z0-9_]){}\(".format(re.escape(leaf)), "--", "*.py").stdout.splitlines()
        aux = ("docs/", "doc/", "docs_src/", "examples/", "benchmarks/", "scripts/", "tools/")
        uses = [g for g in grep if not is_test_path(g.split(":", 1)[0]) and not g.startswith(aux)
                and not re.search(r"\bdef\s+" + re.escape(leaf), g)]
        if uses:
            callers = "\nCALLED FROM:\n" + "\n".join("  " + u.strip()[:160] for u in uses[:6])
    return "{} {}\n{}{}".format(file, name or "", body, callers)


# ----------------------------------------------------------------------------- evaluation
def checkpoint(root, state, entry):
    directory = state_dir(root) / "checkpoints"
    directory.mkdir(exist_ok=True)
    index = len(state.setdefault("checkpoints", [])) + 1
    (directory / "{:03d}.patch".format(index)).write_text(entry.pop("diff"), encoding="utf-8")
    entry["index"] = index
    state["checkpoints"].append(entry)
    return index


def best(state):
    rows = [c for c in state.get("checkpoints", []) if c.get("compiles", True)]
    if not rows:
        return None
    # green first; then most spec lines fixed; then no crash failures; then smaller diff
    return max(rows, key=lambda c: (c.get("green", False), -c.get("fails", 99), c.get("passes", 0),
                                    -len(c.get("crash", [])), -c.get("size", 1e9)))


def patch_of(root, entry):
    return (state_dir(root) / "checkpoints" / "{:03d}.patch".format(entry["index"])).read_text(encoding="utf-8")


def compiles(root, files):
    for name in files:
        if name.endswith(".py") and (root / name).is_file():
            try:
                compile((root / name).read_text(encoding="utf-8"), name, "exec")
            except SyntaxError as exc:
                return "{} line {}: {}".format(name, exc.lineno, exc.msg)
    return None


def evaluate(root, state, report):
    removed = clean_workspace(root)
    if removed:
        report.append("removed from the workspace (scratch/test edits never belong in the patch): " + ", ".join(removed[:6]))
    diff = current_diff(root)
    if not diff.strip():
        report.append("No source change yet.")
        return
    digest = hashlib.sha256(diff.encode()).hexdigest()
    if digest == state.get("last_eval"):
        report.append("No change since the last check.")
        return
    state["last_eval"] = digest
    files = [f for f in changed_files(root) if not is_test_path(f)]
    error = compiles(root, files)
    if error:
        checkpoint(root, state, {"diff": diff, "compiles": False, "t": elapsed(state)})
        report.append("DOES NOT COMPILE: {}. Fix it (put again) before anything else.".format(error))
        return
    spec = run_spec(root)
    entry = {"diff": diff, "compiles": True, "passes": spec["passes"], "fails": spec["fails"],
             "spec_ok": spec["ok"], "size": len(diff), "t": round(elapsed(state)), "files": files}
    baseline = state.get("spec_clean", {})
    report.append("CHANGED: {}".format(", ".join(files)))
    if not spec["ok"]:
        entry["green"] = False
        checkpoint(root, state, entry)
        report.append("SPEC STILL FAILS with your change (FAIL lines {} -> {}; original code had {}):\n{}".format(
            baseline.get("fails", "?"), spec["fails"], baseline.get("fails", "?"), indent(spec["text"][-900:])))
        report.append("TRACE with your change (what each function received and returned):\n" +
                      indent(trace(root, focus=spec["text"], chars=1300)))
        report.append("NEXT: compare the trace with what the spec expects, find the first call whose result is "
                      "already wrong, and change that function (show it, then put).")
        return
    tests = compare_tests(root, files)
    entry.update({"crash": tests.get("crash", []), "assertion": tests.get("assertion", [])})
    lines = ["SPEC PASSES ({} PASS lines, exit 0).".format(spec["passes"])]
    if tests.get("note"):
        lines.append("Tests: " + tests["note"])
    else:
        lines.append("Existing tests {}: {}".format(" ".join(tests["targets"]), tests.get("summary", "")))
        for node in tests["crash"][:4]:
            lines.append("  NEW CRASH  {}  {}".format(node[:110], tests["messages"].get(node, "")))
        for node in tests["assertion"][:4]:
            lines.append("  changed expectation  {}  {}".format(node[:110], tests["messages"].get(node, "")))
    if tests.get("crash"):
        entry["green"] = False
        checkpoint(root, state, entry)
        lines.append("NOT DONE: your change makes existing tests crash (errors, not changed expectations). "
                     "Keep the fix but repair the crash with one more put.")
        report.append("\n".join(lines))
        return
    entry["green"] = True
    index = checkpoint(root, state, entry)
    state["locked"] = index
    if tests.get("assertion"):
        lines.append("{} existing test(s) assert the OLD behaviour this issue asks to change; that is expected "
                     "and they will be updated by the maintainers' tests. Do not edit tests.".format(len(tests["assertion"])))
    lines.append("GREEN: fix verified and saved as checkpoint #{} and LOCKED.\n"
                 "FINAL STEP: call submit_patch now. Do not edit, refactor or 'improve' anything else: "
                 "edits after this point are reverted automatically.".format(index))
    report.append("\n".join(lines))


def indent(text, prefix="  "):
    return "\n".join(prefix + line for line in str(text).splitlines())


def enforce_lock(root, state, report):
    """After GREEN, the saved fix is the answer; anything else is undone."""
    if not state.get("locked"):
        return False
    entry = next(c for c in state["checkpoints"] if c["index"] == state["locked"])
    saved = patch_of(root, entry)
    clean_workspace(root)
    if current_diff(root) != saved:
        restore(root, saved)
        report.append("The workspace had changed after the verified fix; it was RESTORED to checkpoint #{}."
                      .format(entry["index"]))
    report.append("The verified fix (checkpoint #{}) is in place. Call submit_patch now.".format(entry["index"]))
    return True


def enforce_deadline(root, state, report):
    t = elapsed(state)
    if t < SOFT_DEADLINE:
        return
    if t < HARD_DEADLINE:
        report.append("TIME: {:.0f}s used. Make at most one more change, then submit_patch.".format(t))
        return
    choice = best(state)
    baseline_fails = state.get("spec_clean", {}).get("fails", 99)
    if choice and (choice.get("green") or choice.get("fails", 99) < baseline_fails or choice.get("passes", 0) >
                   state.get("spec_clean", {}).get("passes", 0)):
        restore(root, patch_of(root, choice))
        report.append("TIME IS UP ({:.0f}s). Restored the best saved state (checkpoint #{}: {}).".format(
            t, choice["index"], "verified fix" if choice.get("green") else "partial fix, fewest spec failures"))
    else:
        clean_workspace(root)
        report.append("TIME IS UP ({:.0f}s). No saved state beat the original code; keeping the current change.".format(t))
    report.append("Call submit_patch NOW.")


# ----------------------------------------------------------------------------- spec registration
SPEC_HELP = ("Write it with run_command:\n"
             "  cat > {spec} <<'EOF'\n"
             "  import sys\n"
             "  from <package> import <public API named in the issue>\n"
             "  cases = [(<input>, <expected>), ...]   # the issue's example + boundary inputs (empty, one, many, edges)\n"
             "  bad = 0\n"
             "  for given, expected in cases:\n"
             "      got = <call the public API>(given)\n"
             "      print('PASS' if got == expected else 'FAIL', repr(given), '->', repr(got), 'expected', repr(expected))\n"
             "      bad += got != expected\n"
             "  sys.exit(1 if bad else 0)\n"
             "  EOF\n"
             "then run: python3 /tmp/flow.py").format(spec=SPEC)


def register_spec(root, state, report):
    if not SPEC.is_file():
        report.append("NEXT: write the spec {}: a script that calls the code the way a user would and prints one "
                      "PASS/FAIL line per case. It must FAIL on the current code.\n{}".format(SPEC, SPEC_HELP))
        return False
    digest = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    if state.get("spec_sha") == digest:
        return True
    clean = run_spec(clean_copy(root))
    if clean["broken"]:
        report.append("SPEC IS BROKEN (error inside the spec itself, not in the library):\n{}\nFix {} and rerun."
                      .format(indent(clean["text"][-700:]), SPEC))
        return False
    if clean["ok"]:
        report.append("SPEC REJECTED: it PASSES on the original code, so it does not show the problem.\n{}\n"
                      "Use inputs/expectations that expose what the issue describes (boundary cases), then rerun."
                      .format(indent(clean["text"][-600:])))
        return False
    first = state.get("spec_sha") is None
    state["spec_sha"] = digest
    state["spec_clean"] = {"passes": clean["passes"], "fails": clean["fails"]}
    state.pop("last_eval", None)
    report.append("SPEC ACCEPTED{}: on the original code it fails ({} FAIL / {} PASS lines):\n{}".format(
        "" if first else " (updated)", clean["fails"], clean["passes"], indent(clean["text"][-800:])))
    if clean["fails"] == 0 and "Traceback" in clean["text"]:
        report.append("WARNING: the spec crashed instead of printing FAIL lines. Make sure the crash IS the issue's bug "
                      "and not a wrong call in the spec (wrong name/arguments); otherwise fix the spec first.")
    report.append("TRACE on the original code (what each repository function received and returned):\n" +
                  indent(trace(clean_copy(root), focus=clean["text"], chars=1600)))
    report.append("NEXT: find the first call in the trace whose result is already wrong. "
                  "`python3 /tmp/flow.py show FILE::NAME` to see it, write the corrected whole function to {} "
                  "and run `python3 /tmp/flow.py put FILE::NAME`.".format(NEW))
    return True


# ----------------------------------------------------------------------------- entry points
def install(root):
    bin_dir = STATE_ROOT / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("flow.py", "tracer.py", "find_candidates.py"):
        if (HERE / name).is_file() and (HERE / name).resolve() != (bin_dir / name).resolve():
            shutil.copyfile(HERE / name, bin_dir / name)
    SHIM.write_text("import runpy, sys\nsys.path.insert(0, {!r})\nrunpy.run_path({!r}, run_name='__main__')\n"
                    .format(str(bin_dir), str(bin_dir / "flow.py")))


def start(root, issue):
    state = load(root)
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    fresh = not state or state.get("head") != head or time.time() - state.get("t0", 0) > 900
    install(root)
    report = []
    if fresh:
        state = {"t0": time.time(), "head": head, "checkpoints": []}
        for stale in (SPEC, NEW):
            try:
                stale.unlink()
            except OSError:
                pass
        clean_copy(root)
        save(root, state)
    report.append("swe_flow ready {}. From now on use run_command only:\n"
                  "  python3 /tmp/flow.py                 check progress / what to do next\n"
                  "  python3 /tmp/flow.py show FILE::NAME  a function with line numbers and its callers\n"
                  "  python3 /tmp/flow.py put FILE::NAME   replace that whole function with the code in {}\n"
                  .format(clock(state), NEW))
    if fresh and issue.strip():
        sys.path.insert(0, str(HERE))
        try:
            import find_candidates
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                try:
                    find_candidates.main(["--query", issue, "--detail", "2", "--limit", "5"])
                except SystemExit:
                    pass
            ranking = buffer.getvalue().strip()
            report.append("WHERE THE ISSUE'S TERMS LIVE (hypotheses, not facts; the trace will decide):\n" + ranking[:2400])
        except Exception as exc:  # the controller must keep working without the ranking
            report.append("(ranking unavailable: {})".format(exc))
    elif not state.get("spec_sha"):
        report.append("(already started; clock continues)")
    if state.get("spec_sha"):
        report.append("Spec already registered. NEXT: python3 /tmp/flow.py")
    else:
        report.append("NEXT: write the spec {} (it must FAIL now).\n{}".format(SPEC, SPEC_HELP))
    out("\n\n".join(report))


def check(root, op, target):
    state = load(root)
    if not state:
        out("Not started. First call run_skill_script(skill_name=\"swe_flow\", file_path=\"scripts/flow.py\", "
            "args={\"issue\": \"<issue text>\"}).")
        return
    report = [clock(state)]
    if op == "show":
        out(clock(state) + "\n" + show(root, target))
        return
    if enforce_lock(root, state, report):
        save(root, state)
        out("\n".join(report))
        return
    if op == "put":
        if not state.get("spec_sha") and not register_spec(root, state, report):
            report.append("Register a failing spec before editing, so the change can be checked.")
            save(root, state)
            out("\n\n".join(report))
            return
        report.append(put(root, state, target))
    if register_spec(root, state, report):
        evaluate(root, state, report)
    enforce_deadline(root, state, report)
    if state.get("locked") and "submit_patch" not in report[-1]:
        report.append("Call submit_patch now.")
    save(root, state)
    out("\n\n".join(report))


def parse(argv):
    values, positional, i = {}, [], 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            positional += argv[i + 1:]
            break
        if token.startswith("--"):
            key, _, value = token[2:].partition("=")
            if not _:
                value = argv[i + 1] if i + 1 < len(argv) else ""
                i += 1
            values[key.replace("-", "_")] = value
        else:
            positional.append(token)
        i += 1
    return values, positional


def main(argv):
    values, positional = parse(argv)
    op = (values.pop("op", "") or (positional.pop(0) if positional and positional[0] in
                                     {"put", "show", "check", "start", "status"} else "")).lower()
    target = values.pop("target", "") or values.pop("file", "") or values.pop("symbol", "") or " ".join(positional)
    root = workspace_root()
    via_skill = Path(__file__).resolve().parent != (STATE_ROOT / "bin").resolve()
    if op == "start" or (via_skill and op not in {"put", "show"}):
        issue = " ".join(str(v) for v in values.values()) + " " + (target if op != "start" else "")
        start(root, issue)
        return 0
    check(root, op or "check", target)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # never leave the agent without guidance
        print("swe_flow internal error: {}: {}. You can still edit with shell commands and run "
              "`python3 /tmp/flow.py` again; if time is short, call submit_patch.".format(type(exc).__name__, exc))
        sys.exit(0)
