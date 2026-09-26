"""Offline recall of the swe_locate find_candidates ranker against gold patches.

For every public task whose snapshot is available locally, extract search terms
mechanically from the problem statement (no model, no task-specific tuning), run
find_candidates.py, and report whether the gold patch's non-test source files
appear in the top-k ranked files.

Usage: python tools/eval_localization.py [--snapshots snapshots] [--limit 10] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "submission_v3" / "skills" / "swe_locate" / "scripts" / "find_candidates.py"
STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "when", "not", "are", "fix", "add",
    "use", "none", "true", "false", "self", "return", "should", "issue", "error", "fixes",
    "support", "allow", "make", "into", "have", "has", "was", "were", "which", "will",
}


def extract_terms(statement: str, limit: int = 8) -> list[str]:
    """Identifiers a reader would pick out: code spans, dotted names, snake/Camel case, calls."""
    statement = re.sub(r"https?://\S+|\S+\.(?:com|org|io|net)\S*", " ", statement)
    candidates: list[str] = []
    for span in re.findall(r"`([^`\n]{2,80})`", statement):
        candidates.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", span))
    candidates += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\(", statement)
    candidates += re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]{2,})\b", statement)
    candidates += re.findall(r"\b([a-z0-9]+(?:_[a-z0-9]+)+)\b", statement)
    candidates += re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", statement)
    terms: list[str] = []
    for term in candidates:
        if len(term) >= 3 and term.lower() not in STOPWORDS and term not in terms:
            terms.append(term)
    return terms[:limit]


def gold_source_files(patch: str) -> list[str]:
    files = []
    for path in re.findall(r"^\+\+\+ b/(\S+)", patch, re.M):
        name = path.rsplit("/", 1)[-1]
        if path.endswith(".py") and not (name.startswith("test_") or "/tests/" in f"/{path}"):
            files.append(path)
    return files


def ranked_files(output: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"^\d+\. (\S+)", output, re.M)]


def evaluate(task: dict, snapshot: Path, limit: int) -> dict:
    gold = gold_source_files(task["patch"])
    terms = extract_terms(task["problem_statement"])
    with tempfile.TemporaryDirectory(prefix="loc-") as tmp:
        root = Path(tmp)
        with tarfile.open(snapshot) as archive:
            archive.extractall(root, filter="data")
        if not (root / ".git").exists():
            for command in (["git", "init", "-q"], ["git", "add", "-A"],
                            ["git", "-c", "user.email=e@e", "-c", "user.name=e", "commit", "-qm", "base"]):
                subprocess.run(command, cwd=root, check=True, capture_output=True)
        output = ""
        if terms:
            output = subprocess.run(
                [sys.executable, str(SCRIPT), "--terms", " ".join(terms), "--limit", str(limit)],
                capture_output=True, text=True, timeout=120,
                env={"SWE_WORKSPACE": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
            ).stdout
    ranked = ranked_files(output)
    ranks = [ranked.index(path) + 1 for path in gold if path in ranked]
    return {"task_id": task["instance_id"], "terms": terms, "gold": gold, "ranked": ranked,
            "best_rank": min(ranks) if ranks else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshots", default=str(PROJECT / "snapshots"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json")
    options = parser.parse_args()
    tasks = [json.loads(line) for line in (PROJECT / "tasks.jsonl").open(encoding="utf-8")]
    rows = []
    for task in tasks:
        snapshot = Path(options.snapshots) / f"{task['instance_id']}.tgz"
        if not snapshot.is_file() or not gold_source_files(task["patch"]):
            continue
        row = evaluate(task, snapshot, options.limit)
        rows.append(row)
        print(f"{row['task_id']:16} best_rank={row['best_rank']!s:5} terms={row['terms']}")
    if not rows:
        print("no tasks with local snapshots")
        return 1
    for k in (1, 3, 5, options.limit):
        hit = sum(1 for row in rows if row["best_rank"] and row["best_rank"] <= k)
        print(f"recall@{k}: {hit}/{len(rows)} = {hit / len(rows):.2f}")
    no_terms = sum(1 for row in rows if not row["terms"])
    print(f"tasks with no extractable terms: {no_terms}/{len(rows)}")
    if options.json:
        Path(options.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
