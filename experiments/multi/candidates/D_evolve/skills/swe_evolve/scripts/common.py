"""Shared helpers for the swe_evolve scripts (spec-first evolutionary patch search).

Everything the scripts keep lives outside the repository:
  /tmp/d_spec.py          executable specification written by the scout (exit 0 only when fixed)
  /tmp/d_cands/<id>.py    candidate patches written by proposers (heredoc via run_command)
  /tmp/d_evolve/          state: focus, baseline, evaluated results, run start time
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

SPEC = Path("/tmp/d_spec.py")
CANDS = Path("/tmp/d_cands")
STATE = Path("/tmp/d_evolve")


# ---------------------------------------------------------------- arguments
def parse_args(argv):
    """ADK passes --key value pairs; tolerate --key=value, positional values and stray '--'."""
    values, positional, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            values[key.replace("-", "_").lower()] = value
            index += 1
        elif token.startswith("--"):
            value = argv[index + 1] if index + 1 < len(argv) and not argv[index + 1].startswith("--") else ""
            values[token[2:].replace("-", "_").lower()] = value
            index += 2 if value != "" else 1
        else:
            positional.append(token)
            index += 1
    return values, positional


# ---------------------------------------------------------------- workspace
def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        if candidate and os.path.exists(os.path.join(candidate, ".git")):
            return os.path.realpath(candidate)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        if top:
            return top
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def git(root, *args, timeout=20):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)


def untracked(root):
    return set(git(root, "ls-files", "--others", "--exclude-standard").stdout.split())


def python_env(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ---------------------------------------------------------------- state
def load_state(root):
    """State for this workspace. A state left by another workspace (another task) is discarded."""
    path = STATE / "state.json"
    state = {}
    if path.is_file():
        try:
            state = json.loads(path.read_text())
        except ValueError:
            state = {}
    CANDS.mkdir(parents=True, exist_ok=True)  # proposers write here with a plain `cat >` heredoc
    if state.get("root") != root:
        for stale in (CANDS.glob("*") if CANDS.is_dir() else []):
            stale.unlink(missing_ok=True)
        if SPEC.is_file() and state.get("root"):
            SPEC.unlink(missing_ok=True)
        state = {"root": root, "started": time.time(), "untracked_at_start": sorted(untracked(root)),
                 "focus": [], "results": {}, "originals": {}}
        save_state(state)
    return state


def save_state(state):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE / "state.json")


def elapsed(state):
    return time.time() - state.get("started", time.time())


def scout_clock(state):
    """One line for the scout: the proposers need the remaining time more than the spec needs polish."""
    spent = elapsed(state)
    if spent > 100:
        return f"[{spent:.0f}s used] TIME: stop now and reply with your one-line FOCUS/SPEC summary."
    return f"[{spent:.0f}s used of a ~100s scouting budget]"


# ---------------------------------------------------------------- symbols
def split_target(text):
    """'pkg/mod.py::Cls.meth', 'pkg/mod.py:Cls.meth', 'pkg/mod.py Cls.meth' -> (path, qualname)."""
    text = text.strip().strip("`'\"")
    for sep in ("::", ":", " ", "#"):
        if sep in text and text.split(sep, 1)[0].endswith(".py"):
            path, name = text.split(sep, 1)
            return path.strip(), name.strip().strip(":").strip()
    return text, ""


def find_node(tree, qualname):
    """Function/class node for a dotted qualname (Cls.meth, func, Outer.Inner.meth)."""
    parts = [part for part in qualname.split(".") if part]
    body, node = tree.body, None
    for part in parts:
        node = next((child for child in body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and child.name == part), None)
        if node is None:
            return None
        body = node.body if isinstance(node, ast.ClassDef) else []
    return node


def node_span(node):
    """1-based inclusive (start, end) including decorators."""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def callers(root, path, qualname, limit=6):
    """Lines elsewhere that call the symbol's last name: how its result is used."""
    name = qualname.split(".")[-1]
    if not name or name.startswith("__"):
        return []
    try:
        out = subprocess.run(["git", "grep", "-n", "-E", rf"[.( ]{re.escape(name)}\(", "--", "*.py"],
                             cwd=root, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        file_ = line.split(":", 1)[0]
        if re.search(r"(^|/)(tests?|testing|docs?|examples?|benchmarks?)/|(^|/)test_", file_):
            continue
        if re.search(rf"\bdef {re.escape(name)}\b", line):
            continue
        rows.append(line.strip()[:220])
        if len(rows) <= 3 * 4:
            rows += _uses(root, line)
    return rows[:limit * 4]


def _uses(root, hit, window=40):
    """Def-use slice for a caller line: where the names used on it were assigned just before (first 3 lines each)."""
    try:
        file_, number, code = hit.split(":", 2)
        lines = (Path(root) / file_).read_text(encoding="utf-8", errors="replace").split("\n")
        number = int(number)
    except (ValueError, OSError):
        return []
    names = set(re.findall(r"\b([A-Za-z_]\w*)\b", code)) - {"self", "for", "in", "return", "yield", "if", "not", "and", "or"}
    out = []
    for index in range(max(0, number - 1 - window), number - 1):
        match = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)", lines[index])
        if match and match[1] in names:
            out += [f"      {file_}:{index + 1 + k}: {lines[index + k].strip()}"[:200] for k in range(3)
                    if index + k < number - 1]
    return out[-6:]


