"""Replace one snippet in a workspace file and print the resulting diff.

Arguments (ADK passes them as --name value pairs):
  --file PATH        workspace-relative file to edit
  --old TEXT         existing text to replace (one or more consecutive lines)
  --new TEXT         replacement text ("" deletes the snippet)
  --occurrence N     optional 1-based match to replace when --old is not unique

Tool-call arguments sometimes arrive with line breaks and quotes typed as the
two-character escapes \\n, \\t and \\". The script tries the text as given first
and falls back to the decoded form, and it refuses any Python edit that would
leave the file unable to compile.
"""
import ast
import contextlib
import io
import json
import difflib
import os
import subprocess
import sys

OUTPUT_LIMIT = 1500


def parse_args(argv):
    values, positional, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positional.extend(argv[index + 1:])
            break
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            values[key.replace("-", "_")] = value
            index += 1
        elif token.startswith("--"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            values[token[2:].replace("-", "_")] = value
            index += 2
        else:
            positional.append(token)
            index += 1
    return values, positional


def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        # A git worktree has a .git file rather than a directory.
        if candidate and os.path.exists(os.path.join(candidate, ".git")):
            return os.path.realpath(candidate)
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if top:
            return top
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def decode_escapes(text):
    """Turn typed escape sequences into the characters they name."""
    replacements = {"n": "\n", "t": "\t", '"': '"', "'": "'", "\\": "\\", "r": ""}
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in replacements:
            out.append(replacements[text[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def decode_line_breaks(text):
    """Decode typed \\n / \\t only outside quoted string literals; escapes inside quotes stay as typed."""
    out, index, quote = [], 0, None
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            pair = text[index + 1]
            if quote is None and pair in "nt":
                out.append("\n" if pair == "n" else "\t")
            else:
                out.append(char + pair)
            index += 2
            continue
        if char in "'\"":
            quote = None if quote == char else (quote or char)
        elif char == "\n":
            quote = None
        out.append(char)
        index += 1
    return "".join(out)


def leading_ws(line):
    return line[: len(line) - len(line.lstrip())]


def exact_spans(content, old):
    spans, start = [], content.find(old)
    while start != -1:
        spans.append((start, start + len(old)))
        start = content.find(old, start + 1)
    return spans


def flexible_spans(content, old):
    """Match line by line ignoring leading/trailing whitespace; returns spans and indent info."""
    old_lines = [line.strip() for line in old.strip("\n").split("\n")]
    if not any(old_lines):
        return []
    lines = content.split("\n")
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line) + 1
    spans = []
    for first in range(len(lines) - len(old_lines) + 1):
        if all(lines[first + i].strip() == old_lines[i] for i in range(len(old_lines))):
            last = first + len(old_lines) - 1
            end = offsets[last] + len(lines[last])
            spans.append((offsets[first], end, leading_ws(lines[first])))
    return spans


def reindent(new, old, target_indent):
    old_first = next((line for line in old.split("\n") if line.strip()), "")
    source_indent = leading_ws(old_first)
    result = []
    for line in new.strip("\n").split("\n"):
        if line.startswith(source_indent):
            result.append(target_indent + line[len(source_indent):])
        elif line.strip():
            result.append(target_indent + line.lstrip())
        else:
            result.append(line)
    return "\n".join(result)


def compiles(path, text):
    try:
        compile(text, path, "exec")
        return None
    except SyntaxError as exc:
        return "line {}: {}".format(exc.lineno, exc.msg)


def closest_windows(content, old, limit=3):
    lines = content.split("\n")
    old_lines = old.strip("\n").split("\n")
    width = max(1, len(old_lines))
    target = "\n".join(line.strip() for line in old_lines)
    scored = []
    for first in range(0, max(1, len(lines) - width + 1)):
        window = "\n".join(line.strip() for line in lines[first:first + width])
        ratio = difflib.SequenceMatcher(None, target, window).quick_ratio()
        if ratio > 0.5:
            scored.append((ratio, first))
    scored.sort(reverse=True)
    picked = []
    for ratio, first in scored:
        if len(picked) >= limit * 20:
            break
        exact_ratio = difflib.SequenceMatcher(
            None, target, "\n".join(line.strip() for line in lines[first:first + width])
        ).ratio()
        picked.append((exact_ratio, first))
    picked.sort(reverse=True)
    shown, blocks = set(), []
    for ratio, first in picked:
        if any(abs(first - other) < width for other in shown) or len(blocks) >= limit:
            continue
        shown.add(first)
        body = "\n".join(
            "{:>5}: {}".format(first + 1 + i, line[:120])
            for i, line in enumerate(lines[first:first + width][:6])
        )
        blocks.append("similarity {:.2f}\n{}".format(ratio, body))
    return blocks


def emit(text):
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + "\n...[truncated]"
    print(text)


def single_edit(argv, staged=None):
    values, positional = parse_args(argv)
    pick = lambda *keys: next((values[key] for key in keys if values.get(key) is not None), None)
    rel = pick("file", "filepath", "path", "filename", "target_file", "target") or (
        values["file_path"] if not str(values.get("file_path", "scripts/")).startswith("scripts/") else "") or (
        positional[0] if positional else "")
    old = pick("old", "old_string", "old_str", "old_text", "search", "find", "before")
    new = pick("new", "new_string", "new_str", "new_text", "replace", "replacement", "after")
    if old is None and new is None and len(positional) == 3:
        old, new = positional[1], positional[2]
    if not rel or old is None or new is None:
        emit("ERROR: usage: --file PATH --old TEXT --new TEXT [--occurrence N]")
        return 2
    if not old.strip():
        emit("ERROR: --old must contain the existing text to replace")
        return 2
    root = workspace_root()
    rel = rel.replace("/workspace/", "", 1).lstrip("/") if rel.startswith("/workspace") else rel
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep):
        emit("ERROR: path must stay inside the workspace")
        return 2
    if not os.path.isfile(path):
        emit("ERROR: file not found: {}".format(rel))
        return 2
    with open(path, encoding="utf-8") as handle:
        content = handle.read().replace("\r\n", "\n")
    if staged is not None and path in staged:
        content = staged[path]
    occurrence = values.get("occurrence")
    try:
        occurrence = int(occurrence) if occurrence not in (None, "", "None") else None
    except ValueError:
        emit("ERROR: --occurrence must be an integer")
        return 2

    old_variants = [(old, False)]
    decoded_old = decode_escapes(old)
    if decoded_old != old:
        old_variants.append((decoded_old, True))

    chosen = None
    for variant, decoded in old_variants:
        spans = [(s, e, None) for s, e in exact_spans(content, variant)]
        strategy = "exact"
        if not spans:
            spans = flexible_spans(content, variant)
            strategy = "flexible"
        if spans:
            chosen = (variant, decoded, spans, strategy)
            break
    if chosen is None:
        blocks = closest_windows(content, decoded_old)
        message = "ERROR: --old text not found in {}.".format(rel)
        if blocks:
            message += " Closest regions (copy exact text from one of these):\n" + "\n\n".join(blocks)
        emit(message)
        return 1
    variant, decoded, spans, strategy = chosen
    if len(spans) > 1 and occurrence is None:
        starts = [content.count("\n", 0, span[0]) + 1 for span in spans]
        emit("ERROR: --old matches {} places (lines {}). Add surrounding lines to make it unique "
             "or pass --occurrence N.".format(len(spans), ", ".join(map(str, starts[:10]))))
        return 1
    if occurrence is not None and not 1 <= occurrence <= len(spans):
        emit("ERROR: --occurrence must be between 1 and {}".format(len(spans)))
        return 1
    start, end, indent = spans[(occurrence or 1) - 1]

    # The replacement is tried as typed first even when --old only matched after decoding: models often
    # copy --old from an escaped display but type --new correctly. Compilation decides between variants.
    new_variants = list(dict.fromkeys([new, decode_line_breaks(new), decode_escapes(new)]))
    is_python = path.endswith(".py")
    original_error = compiles(path, content) if is_python else None
    result, first_error, used = None, None, new
    for candidate in new_variants:
        replacement = candidate
        if strategy == "flexible":
            replacement = reindent(candidate, variant, indent) if candidate.strip() else ""
        updated = content[:start] + replacement + content[end:]
        error = compiles(path, updated) if is_python and original_error is None else None
        if error is None:
            result, used = updated, candidate
            break
        first_error = first_error or error
    if result is None:
        emit("ERROR: edit not applied because the file would not compile ({}, with --new exactly as typed). "
             "Check indentation and brackets in --new; escape sequences inside string literals are kept as "
             "typed.".format(first_error))
        return 1
    if staged is not None:
        staged[path] = result
        return 0
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(result)
    note = " (escape sequences decoded)" if decoded or used != new else ""
    diff = subprocess.run(
        ["git", "diff", "-U2", "--", os.path.relpath(path, root)],
        cwd=root, capture_output=True, text=True, timeout=20,
    ).stdout
    if not diff.strip():
        diff = "(no textual change: --new equals the matched text)"
    emit("OK: edited {} [{} match{}]\n{}".format(rel, strategy, note, diff))
    return 0


def arm_guard():
    """Start the checkpoint guard (see guard.py) for the real workspace; returns deadline-salvage lines."""
    try:
        import guard
        root = workspace_root()
        return guard.deadline_check(root) if guard.arm(root) else []
    except Exception:  # the guard must never break an edit
        return []


def main(argv):
    values, _ = parse_args(argv)
    late = arm_guard()
    if late:
        emit("\n".join(late + ["The edit was NOT applied."]))
        return 1
    if "edits" not in values:
        return single_edit(argv)
    try:
        try:
            edits = json.loads(values["edits"])
        except ValueError:
            edits = ast.literal_eval(values["edits"])
        if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
            raise ValueError("edits must contain 1 to 16 replacement objects")
        staged = {}
        for index, edit in enumerate(edits, 1):
            if not isinstance(edit, dict) or not {"file", "old", "new"} <= set(edit):
                raise ValueError("each edit needs file, old, new")
            if set(edit) - {"file", "old", "new", "occurrence"}:
                raise ValueError("unknown edit field")
            if not all(isinstance(edit[key], str) for key in ("file", "old", "new")):
                raise ValueError("file, old and new must be strings")
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = single_edit([part for key, value in edit.items() for part in ("--" + key, str(value))], staged)
            if code:
                emit(f"Batch rejected at edit {index}; no files changed.\n" + captured.getvalue())
                return code
        original = {path: open(path, "rb").read() for path in staged}
        try:
            for path, text in staged.items():
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
        except Exception:
            for path, content in original.items():
                with open(path, "wb") as handle:
                    handle.write(content)
            raise
        root = workspace_root()
        paths = [os.path.relpath(path, root) for path in staged]
        diff = subprocess.run(["git", "diff", "-U2", "--", *paths], cwd=root,
                              capture_output=True, text=True, timeout=20).stdout
        emit("OK: applied {} edits to {}\n{}".format(len(edits), ", ".join(paths), diff))
        return 0
    except (ValueError, SyntaxError, OSError, TypeError) as error:
        emit("ERROR: " + str(error))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
