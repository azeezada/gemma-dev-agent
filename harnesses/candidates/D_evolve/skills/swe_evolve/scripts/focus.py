"""Record the function the fix should change and show it with its callers.

args: {"target": "pkg/module.py::Class.method"}   (also accepts file + symbol, or a positional value)
Prints the current definition with line numbers and the lines that call it (how its result is used).
Proposers see the same view through context.py.
"""
import ast
import sys
from pathlib import Path

from common import (callers, find_node, git, load_state, node_span, parse_args, save_state, scout_clock, split_target,
                    workspace_root)


def show(root, path, qualname, rows=80):
    source = git(root, "show", f"HEAD:{path}").stdout or (Path(root) / path).read_text(encoding="utf-8")
    node = find_node(ast.parse(source), qualname)
    if node is None:
        return None
    start, end = node_span(node)
    lines = source.split("\n")
    body = [f"{number:5d}  {lines[number - 1]}" for number in range(start, min(end, start + rows - 1) + 1)]
    if end > start + rows - 1:
        body.append(f"      ... ({end - start + 1} lines in total)")
    used = callers(root, path, qualname)
    text = [f"== {path}::{qualname} (lines {start}-{end})", *body]
    text += ["  called from:"] + [f"    {row}" for row in used] if used else ["  called from: (no non-test callers found)"]
    return "\n".join(text)


def main(argv):
    values, positional = parse_args(argv)
    raw = values.get("target") or values.get("symbol") or " ".join(positional)
    path, name = split_target(raw)
    if values.get("file"):
        path, name = values["file"], values.get("symbol") or values.get("name") or name
    if "::" in (name or "") or (name or "").endswith(".py"):
        path, name = split_target(name)
    root = workspace_root()
    state = load_state(root)
    if not path.endswith(".py") or not (Path(root) / path).is_file():
        print(f"ERROR: give target as 'pkg/module.py::Class.method' (got {raw!r}); the file must exist.")
        return 2
    if not name:
        # Module-level code (constants, configuration, calls): show the file head; proposers use '# replace:'.
        source = (Path(root) / path).read_text(encoding="utf-8").split("\n")
        state["focus"] = [f"{path}::"] + [t for t in state.get("focus", []) if t != f"{path}::"][:2]
        save_state(state)
        print(f"== {path} (module level, lines 1-{min(len(source), 120)})")
        print("\n".join(f"{n:5d}  {line}" for n, line in enumerate(source[:120], 1)))
        print(f"\nFOCUS recorded: {path} (module level; candidates use '# replace: {path}' blocks).")
        return 0
    view = show(root, path, name)
    if view is None:
        print(f"ERROR: {name} not found in {path}. Use Class.method for methods.")
        return 2
    target = f"{path}::{name}"
    state["focus"] = [target] + [t for t in state.get("focus", []) if t != target][:2]
    save_state(state)
    print(view)
    print(f"\nFOCUS recorded: {target} (primary). Next: write /tmp/d_spec.py and run spec.py. {scout_clock(state)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
