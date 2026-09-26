from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.trace_viewer import build_index, parse_patch

GOLD_PATCH = "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,3 +1,3 @@\n def area(r):\n-    return r * r\n+    return 3.14159 * r * r\n"
TEST_PATCH = "--- /dev/null\n+++ b/tests/test_core.py\n@@ -0,0 +1,2 @@\n+def test_area():\n+    assert area(1) > 3\n"


class RunBuilder:
    """Writes a minimal sealed-run directory in the harness's on-disk format."""

    def __init__(self, root: Path, run_id: str, task_id: str, outcome: str) -> None:
        self.root, self.run_id, self.task_id, self.outcome = root, run_id, task_id, outcome
        self.events: list[dict] = []
        (root / "artifacts" / "blobs").mkdir(parents=True)

    def blob(self, purpose: str, payload, suffix: str = "json") -> dict:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        digest = hashlib.sha256(data.encode()).hexdigest()
        rel = f"artifacts/blobs/{digest}.{suffix}"
        (self.root / rel).write_text(data)
        return {"purpose": purpose, "relative_path": rel, "byte_length": len(data), "media_type": "application/json", "sha256": digest}

    def event(self, event_type: str, attributes: dict | None = None, refs: list | None = None, status: str = "ok", **extra) -> None:
        seq = len(self.events) + 1
        self.events.append({"event_type": event_type, "sequence": seq, "monotonic_ns": seq * 1_000_000_000,
                            "attributes": attributes or {}, "artifact_refs": refs or [], "status": status,
                            "duration_ms": extra.get("duration_ms"), "budget_after": extra.get("budget_after"),
                            "run_id": self.run_id, "task_id": self.task_id})

    def turn(self, n: int, tool: str, arguments: dict, result: dict, *, reasoning: str = "", error: str | None = None, changed: bool = False) -> None:
        corr, call_id = f"model-{n}", f"call-{n}"
        self.event("context_measurement", {"context_window": 1000, "utilization": 0.1 * n})
        self.event("model_request", {"correlation_id": corr, "turn": n, "attempt": 0},
                   [self.blob("complete outbound model messages", [{"role": "system", "content": "sys"}] + [{"role": "user", "content": f"m{i}"} for i in range(n)])])
        response = {"choices": [{"message": {"content": None, "reasoning": reasoning,
                                             "tool_calls": [{"id": call_id, "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}], "model": "fake"}
        self.event("model_response", {"correlation_id": corr, "turn": n, "finish_reason": "tool_calls", "usage": {"prompt_tokens": 100 * n, "completion_tokens": 10}},
                   [self.blob("raw model response", response)], budget_after={"tool_calls_used": n - 1, "max_tool_calls": 40})
        self.event("tool_call", {"correlation_id": call_id, "tool": tool, "working_directory": "/ws/solver/workspace"},
                   [self.blob(f"tool arguments: {tool}", arguments)])
        self.event("tool_result", {"correlation_id": call_id, "tool": tool, "error_type": error, "workspace_changed": changed},
                   [self.blob(f"model-visible tool result: {tool}", result)], status="error" if error else "ok")

    def seal(self, patch: str, termination: str = "submitted_patch") -> None:
        (self.root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in self.events) + "\n")
        (self.root / "final").mkdir(exist_ok=True)
        (self.root / "final" / "agent.patch").write_text(patch)
        (self.root / "final" / "summary.json").write_text(json.dumps({"outcome": self.outcome, "termination_reason": termination,
                                                                      "agent": {"model_calls": 2, "patch_submitted_explicitly": True, "budget": {"tool_calls_used": 2, "max_tool_calls": 40}}}))
        (self.root / "run_manifest.json").write_text(json.dumps({"run_id": self.run_id, "task_id": self.task_id, "experiment_id": "exp", "outcome": self.outcome,
                                                                 "trace_validation": {"valid": True}}))


class TraceViewerIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        task = {"instance_id": "pkg_1", "repo": "org/pkg", "base_commit": "abc", "patch": GOLD_PATCH, "test_patch": TEST_PATCH,
                "problem_statement": "Area is wrong\n\nFix it.", "hints_text": "", "created_at": "2026-01-01"}
        (self.project / "data").mkdir()
        (self.project / "data" / "tasks.jsonl").write_text(json.dumps(task) + "\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_dir(self, version: str, run_id: str, task_id: str = "pkg_1", outcome: str = "FAIL") -> RunBuilder:
        return RunBuilder(self.project / "work" / version / "trace_runs" / "exp" / run_id, run_id, task_id, outcome)

    def load(self, name: str):
        return json.loads((self.project / "work" / ".trace_viewer" / name).read_text())

    def test_turns_join_model_io_tools_and_gold_overlap(self) -> None:
        run = self.run_dir("kaggle-trace-v1", "pkg_1-a", outcome="PASS")
        run.turn(1, "read_file", {"filepath": "/ws/solver/workspace/pkg/core.py"}, {"status": "ok", "content": "def area"}, reasoning="look at core")
        run.turn(2, "edit_file", {"filepath": "pkg/core.py", "old_string": "r * r", "new_string": "3.14159 * r * r"}, {"status": "ok"}, changed=True)
        run.event("verification_result", {"phase": "patched", "resolved": True})
        run.seal(GOLD_PATCH)
        build_index(self.project, quiet=True)

        catalog = self.load("catalog.json")
        self.assertEqual([r["outcome"] for r in catalog["runs"]], ["PASS"])
        doc = self.load(f"runs/{catalog['runs'][0]['key']}.json")
        self.assertEqual(len(doc["turns"]), 2)
        first = doc["turns"][0]
        self.assertEqual(first["response"]["reasoning"], "look at core")
        self.assertEqual(first["tool_calls"][0]["file"], "pkg/core.py")
        self.assertEqual(first["tool_calls"][0]["result"]["model_visible"]["content"], "def area")
        self.assertEqual(len(doc["turns"][1]["new_messages"]), 1)  # only the message added since the previous request
        self.assertEqual(doc["files_edited"], ["pkg/core.py"])
        self.assertEqual(doc["gold_overlap"]["patched"], ["pkg/core.py"])
        self.assertEqual(doc["gold_overlap"]["first_seen_step"], 0)
        self.assertIn("patched", doc["verification"])
        self.assertEqual(self.load("tasks/pkg_1.json")["test_files"], ["tests/test_core.py"])

    def test_unknown_event_types_outcomes_and_versions_pass_through(self) -> None:
        run = self.run_dir("kaggle-trace-v99", "pkg_1-b", outcome="NEW_OUTCOME")
        run.turn(1, "brand_new_tool", {"query": "x"}, {"status": "ok"})
        run.event("custom.probe", {"anything": 1})
        run.seal("")
        build_index(self.project, quiet=True)

        catalog = self.load("catalog.json")
        self.assertEqual(catalog["versions"], ["kaggle-trace-v99"])
        self.assertIn("NEW_OUTCOME", catalog["outcomes"])
        self.assertIn("brand_new_tool", catalog["tools"])
        doc = self.load(f"runs/{catalog['runs'][0]['key']}.json")
        self.assertIn("custom.probe", [e["type"] for e in doc["lifecycle"]])
        self.assertIn("empty_patch", catalog["runs"][0]["flags"])

    def test_behavior_flags(self) -> None:
        run = self.run_dir("kaggle-trace-v1", "pkg_1-c")
        for n in (1, 2, 3):
            run.turn(n, "edit_file", {"filepath": "other.py", "old_string": "a", "new_string": "b"}, {"status": "error"}, error="EditError")
        run.turn(4, "write_file", {"filepath": "tmp/repro.py", "content": "print(1)"}, {"status": "ok"}, changed=True)
        run.seal("--- /dev/null\n+++ b/tmp/repro.py\n@@ -0,0 +1 @@\n+print(1)\n")
        build_index(self.project, quiet=True)

        flags = set(self.load("catalog.json")["runs"][0]["flags"])
        self.assertTrue({"failed_edits", "error_streak", "scratch_files_in_patch", "never_saw_gold_file", "no_check_after_edit"} <= flags, flags)
        doc = self.load(f"runs/{self.load('catalog.json')['runs'][0]['key']}.json")
        self.assertEqual(doc["files_edit_failed"], ["other.py"])  # failed edits are not counted as edits

    def test_stale_run_documents_are_removed(self) -> None:
        run = self.run_dir("kaggle-trace-v1", "pkg_1-d")
        run.seal("")
        build_index(self.project, quiet=True)
        stale = self.project / "work" / ".trace_viewer" / "runs" / "gone.json"
        stale.write_text("{}")
        build_index(self.project, quiet=True)
        self.assertFalse(stale.exists())


class ParsePatchTests(unittest.TestCase):
    def test_git_and_plain_diffs(self) -> None:
        files = parse_patch("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@ def f():\n-a\n+b\n c\n" + TEST_PATCH)
        self.assertEqual([(f["path"], f["status"], f["added"], f["removed"]) for f in files],
                         [("x.py", "modified", 1, 1), ("tests/test_core.py", "added", 2, 0)])
        self.assertEqual(files[0]["hunks"][0]["header"], "def f():")


if __name__ == "__main__":
    unittest.main()