# ---------------------------------------------------------------- candidates
TARGET_RE = re.compile(r"^\s*#\s*(target|replace)\s*:\s*(.+?)\s*$", re.I)
FENCE_RE = re.compile(r"^\s*```")


def parse_candidate(text):
    """Split a candidate file into blocks.

    Function block:   # target: pkg/mod.py::Cls.meth   then the complete new definition
    Replace block:    # replace: pkg/mod.py   then <<<<<<< / ======= / >>>>>>> sections (exact old text, new text)
    """
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if not FENCE_RE.match(line)]
    blocks, current = [], None
    for line in lines:
        match = TARGET_RE.match(line)
        if match:
            kind, spec = match[1].lower(), match[2]
            path, name = split_target(spec)
            current = {"kind": "function" if kind == "target" and name else "replace", "path": path,
                       "name": name, "lines": []}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)
    if not blocks:
        raise ValueError("no '# target: path.py::Name' (or '# replace: path.py') header line found")
    for block in blocks:
        block["code"] = "\n".join(block.pop("lines")).strip("\n")
        if not block["code"].strip():
            raise ValueError(f"empty block for {block['path']}")
    return blocks


def _reindent(code, indent):
    code = textwrap.dedent(code)
    return "\n".join((indent + line) if line.strip() else "" for line in code.split("\n"))


def _docstring(node):
    body = getattr(node, "body", None) or []
    first = body[0] if body else None
    return first if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
        and isinstance(first.value.value, str) else None


def _keep_docstring(code, new_def, old_node, old_lines):
    """A proposer may omit the docstring to save tokens; the original one is kept."""
    if old_node is None or getattr(new_def, "name", None) != old_node.name or _docstring(new_def) or not new_def.body:
        return code
    old_doc = _docstring(old_node)
    if old_doc is None:
        return code
    doc = old_lines[old_doc.lineno - 1:old_doc.end_lineno]
    old_indent = re.match(r"\s*", doc[0])[0]
    new_lines = code.split("\n")
    first = new_def.body[0].lineno - 1
    if new_def.body[0].lineno == new_def.lineno:  # one-line def: leave as is
        return code
    new_indent = re.match(r"\s*", new_lines[first])[0]
    moved = [new_indent + line[len(old_indent):] if line.startswith(old_indent) else line for line in doc]
    return "\n".join(new_lines[:first] + moved + new_lines[first:])


def _apply_function(source, block):
    tree = ast.parse(source)
    code = textwrap.dedent(block["code"])
    # Leading import lines in a block are hoisted to the module top.
    imports, rest = [], code.split("\n")
    while rest and (re.match(r"^(import|from)\s+\S+", rest[0]) or not rest[0].strip()):
        line = rest.pop(0)
        if line.strip():
            imports.append(line.strip())
    code = "\n".join(rest)
    try:
        new_tree = ast.parse(code)
    except SyntaxError as error:
        raise ValueError(f"{block['path']}::{block['name']}: new code does not parse (line {error.lineno}: {error.msg})")
    defs = [n for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not defs:
        raise ValueError(f"{block['path']}::{block['name']}: block must contain the complete 'def'/'class'")
    lines = source.split("\n")
    node = find_node(tree, block["name"])
    code = _keep_docstring(code, defs[0], node, lines)
    if node is not None:
        start, end = node_span(node)
        indent = re.match(r"\s*", lines[node.lineno - 1])[0]
        # Keep the original decorators unless the new code brings its own.
        if not getattr(defs[0], "decorator_list", None) and start < node.lineno:
            start = node.lineno
        new = _reindent(code, indent).split("\n")
        lines[start - 1:end] = new
    else:
        parent_name, _, leaf = block["name"].rpartition(".")
        parent = find_node(tree, parent_name) if parent_name else None
        if parent_name and not isinstance(parent, ast.ClassDef):
            raise ValueError(f"{block['path']}: '{block['name']}' not found (no class '{parent_name}')")
        if parent is not None:
            indent = re.match(r"\s*", lines[parent.body[0].lineno - 1])[0]
            lines[parent.end_lineno:parent.end_lineno] = [""] + _reindent(code, indent).split("\n")
        else:
            lines += ["", ""] + _reindent(code, "").split("\n")
    text = "\n".join(lines)
    missing = [imp for imp in imports if imp not in text]
    if missing:
        module = ast.parse(text)
        last = 0
        for stmt in module.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                last = stmt.end_lineno
            elif last or not (isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant)):
                break
        body = text.split("\n")
        body[last:last] = missing
        text = "\n".join(body)
    return text


