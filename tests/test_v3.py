from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.harness import Budget, EventLogger, ModelHTTPError, OpenAIHTTPClient, WorkspaceTools, configure_workspace, run_process, verify_patch
from mvp_v2.v3 import load_submission, submission_request_settings, budget_from_submission, compact_context, run_agent_v3, classify_outcome, audit_trajectory, benchmark_v3

ROOT = Path(__file__).resolve().parents[1]


def config_files():
    root = ROOT / "harnesses" / "v3"
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".pyc" and not any(
            part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
    }


def tool_response(name, arguments, identity="call"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "", "reasoning": "Inspect evidence.", "tool_calls": [{
            "id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def count_tokens(self, messages):
        return len(json.dumps(messages)) // 4

    def chat(self, messages, **settings):
        self.requests.append((copy.deepcopy(messages), settings))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class V3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "agent"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "sample.py").write_text("def value():\n    return 1\n")
        configure_workspace(self.repo)
        self.logger = EventLogger(self.root / "diagnostics")
        loaded = load_submission(config_files())
        # The legacy loop is single-agent; exercise it with the root agent alone.
        self.config = {**loaded, "agent_tools": {},
                       "tools": [name for name in loaded["tools"] if name not in loaded["agent_tools"]]}
        self.tools = WorkspaceTools(self.repo, budget_from_submission(self.config), self.logger, python=Path(sys.executable))
        self.task = {"instance_id": "synthetic", "repo": "test/example", "problem_statement": "Return two", "hints_text": ""}

    def tearDown(self):
        self.temp.cleanup()

    def run_client(self, client, **kwargs):
        return run_agent_v3(client, self.task, self.config, self.tools, self.logger, **kwargs)

    def test_settings_come_from_submission_and_map_to_vllm(self):
        client = FakeClient([tool_response("submit_patch", {})])
        self.run_client(client)
        settings = client.requests[0][1]
        self.assertEqual(settings["thinking_token_budget"], 2048)
        self.assertEqual(settings["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(settings["temperature"], 1)
        self.assertEqual(settings["top_k"], 64)
        self.assertEqual(settings["seed"], 42)
        self.assertLessEqual(settings["request_timeout"], 300)
        self.assertEqual(self.tools.budget.max_tool_calls, 40)

    def test_disabled_thinking_maps_to_template_switch(self):
        for level, include in (("NONE", True), ("low", False)):
            config = {"sampling": {"temperature": 0.2, "top_p": 0.95, "top_k": 64, "seed": 1, "max_output_tokens": 512,
                                   "thinking_config": {"thinking_level": level, "thinking_budget": 1,
                                                       "include_thoughts": include}}}
            settings = submission_request_settings(config)
            self.assertEqual(settings["chat_template_kwargs"], {"enable_thinking": False})
            self.assertNotIn("thinking_token_budget", settings)
        files = config_files()
        files["configs/sampling.yaml"] = files["configs/sampling.yaml"].replace("include_thoughts: true", "include_thoughts: false")
        load_submission(files)
        files["configs/sampling.yaml"] = files["configs/sampling.yaml"].replace("include_thoughts: false", "include_thoughts: maybe")
        with self.assertRaises(ValueError):
            load_submission(files)

    def test_reject_unknown_fields_includes_and_duplicate_keys(self):
        for extra in ("\nunknown: true\n", "\nname: duplicate\n", "\ninstruction: !include ../secret.md\n"):
            files = config_files()
            files["agent.yaml"] += extra
            with self.assertRaises(ValueError):
                load_submission(files)
        files = config_files()
        files["configs/sampling.yaml"] += "\nresponse_schema: {}\n"
        with self.assertRaises(ValueError):
            load_submission(files)

    def test_patch_capture_is_lossless(self):
        content = "\n".join(f"value_{i} = {i}" for i in range(1200)) + "\n"
        (self.repo / "large.py").write_text(content)
        result = self.tools.submit_patch()
        expected = subprocess.check_output(["git", "diff", "HEAD"], cwd=self.repo).decode()
        self.assertGreater(result["patch_size"], 5000)
        self.assertEqual(self.tools.submitted_patch, expected)
        self.assertIn("value_1199", expected)

    def test_file_view_caps_oversized_first_line(self):
        (self.repo / "long.txt").write_text("x" * 25000)
        result = self.tools.read_file("long.txt")
        self.assertEqual(len(result["content"]), 10000)
        self.assertTrue(result["is_truncated"])

    def test_flexible_edit_preserves_nested_indentation(self):
        (self.repo / "sample.py").write_text("class Sample:\n    def value(self):\n        return 1\n")
        result = self.tools.edit_file("sample.py", "def value(self):\n    return 1", "    def value(self):\n        return 2")
        self.assertEqual(result["strategy"], "flexible")
        self.assertEqual((self.repo / "sample.py").read_text(), "class Sample:\n    def value(self):\n        return 2\n")

    def test_timeout_kills_child_and_preserves_full_log(self):
        child_file = self.root / "child.pid"
        command = f"sleep 30 & echo $! > {child_file}; wait"
        result = run_process(["/bin/bash", "-c", command], timeout=0.15)
        self.assertTrue(result["timed_out"])
        pid = int(child_file.read_text())
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        self.assertTrue(status.returncode != 0 or status.stdout.strip().startswith("Z"), status.stdout)
        result = run_process([sys.executable, "-c", "print('x'*12000)"], log_path=self.root / "full.log")
        self.assertTrue(result["output_truncated"])
        self.assertGreater((self.root / "full.log").stat().st_size, 12000)

    def test_malformed_tool_arguments_recover_without_stale_args(self):
        bad = tool_response("edit_file", {})
        bad["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
        client = FakeClient([bad, tool_response("submit_patch", {}, "done")])
        result = self.run_client(client)
        self.assertEqual(result["termination_reason"], "submitted_patch")
        self.assertIn("ToolArgumentsJSONError", json.dumps(client.requests[1][0]))
        self.assertTrue((self.logger.root / "transcript.md").is_file())

    def test_transient_retries_are_bounded(self):
        client = FakeClient([ModelHTTPError(503, "busy")] * 3)
        with patch("mvp_v2.v3.time.sleep"):
            result = self.run_client(client)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(result["termination_reason"], "model_error")

    def test_invalid_request_not_retried_and_patch_recovered(self):
        (self.repo / "sample.py").write_text("def value():\n    return 2\n")
        client = FakeClient([ModelHTTPError(400, "bad request")])
        result = self.run_client(client)
        self.assertEqual(len(client.requests), 1)
        self.assertIn("+    return 2", result["patch"])
        self.assertTrue((self.logger.root / "transcript.md").is_file())

    def test_deadline_prevents_model_request(self):
        self.tools.budget.started_at -= 301
        client = FakeClient([])
        result = self.run_client(client)
        self.assertEqual(result["termination_reason"], "time_budget_exhausted")
        self.assertEqual(client.requests, [])

    def test_context_overflow_never_sent(self):
        client = FakeClient([])
        client.count_tokens = lambda messages: 33000
        result = self.run_client(client)
        self.assertEqual(result["termination_reason"], "context_capacity_exceeded")
        self.assertFalse(client.requests)

    def test_compaction_retains_complete_pairs_and_useful_facts(self):
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for index in range(6):
            messages.extend([
                {"role": "assistant", "content": "Check result", "tool_calls": [{"id": str(index), "function": {"name": "run_command", "arguments": '{"command":"test"}'}}]},
                {"role": "tool", "name": "run_command", "tool_call_id": str(index), "content": json.dumps({"status": "ok", "stdout": "BUG IN sample.py\n" + "x" * 3000 + "\n1 failed"})},
            ])
        compacted, changed = compact_context(messages)
        self.assertGreater(changed, 0)
        self.assertEqual(compacted[-4:], messages[-4:])
        self.assertIn("sample.py", compacted[2]["content"])
        self.assertIn("1 failed", compacted[2]["content"])
        again, changed = compact_context(compacted)
        self.assertEqual(changed, 0)
        self.assertEqual(again, compacted)

    def test_audit_checks_late_messages_and_snapshots_requests(self):
        self.logger.emit("model_request", messages=[{"role": "user", "content": "safe"}])
        self.logger.emit("model_request", messages=[{"role": "tool", "content": "SECRET_GOLD"}])
        audit = audit_trajectory({**self.task, "test_patch": "SECRET_GOLD"}, self.logger, {"requests_audited": 2})
        self.assertFalse(audit["passed"])
        value = [{"role": "user", "content": "before"}]
        self.logger.emit("model_request", messages=value)
        value[0]["content"] = "after"
        self.assertEqual(self.logger.rows[-1]["messages"][0]["content"], "before")

    def test_classification_requires_baseline_test_failure(self):
        agent = {"patch": "diff", "termination_reason": "submitted_patch"}
        self.assertEqual(classify_outcome(agent, {"resolved": True}, {"test_failure": True}), "PASS")
        self.assertEqual(classify_outcome(agent, {"resolved": True}, {"resolved": True}), "INCONCLUSIVE")
        self.assertEqual(classify_outcome(agent, {"resolved": True}, {"infrastructure_error": True}), "INFRA_ERROR")
        self.assertEqual(classify_outcome(agent, {"resolved": False}, {"test_failure": True}), "FAIL")

    def test_reasoning_is_preserved_only_for_tool_turns(self):
        client = OpenAIHTTPClient("http://localhost", "test")
        history = client.assistant_history(tool_response("get_status", {})["choices"][0]["message"])
        self.assertEqual(history["reasoning"], "Inspect evidence.")
        self.assertNotIn("reasoning", client.assistant_history({"content": "done", "reasoning": "secret"}))

    def test_fresh_baseline_and_patched_use_independent_environments(self):
        data = self.root / "data"
        (data / "snapshots").mkdir(parents=True)
        (data / "wheels").mkdir()
        snapshot = data / "snapshots" / "synthetic.tgz"
        with tarfile.open(snapshot, "w:gz") as archive:
            archive.add(self.repo, arcname=".")
        seen = []
        def fake_verify(**kwargs):
            seen.append(kwargs)
            return {"resolved": bool(kwargs["agent_patch"]), "test_failure": not kwargs["agent_patch"]}
        client = FakeClient([tool_response("edit_file", {"filepath": "sample.py", "old_string": "return 1", "new_string": "return 2"}), tool_response("submit_patch", {}, "done")])
        output = self.root / "run"
        output.mkdir()
        with patch("mvp_v2.v3.create_task_venv", return_value=Path(sys.executable)), patch("mvp_v2.v3.verify_patch", side_effect=fake_verify):
            summaries, audits = benchmark_v3(client=client, tasks={"synthetic": {**self.task, "test_patch": "SECRET_GOLD"}}, task_ids=("synthetic",),
                                             config=self.config, data_root=data, output_root=output, work_root=self.root / "work")
        self.assertEqual(summaries[0]["status"], "PASS")
        self.assertNotEqual(seen[0]["workspace"], seen[1]["workspace"])
        self.assertNotEqual(seen[0]["venv"], seen[1]["venv"])
        self.assertNotIn("SECRET_GOLD", json.dumps(client.requests))
        self.assertTrue(audits[0]["passed"])


if __name__ == "__main__":
    unittest.main()
