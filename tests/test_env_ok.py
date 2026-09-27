"""Plan 6.2.3: per-task validity gate (baseline fails, gold passes the whole target test files)."""
from __future__ import annotations

import json
from unittest import mock

from gemma_agent.tasks.pools import build_pools
from gemma_agent.tasks.validate import classify, main, validate_task
from test_agent_harness import WorkspaceCase

GOLD = """--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""
WRONG = GOLD.replace("return 2", "return 3")
# The scratch venv's base interpreter may lack pytest; the whole-file pytest command is exercised on Kaggle.
RUN = "python -c 'import test_sample; test_sample.test_value()'"


class EnvOkTests(WorkspaceCase):
    def task(self, patch: str) -> dict:
        return {"instance_id": "synthetic_1", "repo": "example/repo", "problem_statement": "Make value return two.",
                "patch": patch, "test_patch": self.test_patch.read_text()}

    def test_gold_fix_is_env_ok(self):
        row = validate_task(self.task(GOLD), self.snapshot, self.root / "work", command=RUN)
        self.assertTrue(row["env_ok"], row)
        self.assertEqual(row["targets"], ["test_sample.py"])
        self.assertFalse(row["baseline"]["resolved"])
        self.assertTrue(row["gold"]["resolved"])
        self.assertFalse((self.root / "work" / "synthetic_1" / "control").exists())

    def test_wrong_gold_and_unapplicable_gold_are_rejected(self):
        self.assertEqual(validate_task(self.task(WRONG), self.snapshot, self.root / "w1", command=RUN)["reason"], "gold_fails")
        broken = GOLD.replace("return 1", "return 9")
        self.assertEqual(validate_task(self.task(broken), self.snapshot, self.root / "w2", command=RUN)["reason"],
                         "gold_setup_error")

    def test_cli_is_resumable(self):
        tasks = self.root / "tasks.jsonl"
        tasks.write_text(json.dumps(self.task(GOLD)) + "\n")
        snapshots = self.root / "snaps"
        snapshots.mkdir()
        (snapshots / "synthetic_1.tgz").write_bytes(self.snapshot.read_bytes())
        out = self.root / "env_ok.jsonl"
        args = [str(tasks), str(snapshots), str(out), "--work", str(self.root / "cli")]
        with mock.patch("gemma_agent.tasks.validate.pytest_command", return_value=RUN):
            main(args)
            main(args)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([(r["task_id"], r["env_ok"]) for r in rows], [("synthetic_1", True)])


def test_classify_accepts_any_baseline_failure_once_gold_passes():
    gold_ok = {"resolved": True}
    assert classify({"resolved": False, "infrastructure_error": True}, gold_ok) == "ok"  # ImportError of new symbol
    assert classify({"resolved": True}, gold_ok) == "baseline_passes"
    assert classify({"resolved": False}, {"resolved": False, "infrastructure_error": True}) == "gold_infra_error"


def test_pools_restricted_to_env_ok_tasks():
    tasks = [{"instance_id": f"a_{i}", "repo": "a/a"} for i in range(50)] + [{"instance_id": "rich_4076", "repo": "b/b"}]
    env_ok = {t["instance_id"]: {"env_ok": not t["instance_id"].endswith("7")} for t in tasks}
    pools = build_pools(tasks, env_ok=env_ok)
    assert "fast-40" in pools and "public-ok" in pools and "fast-40-ok" in pools
    assert all(not i.endswith("7") for i in pools["fast-40-ok"] + pools["public-ok"])
    assert "rich_4076" in pools["fast-40-ok"]