SR_RE = re.compile(r"<{5,}[^\n]*\n(.*?)\n={5,}[^\n]*\n(.*?)\n>{5,}[^\n]*", re.S)


def _apply_replace(source, block):
    pairs = SR_RE.findall(block["code"] + "\n")
    if not pairs:
        raise ValueError(f"{block['path']}: replace block needs <<<<<<< old ======= new >>>>>>> sections")
    for old, new in pairs:
        if old in source:
            source = source.replace(old, new, 1)
            continue
        # Tolerate indentation drift: match on stripped lines, keep the file's indentation.
        src_lines, old_lines = source.split("\n"), old.split("\n")
        stripped = [line.strip() for line in old_lines]
        hit = next((i for i in range(len(src_lines) - len(old_lines) + 1)
                    if [l.strip() for l in src_lines[i:i + len(old_lines)]] == stripped), None)
        if hit is None:
            raise ValueError(f"{block['path']}: old text not found (copy it exactly from the context)")
        indent = re.match(r"\s*", src_lines[hit])[0]
        old_indent = re.match(r"\s*", old_lines[0])[0]
        shifted = [indent + line[len(old_indent):] if line.startswith(old_indent) else line
                   for line in new.split("\n")]
        src_lines[hit:hit + len(old_lines)] = shifted
        source = "\n".join(src_lines)
    return source


def head_text(root, rel):
    """File content at HEAD (candidates always apply to the original, never to another candidate)."""
    shown = git(root, "show", f"HEAD:{rel}")
    return shown.stdout if shown.returncode == 0 else (Path(root) / rel).read_text(encoding="utf-8")


def apply_blocks(root, blocks):
    """Return {path: new_text} computed from HEAD; raises ValueError with a model-readable reason."""
    out = {}
    for block in blocks:
        raw = block["path"].strip()
        rel = os.path.relpath(raw, root) if raw.startswith("/") else raw.removeprefix("./")
        path = Path(root) / rel
        if not path.is_file() or ".." in Path(rel).parts:
            raise ValueError(f"{block['path']}: not an existing file in the repository")
        if re.search(r"(^|/)tests?/|(^|/)test_[^/]*\.py$", rel):
            raise ValueError(f"{rel}: candidates change source code only, not tests")
        source = out.get(rel) or head_text(root, rel)
        new = _apply_function(source, block) if block["kind"] == "function" else _apply_replace(source, block)
        try:
            compile(new, rel, "exec")
        except SyntaxError as error:
            raise ValueError(f"{rel}: result does not compile (line {error.lineno}: {error.msg})")
        out[rel] = new
    return out


def unified(root, changes):
    chunks = []
    for rel, new in changes.items():
        old = head_text(root, rel)
        chunks += difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}")
    return "".join(chunks)


def digest(text):
    return hashlib.sha1(text.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- execution
CHECK_RE = re.compile(r"^\s*(PASS|FAIL)\b(.*)$")


def run_spec(root, timeout=10):
    """Run /tmp/d_spec.py against the checkout at root. Returns exit code, PASS/FAIL lines and tail."""
    if not SPEC.is_file():
        return {"exit": None, "passes": 0, "fails": 0, "fail_lines": [], "tail": "no /tmp/d_spec.py yet"}
    try:
        done = subprocess.run([sys.executable, str(SPEC)], cwd=root, env=python_env(root), capture_output=True,
                              text=True, timeout=timeout)
        output, code = done.stdout + ("\n" + done.stderr if done.stderr.strip() else ""), done.returncode
    except subprocess.TimeoutExpired:
        output, code = f"TIMEOUT after {timeout}s", 124
    checks = [CHECK_RE.match(line) for line in output.splitlines()]
    checks = [m for m in checks if m]
    tail = "\n".join(output.strip().splitlines()[-8:])
    return {"exit": code, "crashed": "Traceback (most recent call last)" in output,
            "passes": sum(m[1] == "PASS" for m in checks), "fails": sum(m[1] == "FAIL" for m in checks),
            "fail_lines": [("FAIL" + m[2]).strip()[:300] for m in checks if m[1] == "FAIL"][:8], "tail": tail[-1500:]}


def spec_bug(result):
    """The spec crashed before checking anything: a bug in the spec, not in the code under test."""
    return result["exit"] not in (0, None) and result["passes"] + result["fails"] == 0 and \
        (result.get("crashed") or "Traceback" in result["tail"])


def clean_copy(root):
    """A pristine HEAD checkout outside the repository (cached per commit)."""
    head = git(root, "rev-parse", "HEAD").stdout.strip() or "nohead"
    base = STATE / f"base-{head[:12]}"
    if not (base / ".ok").is_file():
        import io
        import shutil
        import tarfile
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", "HEAD"], cwd=root, capture_output=True, timeout=20).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(base, filter="data")
        (base / ".ok").write_text("")
    return str(base)


def dirty(root):
    return bool(git(root, "diff", "--name-only", "HEAD").stdout.strip())
