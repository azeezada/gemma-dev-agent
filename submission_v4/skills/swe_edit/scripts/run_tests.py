"""Bounded focused pytest runs; compare failures with a separate clean checkout."""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from apply_edit import parse_args, workspace_root

OUTPUT_LIMIT = 2800


def run_pytest(root, targets, keyword, timeout):
    command = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=short", "--color=no",
               "-p", "no:cacheprovider", *targets]
    if keyword:
        command += ["-k", keyword]
    env = dict(os.environ)
    # A .pth installed for the edited tree must not override the clean baseline.
    env["PYTHONPATH"] = os.pathsep.join([str(Path(root) / "src"), str(root)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                text=True, timeout=max(0.1, timeout))
        output = result.stdout + "\n" + result.stderr
        code = result.returncode
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or b"") + (error.stderr or b"")
        output = output.decode(errors="replace") if isinstance(output, bytes) else output
        return {"returncode": 124, "failed": {}, "timed_out": True, "output": output,
                "summary": "TIMEOUT: narrow to relevant test nodes; do not repeat this target unchanged."}
    failed = {}
    for line in output.splitlines():
        match = re.match(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", line.strip())
        if match:
            failed[match[2]] = (match[1], match[3] or "")
    summaries = [line.strip("= ") for line in output.splitlines()
                 if re.search(r"\b(passed|failed|errors?|skipped|deselected|no tests ran)\b", line) and " in " in line]
    return {"returncode": code, "failed": failed, "timed_out": False, "output": output,
            "summary": summaries[-1] if summaries else f"pytest exit {code}"}


def clean_baseline(root, destination):
    result = subprocess.run(["git", "archive", "HEAD"], cwd=root, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[:300])
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(destination, filter="data")


def main(argv):
    args, positional = parse_args(argv)
    raw = args.get("targets", args.get("target", ""))
    # Accept ADK list repr as well as a space-separated target string.
    import ast
    if raw.startswith("["):
        targets = list(ast.literal_eval(raw))
    else:
        import shlex
        targets = shlex.split(raw)
    targets += positional
    if not targets:
        print("ERROR: provide --targets with focused test files or node ids")
        return 2
    try:
        timeout = max(1, min(60, float(args.get("timeout", 20))))
    except ValueError:
        print("ERROR: timeout must be numeric")
        return 2
    started = time.monotonic()
    root = workspace_root()
    result = run_pytest(root, targets, args.get("k", ""), timeout)
    lines = [result["summary"]]
    if result["returncode"] == 5:
        lines.append("NO TESTS SELECTED: use a real test node name from source or --collect-only. This is not a pass.")
    labels = {}
    if result["failed"] and args.get("baseline", "false").lower() in {"true", "1", "yes"}:
        remaining = timeout - (time.monotonic() - started)
        if remaining < 1:
            lines.append("BASELINE UNKNOWN: shared test timeout exhausted")
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="swe-baseline-") as clean:
                    clean_baseline(root, clean)
                    # Only nodes present in the clean tree can be compared.
                    nodes = [node for node in result["failed"]
                             if not os.path.isabs(node.split("::")[0]) and Path(clean, node.split("::")[0]).is_file()]
                    if nodes:
                        before = run_pytest(clean, nodes, "", timeout - (time.monotonic() - started))
                        for node in nodes:
                            if before["timed_out"] or before["returncode"] not in (0, 1):
                                labels[node] = "baseline inconclusive"
                            elif node in before["failed"]:
                                labels[node] = "already failing before your change"
                            elif before["returncode"] == 0:
                                labels[node] = "NEW: introduced by your change (or intentionally changed contract)"
                            else:
                                labels[node] = "baseline inconclusive"
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                lines.append("BASELINE UNKNOWN: " + str(error)[:180])
    for node, (kind, message) in list(result["failed"].items())[:6]:
        lines.append(f"{kind} {node} [{labels.get(node, 'not compared')}]\n  {message[:230]}")
    if result["returncode"]:
        # The short traceback includes the actual assertion, not just its name.
        evidence = [line for line in result["output"].splitlines()
                    if line.startswith(("E ", "> ")) or "fixture" in line or "ImportError" in line]
        lines.extend(evidence[:10])
        if not result["failed"] and not evidence:
            lines.append(result["output"][-700:])
    print("\n".join(lines)[:OUTPUT_LIMIT])
    return 0 if result["returncode"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
