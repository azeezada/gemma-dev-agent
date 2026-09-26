from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from gemma_agent.rollout.evaluation import derive_metrics
from gemma_agent.rollout.reporting import validate_trace
from gemma_agent.rollout.runtime import run_task
from gemma_agent.rollout.schemas import RunConfig, SchemaError, TaskSpec, VerificationSpec
from gemma_agent.rollout.tracing import TraceRecorder
from gemma_agent.env.harness import ModelHTTPError, configure_workspace


PROJECT = Path(__file__).resolve().parents[1]


def tool_response(name: str, arguments: dict, identity: str) -> dict:
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "reasoning": "Use repository evidence.",
                "tool_calls": [{
                    "id": identity,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.offered = []
        self.tokenizer = None

    def count_tokens(self, messages):
        return len(json.dumps(messages)) // 4

    def chat(self, messages, **settings):
        offered = [tool["function"]["name"] for tool in getattr(self, "tool_definitions", None) or []]
        self.requests.append((json.loads(json.dumps(messages)), settings))
        self.offered.append(offered)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def assistant_history(self, message):
        return {**message, "role": "assistant"}


class AgentHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "source"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "sample.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        configure_workspace(repo)
        self.snapshot = self.root / "snapshot.tgz"
        with tarfile.open(self.snapshot, "w:gz") as archive:
            archive.add(repo, arcname=".")
        self.test_patch = self.root / "private-tests.patch"
        self.test_patch.write_text(
            """--- /dev/null
+++ b/test_sample.py
@@ -0,0 +1,4 @@
+from sample import value
+
+def test_value():
+    assert value() == 2
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def specifications(self):
        task = TaskSpec.from_dict({
            "schema_version": "1.0",
            "task_id": "synthetic_1",
            "repository": "example/repo",
            "problem_statement": "Make value return two.",
            "hints": "Inspect sample.py.",
            "snapshot_path": str(self.snapshot),
        })
        verification = VerificationSpec.from_dict({
            "schema_version": "1.0",
            "task_id": "synthetic_1",
            "test_patch_path": str(self.test_patch),
            "verification_command": "python -c 'from sample import value; assert value() == 2'",
        })
        config = RunConfig.from_dict({
            "schema_version": "1.0",
            "submission_directory": str(PROJECT / "harnesses" / "v3"),
            "output_root": str(self.root / "runs"),
            "work_root": str(self.root / "work"),
            "experiment_name": "unit",
            "seed": 42,
        })
        return task, verification, config

    def test_schema_rejects_unknown_and_private_task_fields(self):
        for path in (PROJECT / "src" / "gemma_agent" / "rollout" / "json_schemas").glob("*.json"):
            self.assertIsInstance(json.loads(path.read_text()), dict)
        with self.assertRaises(SchemaError):
            TaskSpec.from_dict({
                "schema_version": "1.0", "task_id": "x", "repository": "x/y",
                "problem_statement": "x", "snapshot_path": str(self.snapshot),
                "test_patch": "private",
            })

    def test_complete_run_is_replayable_and_verified(self):
        task, verification, config = self.specifications()
        client = FakeClient([
            tool_response(
                "run_skill_script",
                {"skill_name": "swe_edit", "file_path": "scripts/apply_edit.py",
                 "args": {"file": "sample.py", "old": "return 1", "new": "return 2"}},
                "edit-1",
            ),
            tool_response("submit_patch", {}, "submit-1"),
        ])
        result = run_task(task, verification, config, client=client)
        self.assertTrue(result.completed)
        self.assertEqual(result.outcome, "PASS")
        self.assertIn("return 2", result.patch_path.read_text())
        validation = validate_trace(result.run_directory)
        self.assertTrue(validation["valid"], validation)
        self.assertTrue((result.run_directory / "trace.atif.json").is_file())
        self.assertTrue((result.run_directory / "timeline.html").is_file())
        self.assertTrue((result.run_directory / "run_manifest.json").is_file())
        self.assertEqual(
            len(list((result.run_directory / "workspace" / "diffs").glob("*.patch"))), 1
        )
        self.assertNotIn(self.test_patch.read_text(), (result.run_directory / "events.jsonl").read_text())
        metrics = derive_metrics(result.run_directory)
        self.assertEqual(metrics["outcome"]["classification"], "PASS")
        self.assertEqual(metrics["components"]["tool_calls"], 2)

        skill_scripts = PROJECT / "skills" / "agent-trace-evaluator" / "scripts"
        created = subprocess.check_output(
            [
                sys.executable, str(skill_scripts / "init_evaluation.py"),
                str(result.run_directory), "--teacher-model", "fixture-teacher",
            ],
            text=True,
        ).strip()
        evaluation_root = Path(created)
        evaluation = json.loads((evaluation_root / "evaluation.json").read_text())
        evidence = {
            "evidence_id": "E1",
            "claim": "The frozen patch passed against a failing baseline.",
            "event_sequences": [1],
            "span_ids": [json.loads((result.run_directory / "events.jsonl").read_text().splitlines()[0])["span_id"]],
            "artifact_refs": [],
        }
        (evaluation_root / "evidence.jsonl").write_text(json.dumps(evidence) + "\n")
        for phase in evaluation["trajectory"]["phases"].values():
            phase.update({"score": 3, "rationale": "Supported by the recorded run.", "evidence_ids": ["E1"]})
        evaluation["trajectory"].update({"weighted_score": 75.0, "observable_weight": 1.0})
        evaluation["teacher"]["confidence"] = 0.8
        evaluation["failure_attribution"].update({
            "primary": "NONE", "secondary": [], "rationale": "Verified success.", "evidence_ids": ["E1"],
        })
        (evaluation_root / "evaluation.json").write_text(json.dumps(evaluation, indent=2))
        (evaluation_root / "evaluation.md").write_text("# Evaluation\n\nVerified success.\n")
        validated = subprocess.run(
            [sys.executable, str(skill_scripts / "validate_evaluation.py"), str(evaluation_root)],
            text=True, capture_output=True,
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_trace_detects_artifact_tampering(self):
        root = self.root / "partial"
        trace = TraceRecorder(root, experiment_id="e", run_id="r", task_id="t")
        ref = trace.artifacts.put_text("payload", purpose="test")
        trace.emit("test", artifact_refs=[ref])
        trace.finish(status="COMPLETE", termination_reason="done", outcome="FAIL")
        (root / ref.relative_path).write_text("tampered")
        validation = validate_trace(root)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("hash mismatch" in error for error in validation["errors"]))

    def test_partial_trace_is_valid_when_explicitly_allowed(self):
        root = self.root / "partial"
        TraceRecorder(root, experiment_id="e", run_id="r", task_id="t")
        result = validate_trace(root, require_complete=False)
        self.assertTrue(result["valid"], result)
        self.assertFalse(result["complete"])

    def test_server_context_error_compacts_and_retries(self):
        task, verification, config = self.specifications()
        overflow = ModelHTTPError(
            400, "This model's maximum context length is 32768 tokens. Please reduce the length"
        )
        reads = [
            tool_response("read_file", {"filepath": "sample.py"}, f"read-{index}")
            for index in range(3)
        ]
        client = FakeClient([
            *reads,
            overflow,
            tool_response(
                "run_skill_script",
                {"skill_name": "swe_edit", "file_path": "scripts/apply_edit.py",
                 "args": {"file": "sample.py", "old": "return 1", "new": "return 2"}},
                "edit-1",
            ),
            tool_response("submit_patch", {}, "submit-1"),
        ])
        result = run_task(task, verification, config, client=client)
        self.assertEqual(result.outcome, "PASS")
        summary = json.loads((result.run_directory / "final" / "summary.json").read_text())
        self.assertEqual(summary["agent"]["termination_reason"], "submitted_patch")
        self.assertEqual(summary["agent"]["context_compactions"], 1)
        retried = client.requests[-2][0]
        self.assertIn("Context digest", json.dumps(retried))
        self.assertEqual(summary["verification"]["target_tests"]["names"], ["test_value"])

    def test_localize_agent_tool_runs_in_its_own_context_and_returns_ledger(self):
        task, verification, config = self.specifications()
        ledger = "LEDGER\n1. sample.py:2 value - returns 1; evidence: probe prints 1"
        client = FakeClient([
            tool_response("localize", {"request": "value() returns 1, expected 2"}, "loc-1"),
            # Inside the localizer: an edit attempt is rejected, then a read, then the ledger.
            tool_response("run_skill_script", {"skill_name": "swe_edit", "file_path": "scripts/apply_edit.py",
                                               "args": {"file": "sample.py", "old": "return 1", "new": "return 3"}},
                          "sub-edit"),
            tool_response("read_file", {"filepath": "sample.py"}, "sub-read"),
            {"choices": [{"finish_reason": "stop", "message": {"content": ledger, "reasoning": "done"}}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
            tool_response(
                "run_skill_script",
                {"skill_name": "swe_edit", "file_path": "scripts/apply_edit.py",
                 "args": {"file": "sample.py", "old": "return 1", "new": "return 2"}},
                "edit-1",
            ),
            tool_response("submit_patch", {}, "submit-1"),
        ])
        result = run_task(task, verification, config, client=client)
        self.assertEqual(result.outcome, "PASS")
        root_first, sub_first, sub_second, sub_final, root_after = (request[0] for request in client.requests[:5])
        # The sub-agent sees only its templated instruction and the request.
        self.assertEqual([message["role"] for message in sub_first], ["system", "user"])
        self.assertIn("Make value return two.", sub_first[0]["content"])
        self.assertEqual(sub_first[1]["content"], "value() returns 1, expected 2")
        self.assertIn("Skill 'swe_edit' not found", json.dumps(sub_second[-1]))
        self.assertIn("localize", client.offered[0])
        self.assertNotIn("submit_patch", client.offered[1])
        self.assertNotIn("localize", client.offered[1])
        # The root gets the ledger as the tool result, without the sub-agent's reads.
        self.assertEqual(json.loads(root_after[-1]["content"]), {"result": ledger})
        self.assertNotIn("sub-read", json.dumps(root_after))
        summary = json.loads((result.run_directory / "final" / "summary.json").read_text())
        self.assertEqual(summary["agent"]["model_calls"], 6)
        self.assertEqual(summary["agent"]["budget"]["tool_calls_used"], 3)
        events = [json.loads(line) for line in (result.run_directory / "events.jsonl").read_text().splitlines()]
        transfers = [row["attributes"]["direction"] for row in events if row.get("event_type") == "agent_transfer"]
        self.assertEqual(transfers, ["enter", "exit"])
        self.assertTrue(validate_trace(result.run_directory)["valid"])


if __name__ == "__main__":
    unittest.main()
