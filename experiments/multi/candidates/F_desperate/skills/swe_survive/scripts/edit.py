"""Replace one snippet in a source file, then immediately score it with the survival check.

    args={"file": "pkg/module.py", "old": "exact existing lines", "new": "replacement lines"}
Aliases: path/filepath/filename for file, search/find/before for old, replace/replacement/after for new.

Tool-call text sometimes arrives over-escaped (line breaks typed as the two characters \\n). The old
text is tried as given, then unescaped one level. The new text is tried at the same escape level that
old needed first, then the other; the first form that leaves the file compiling is used. So a call
typed with real line breaks keeps an escape inside a string literal (e.g. '\\t') exactly as typed.
Then survive.py's check runs on the result, so one call both edits and verifies. Once anything has
passed, a worse state is reverted automatically.
"""
import os
import re
import subprocess
import sys

import survive

KEYS = {"file": ("file", "path", "filepath", "filename", "file_path_in_repo"),
        "old": ("old", "search", "find", "before", "old_text", "old_str"),
        "new": ("new", "replace", "replacement", "after", "new_text", "new_str")}


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}


def decode(text):
    """One level of JSON-style unescaping in a single pass, so an escaped backslash (\\\\n) becomes \\n."""
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(0)), text, flags=re.S)


def strip_numbers(text):
    """Drop 'NNN: ' / 'NNN|' prefixes copied from numbered views."""
    lines = text.split("\n")
    if lines and all(re.match(r"^\s*\d+\s*[:|]\s?", line) for line in lines if line.strip()):
        return "\n".join(re.sub(r"^\s*\d+\s*[:|]\s?", "", line) for line in lines)
    return text


def locate(source, old):
    """Exact text first, then a line-by-line match that ignores leading/trailing whitespace."""
    if old and old in source:
        return old, source.count(old)
    wanted = [line.strip() for line in old.split("\n") if line.strip()]
    if not wanted:
        return None, 0
    lines = source.split("\n")
    hits = []
    for start in range(len(lines)):
        chunk, index = [], start
        while index < len(lines) and len(chunk) < len(wanted):
            if lines[index].strip():
                chunk.append(lines[index].strip())
            index += 1
        if chunk == wanted and lines[start].strip():
            hits.append("\n".join(lines[start:index]))
    if len(hits) == 1:
        return hits[0], 1
    return None, len(hits)


def reindent(found, new):
    """Match the first line's indentation of the text actually found."""
    base = re.match(r"\s*", found).group(0)
    given = re.match(r"\s*", new).group(0)
    if base == given:
        return new
    return "\n".join((base + line[len(given):]) if line.startswith(given) else line for line in new.split("\n"))


def compiles(path, text):
    if not path.endswith(".py"):
        return None
    try:
        compile(text, path, "exec")
        return None
    except SyntaxError as error:
        return f"line {error.lineno}: {error.msg}"


def main(argv):
    args, positional = survive.parse_args(argv)
    pick = lambda key: next((args[k] for k in KEYS[key] if k in args), None)
    file, old, new = pick("file"), pick("old"), pick("new")
    if file is None and positional:
        file, old, new = (positional + [None, None, None])[:3]
    if not file or old is None or new is None:
        print('ERROR: give args={"file": "pkg/module.py", "old": "exact existing lines", "new": "replacement lines"}')
        return 2
    root = os.path.realpath(args.get("root") or survive.workspace_root())
    path = os.path.normpath(os.path.join(root, file))
    relative = os.path.relpath(path, root)
    if not os.path.isfile(path) or os.path.relpath(path, root).startswith(".."):
        print(f"ERROR: {file} is not a file inside the repository. Edit source files only.")
        return 2
    if survive.TEST_PATH.search(relative):
        print("ERROR: test files are frozen (the graders replace them). Fix the source code instead.")
        return 2
    source = open(path, encoding="utf-8").read()
    found, count, decoded_old = None, 0, False
    for form in dict.fromkeys([old, strip_numbers(old), decode(old), decode(strip_numbers(old))]):
        found, count = locate(source, form)
        if found:
            decoded_old = form not in (old, strip_numbers(old))
            break
    if not found:
        why = f"matches {count} places; include more surrounding lines" if count > 1 else "not found"
        numbered = [f"{i + 1}: {line}" for i, line in enumerate(source.split("\n"))]
        key = next((line.strip() for line in decode(old).split("\n") if line.strip()), "")
        near = [n for n in numbered if key and key[:25] in n][:3]
        print(f"ERROR: old text {why} in {relative}. Nothing changed." + (" Nearby:\n" + "\n".join(near) if near else ""))
        return 1
    last_error = None
    # Over-escaping is uniform within a call: if old only matched after unescaping and new has no real
    # line break either, unescape new first too. New text with real line breaks is taken as typed first.
    over_escaped = decoded_old and "\n" not in new
    for form in dict.fromkeys([decode(new), new] if over_escaped else [new, decode(new)]):
        updated = source.replace(found, reindent(found, form), 1)
        last_error = compiles(relative, updated)
        if last_error is None:
            break
    if last_error:
        print(f"ERROR: edit refused, {relative} would not compile ({last_error}). Nothing changed.")
        return 1
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)
    diff = subprocess.run(["git", "diff", "--", relative], cwd=root, capture_output=True, text=True).stdout
    body = "\n".join(line for line in diff.splitlines() if line[:1] in "+-@" and line[:3] not in ("+++", "---"))
    print(f"EDITED {relative}:\n{body[-900:]}\n--- automatic check ---", flush=True)
    return survive.main(["--op", "check", "--root", root])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
