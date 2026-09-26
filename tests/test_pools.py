from gemma_agent.tasks.pools import GUT_CHECK, build_pools, fast_pool


def _tasks():
    repos = {"a/a": 60, "b/b": 45, "c/c": 13, "d/d": 1}
    tasks = [{"instance_id": f"{r.split('/')[0]}_{i}", "repo": r} for r, n in repos.items() for i in range(n)]
    tasks.append({"instance_id": "rich_4076", "repo": "b/b"})
    return tasks


def test_fast_pool_is_stratified_deterministic_and_pins_gut_check():
    tasks = _tasks()
    pool = fast_pool(tasks)
    assert len(pool) == 40 and len(set(pool)) == 40
    assert pool == fast_pool(list(reversed(tasks)))
    assert "rich_4076" in pool
    repos = {t["instance_id"]: t["repo"] for t in tasks}
    counts = {r: sum(repos[i] == r for i in pool) for r in set(repos.values())}
    assert all(counts[r] >= 1 for r in counts)
    assert counts["a/a"] > counts["b/b"] > counts["c/c"]


def test_build_pools_requires_gut_check():
    pools = build_pools(_tasks())
    assert pools["gut-check"] == list(GUT_CHECK) and len(pools["public-129"]) == len(_tasks())
    try:
        build_pools([{"instance_id": "x", "repo": "r"}])
    except ValueError:
        return
    raise AssertionError("expected ValueError")
