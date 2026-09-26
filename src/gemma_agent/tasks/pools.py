"""Eval pools (plan section 7): `public-129` (all public tasks), `fast-N` (stratified, weekly iteration) and
`gut-check` (`rich_4076`, the smoke task for every pipeline change).

Pools are lists of instance ids, deterministic given the task file, and written under `data/pools/` (gitignored:
they derive from competition data). Usage: `python -m gemma_agent.tasks.pools data/tasks.jsonl data/pools`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

GUT_CHECK = ("rich_4076",)
FAST_SIZE = 40


def load_tasks(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _rank(instance_id: str) -> str:
    return hashlib.sha256(instance_id.encode()).hexdigest()


def fast_pool(tasks: list[dict], size: int = FAST_SIZE, must_include: tuple[str, ...] = GUT_CHECK) -> list[str]:
    """Stratified by repo with largest-remainder quotas (every repo gets >= 1); ties broken by a fixed hash."""
    by_repo: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        by_repo[task["repo"]].append(task["instance_id"])
    size = min(size, len(tasks))
    total = len(tasks)
    quota = {repo: max(1, int(size * len(ids) / total)) for repo, ids in by_repo.items()}
    remainders = sorted(by_repo, key=lambda r: (-(size * len(by_repo[r]) / total - int(size * len(by_repo[r]) / total)), r))
    while sum(quota.values()) < size:
        for repo in remainders:
            if sum(quota.values()) >= size:
                break
            if quota[repo] < len(by_repo[repo]):
                quota[repo] += 1
    while sum(quota.values()) > size:
        repo = max(quota, key=lambda r: (quota[r], r))
        quota[repo] -= 1
    chosen: list[str] = []
    for repo, ids in sorted(by_repo.items()):
        pinned = [i for i in must_include if i in ids]
        rest = sorted((i for i in ids if i not in pinned), key=_rank)
        chosen += (pinned + rest)[: max(quota[repo], len(pinned))]
    return sorted(chosen)


def build_pools(tasks: list[dict]) -> dict[str, list[str]]:
    ids = sorted(task["instance_id"] for task in tasks)
    missing = [i for i in GUT_CHECK if i not in ids]
    if missing:
        raise ValueError(f"gut-check task missing from task file: {missing}")
    return {"public-129": ids, f"fast-{FAST_SIZE}": fast_pool(tasks), "gut-check": list(GUT_CHECK)}


def main(argv: list[str]) -> int:
    tasks_path, out_dir = Path(argv[1]), Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, ids in build_pools(load_tasks(tasks_path)).items():
        (out_dir / f"{name}.json").write_text(json.dumps({"pool": name, "n": len(ids), "task_ids": ids}, indent=1) + "\n")
        print(f"{name}: {len(ids)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
