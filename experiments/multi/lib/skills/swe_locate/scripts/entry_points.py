"""Trace a function up to the public API that reaches it.

Arguments (ADK passes them as --name value pairs):
  --file PATH        workspace-relative file defining the symbol
  --symbol NAME      function or Class.method
  --depth N          caller levels to follow (default 3)

A call site counts when the caller's file defines the symbol or mentions its
class or module name, which filters out unrelated methods with the same name
(for example bytes.decode). Output: caller chains ending at public names, which
are the entry points a user would call to exercise the code.
"""
import ast
import os
import sys
from collections import defaultdict

from find_candidates import is_test_path, parse_args, workspace_root

SKIP_DIRS = {".git", "node_modules", "build", "dist", "venv", ".venv", "__pycache__",
             "docs", "docs_src", "examples", "benchmarks", "tests", "test"}


def python_files(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(base, name), root)
                if not is_test_path(rel):
                    yield rel


class Index:
    """Functions per file with the attribute and plain names each one calls."""

    def __init__(self, root):
        self.root = root
        self.functions = {}          # (file, qualname) -> set(called names)
        self.text = {}
        for rel in python_files(root):
            try:
                source = open(os.path.join(root, rel), encoding="utf-8").read()
                tree = ast.parse(source)
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            self.text[rel] = source
            self._walk(rel, tree, "")

    def _walk(self, rel, node, prefix):
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                self._walk(rel, child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                called = set()
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Attribute):
                            called.add(func.attr)
                        elif isinstance(func, ast.Name):
                            called.add(func.id)
                self.functions[(rel, prefix + child.name)] = called

    def callers(self, rel, qualname):
        short = qualname.split(".")[-1]
        owner = qualname.split(".")[0] if "." in qualname else ""
        module = os.path.splitext(os.path.basename(rel))[0]
        found = []
        for (file, name), called in self.functions.items():
            if short not in called or (file, name) == (rel, qualname):
                continue
            text = self.text.get(file, "")
            related = file == rel or (owner and owner in text) or (not owner and module in text)
            if related:
                found.append((file, name))
        return found


def is_public(qualname):
    return not any(part.startswith("_") and not part.startswith("__") for part in qualname.split("."))


def chains(index, rel, symbol, depth):
    results, frontier = [], [[(rel, symbol)]]
    for _ in range(depth):
        next_frontier = []
        for path in frontier:
            callers = index.callers(*path[-1])
            if not callers:
                results.append(path)
                continue
            for caller in callers[:6]:
                if caller in path:
                    continue
                extended = path + [caller]
                if is_public(caller[1]):
                    results.append(extended)
                next_frontier.append(extended)
        frontier = next_frontier[:30]
    results.extend(frontier)
    unique, seen = [], set()
    for path in results:
        key = tuple(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def describe(root, rel, symbol, depth=3, limit=6, index=None):
    index = index or Index(root)
    rows = chains(index, rel, symbol, depth)
    rows.sort(key=lambda path: (not is_public(path[-1][1]), len(path)))
    lines = []
    for path in rows[:limit]:
        entry = path[-1]
        lines.append(" <- ".join(f"{name} ({file})" for file, name in path)
                     + ("   [public entry]" if len(path) > 1 and is_public(entry[1]) else ""))
    return lines


def call_sites(root, rel, qualname, limit=3, context=1, index=None):
    """How callers use a function: numbered lines around each in-repository call of it.

    Returns text blocks, one per caller, so a reader sees what the caller does with the
    return value (joins it, indexes it, compares it) without paging through the file.
    """
    index = index or Index(root)
    leaf = qualname.split(".")[-1]
    blocks = []
    for file, name in sorted(index.callers(rel, qualname), key=lambda item: (item[0] != rel, item)):
        try:
            source = index.text[file]
            tree = ast.parse(source)
        except (KeyError, SyntaxError):
            continue
        lines = source.splitlines()
        target = _find(tree, name)
        if target is None:
            continue
        hits = sorted({sub.lineno for sub in ast.walk(target) if isinstance(sub, ast.Call) and (
            (isinstance(sub.func, ast.Attribute) and sub.func.attr == leaf)
            or (isinstance(sub.func, ast.Name) and sub.func.id == leaf))})
        if not hits:
            continue
        rows, shown = [f"{file}:{target.lineno} {name}  (calls {leaf})"], set()
        for hit in hits[:2]:
            numbers = set(range(max(target.lineno, hit - context), min(target.end_lineno, hit + context) + 1))
            # Def-use slice: where the names used on the call line got their values in this caller.
            names = {sub.id for stmt in ast.walk(target) if isinstance(stmt, ast.stmt) and stmt.lineno == hit
                     for sub in ast.walk(stmt) if isinstance(sub, ast.Name)} - {leaf}
            for stmt in ast.walk(target):
                if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and stmt.lineno < hit:
                    assigned = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    if any(isinstance(t, ast.Name) and t.id in names for t in assigned):
                        numbers.update(range(stmt.lineno, min(stmt.end_lineno, stmt.lineno + 2) + 1))
            previous = None
            for number in sorted(numbers):
                if number in shown or number - 1 >= len(lines):
                    continue
                if previous is not None and number > previous + 1:
                    rows.append("  ...")
                shown.add(number)
                previous = number
                rows.append(f"{number:5}: {lines[number - 1][:140]}")
        blocks.append("\n".join(rows))
        if len(blocks) >= limit:
            break
    return blocks


def _find(tree, qualname):
    node = tree
    for part in qualname.split("."):
        node = next((child for child in getattr(node, "body", [])
                     if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and child.name == part), None)
        if node is None:
            return None
    return node


def main(argv):
    from find_candidates import resolve_target
    values, positional = parse_args(argv)
    root = workspace_root()
    rel, symbol, _, problem = resolve_target(root, values, positional)
    if problem:
        print(problem)
        return 2
    if not symbol:
        print("ERROR: usage: args={\"file\": \"pkg/module.py\", \"symbol\": \"Class.method\"}")
        return 2
    try:
        depth = max(1, min(5, int(values.get("depth", 3))))
    except ValueError:
        depth = 3
    lines = describe(root, rel, symbol, depth)
    if not lines:
        print(f"No in-repository callers of {symbol}; it is itself the entry point.")
    else:
        print(f"Call chains reaching {symbol} (call the public entry with realistic input to reproduce):")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
