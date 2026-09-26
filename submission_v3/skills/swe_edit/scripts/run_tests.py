"""Run focused pytest targets and print a short summary instead of full tracebacks.

Arguments (ADK passes them as --name value pairs):
  --targets "tests/test_a.py tests/test_b.py::test_x"   files, node ids, or directories
  --k EXPR           optional pytest -k selector
  --baseline true    also re-run the failing tests with your uncommitted changes stashed,
                     to separate failures you introduced from failures that already existed
  --timeout SECONDS  per pytest run (default 45)
"""
import ast
import os
import re
import subprocess
import sys

OUTPUT_LIMIT = 1500
MAX_LISTED = 8


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


def as_list(value):
    """Accept a list, a repr of a list, or a comma/space separated string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(item) for item in parsed]
        except (ValueError, SyntaxError):
            pass
    return [item for item in re.split(r"[\s,]+", text) if item]


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        if candidate and os.path.isdir(os.path.join(candidate, ".git")):
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


def run_pytest(root, targets, keyword, timeout):
    command = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=line", "--color=no",
               "-p", "no:cacheprovider"]
    if keyword:
        command += ["-k", keyword]
    command += targets
    try:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "failed": {}, "summary": "", "errors": ""}
    output = completed.stdout + "\n" + completed.stderr
    failed = {}
    for line in output.splitlines():
        match = re.match(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", line.strip())
        if match:
            failed[match.group(2)] = (match.group(1), (match.group(3) or "").strip())
    summary = ""
    for line in reversed(output.splitlines()):
        if re.search(r"\b(passed|failed|error|errors|skipped|no tests ran)\b", line) and " in " in line:
            summary = line.strip("= ").strip()
            break
    errors = ""
    if not failed and completed.returncode not in (0, 5):
        interesting = [line for line in output.splitlines()
                       if re.search(r"Error|error:|ERROR|not found|no match", line)]
        errors = "\n".join(interesting[:6]) or output.strip()[-600:]
    return {"timed_out": False, "failed": failed, "summary": summary, "errors": errors,
            "returncode": completed.returncode}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)


def emit(text):
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + "\n...[truncated]"
    print(text)


def main(argv):
    values, positional = parse_args(argv)
    targets = as_list(values.get("targets")) + as_list(values.get("target")) + positional
    keyword = values.get("k") or values.get("keyword") or ""
    try:
        timeout = max(5, min(120, int(float(values.get("timeout", 45)))))
    except ValueError:
        timeout = 45
    if not targets:
        emit("ERROR: pass --targets with test files or node ids, e.g. --targets tests/test_x.py")
        return 2
    root = workspace_root()
    result = run_pytest(root, targets, keyword, timeout)
    if result["timed_out"]:
        emit("TIMEOUT: pytest exceeded {}s. Narrow --targets to specific files or node ids.".format(timeout))
        return 1
    lines = [result["summary"] or "pytest exit code {}".format(result["returncode"])]
    if result["errors"]:
        lines.append("Collection or usage problems:\n" + result["errors"])
    failed = result["failed"]
    status = {}
    if failed and truthy(values.get("baseline", "false")):
        changed = git(root, "diff", "--name-only").stdout.split()
        if not changed:
            lines.append("baseline skipped: no uncommitted changes to compare against")
        else:
            stash = git(root, "stash", "push", "--quiet", "--", *changed)
            try:
                if stash.returncode == 0:
                    before = run_pytest(root, list(failed), "", timeout)
                    for node in failed:
                        if before["timed_out"]:
                            status[node] = "baseline timed out"
                        elif node in before["failed"]:
                            status[node] = "already failing before your change"
                        else:
                            status[node] = "NEW: introduced by your change"
                else:
                    lines.append("baseline skipped: git stash failed: " + stash.stderr.strip()[:200])
            finally:
                if stash.returncode == 0:
                    popped = git(root, "stash", "pop", "--quiet")
                    if popped.returncode != 0:
                        lines.append("WARNING: git stash pop failed; run git stash pop: "
                                     + popped.stderr.strip()[:200])
    for node, (kind, message) in list(failed.items())[:MAX_LISTED]:
        label = " [{}]".format(status[node]) if node in status else ""
        lines.append("{} {}{}\n    {}".format(kind, node, label, message[:200]))
    if len(failed) > MAX_LISTED:
        lines.append("... {} more failing".format(len(failed) - MAX_LISTED))
    emit("\n".join(lines))
    return 0 if not failed and not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
