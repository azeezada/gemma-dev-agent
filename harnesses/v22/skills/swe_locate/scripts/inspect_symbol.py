"""Print a bounded implementation, actual line numbers, callers and test nodes."""
import ast
import os
import subprocess
import sys
from pathlib import Path

from find_candidates import parse_args, workspace_root, git, is_test_path


def symbols(tree, prefix=""):
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            yield name, node
            yield from symbols(node, name + ".")


def main(argv):
    args, _ = parse_args(argv)
    root = Path(workspace_root()).resolve()
    path = (root / args.get("file", "")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        print("ERROR: file must be an existing workspace-relative source file")
        return 2
    source = path.read_text(); lines = source.splitlines()
    try:
        entries = list(symbols(ast.parse(source)))
    except SyntaxError as error:
        print("Cannot parse source:", error)
        return 1
    symbol = args.get("symbol", "")
    matches = [(name, node) for name, node in entries if name == symbol or name.endswith("." + symbol)] if symbol else []
    if not matches:
        print("SYMBOLS (choose a qualified name):")
        print("\n".join(f"{name}: {node.lineno}-{node.end_lineno}" for name, node in entries)[:4500])
        return 0
    if len(matches) > 1:
        print("Ambiguous symbol:", ", ".join(name for name, _ in matches))
        return 1
    name, node = matches[0]
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    first = body[0].lineno if body else node.end_lineno
    out = [f"{path.relative_to(root)}:{node.lineno}-{node.end_lineno} {name}",
           f"Signature starts at {node.lineno}; executable body at {first} (docstring omitted).",
           f"{node.lineno:5}: {lines[node.lineno - 1]}"]
    # Show the body, not a 150-line annotated signature. Large classes get an outline.
    if isinstance(node, ast.ClassDef):
        out += [f"{n.lineno:5}: {lines[n.lineno - 1]}" for n in body]
    else:
        used = 0
        for i in range(first - 1, node.end_lineno):
            row = f"{i + 1:5}: {lines[i]}"
            if used + len(row) > 3300:
                out.append(f"... body continues at line {i + 1}; read that range if needed")
                break
            out.append(row); used += len(row) + 1
    out.append("REFERENCES (including real test node names):")
    leaf = name.split(".")[-1]
    hits = git(str(root), "grep", "-n", "-w", "-F", "-e", leaf, "--", "*.py").splitlines()
    hits.sort(key=lambda row: (not is_test_path(row.split(":", 1)[0]), row))
    seen = set()
    for hit in hits:
        parts = hit.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        rel, number, text = parts; number = int(number)
        if rel == str(path.relative_to(root)) and node.lineno <= number <= node.end_lineno:
            continue
        label = f"{rel}:{number} {text.strip()[:100]}"
        if is_test_path(rel):
            try:
                nodes = [(n, s) for n, s in symbols(ast.parse((root / rel).read_text()))
                         if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.lineno <= number <= s.end_lineno]
                if nodes:
                    label = rel + "::" + min(nodes, key=lambda item: item[1].end_lineno - item[1].lineno)[0].replace(".", "::")
            except (OSError, SyntaxError):
                pass
        if label not in seen:
            out.append(label); seen.add(label)
        if len(seen) >= 8:
            break
    print("\n".join(out)[:5000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
