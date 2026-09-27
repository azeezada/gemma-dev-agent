"""Plan 6.2.2: token-level recording, renderer parity and SFT sequence assembly."""
from __future__ import annotations

import json
from pathlib import Path

from gemma_agent.data.sft import build_dataset, new_files, run_record, select_runs
from gemma_agent.data.tokens import build_sequences, load_turns, parity_report
from gemma_agent.rollout import run_task
from test_agent_harness import FakeClient, WorkspaceCase, tool_response
from test_train_harness import EDIT

ROOT = Path(__file__).resolve().parents[1]
GEN = "<assistant>"


class CharTokenizer:
    """Chat template stand-in: one token per character; the generation prompt opens the assistant turn."""

    def __init__(self, keep_reasoning: bool = True):
        self.keep_reasoning = keep_reasoning

    def render_message(self, message: dict) -> str:
        body = message.get("content") or ""
        if message["role"] == "assistant":
            reasoning = message.get("reasoning") if self.keep_reasoning else ""
            calls = json.dumps(message.get("tool_calls") or [], sort_keys=True)
            return f"<{message['role']}>{reasoning or ''}|{body}|{calls}</>"
        return f"<{message['role']}>{body}</>"

    def apply_chat_template(self, messages, tools=None, tokenize=True, add_generation_prompt=False, **kwargs):
        text = json.dumps(tools, sort_keys=True) + "".join(self.render_message(m) for m in messages)
        text += GEN if add_generation_prompt else ""
        return [ord(c) for c in text] if tokenize else text


class TokenClient(FakeClient):
    """Fake vLLM that returns prompt/completion token ids rendered by `server_tokenizer`."""

    def __init__(self, responses, server_tokenizer):
        super().__init__(responses)
        self.server_tokenizer = server_tokenizer

    def chat(self, messages, **settings):
        from gemma_agent.env.harness import template_messages
        response = json.loads(json.dumps(super().chat(messages, **settings)))
        tokenizer = self.server_tokenizer
        prompt = tokenizer.apply_chat_template(template_messages(messages), tools=self.tool_definitions,
                                               add_generation_prompt=True)
        history = template_messages([{**response["choices"][0]["message"], "role": "assistant"}])[0]
        response["prompt_token_ids"] = prompt
        response["choices"][0]["token_ids"] = [ord(c) for c in tokenizer.render_message(history)[len(GEN):]]
        return response


class TokenRecordingTests(WorkspaceCase):
    submission = ROOT / "harnesses" / "train_v1"

    def _run(self, server_tokenizer):
        task, verification, config = self.specifications()
        client = TokenClient([
            tool_response("run_command", {"command": EDIT}, "edit-1"),
            tool_response("submit_patch", {}, "submit-1"),
        ], server_tokenizer)
        return run_task(task, verification, config, client=client)

    def test_turns_record_vllm_ids_and_renderer_matches(self):
        result = self._run(CharTokenizer())
        turns = load_turns(result.run_directory)
        self.assertEqual(len(turns), 2)
        self.assertTrue(all(t["prompt_token_ids"] and t["completion_token_ids"] for t in turns))
        report = parity_report(CharTokenizer(), [result.run_directory])
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["prefix_consistent_rate"], 1.0)
        sequences = build_sequences(turns)
        self.assertEqual(len(sequences), 1)
        self.assertEqual(sequences[0]["turns"], 2)
        self.assertEqual(sequences[0]["input_ids"], turns[1]["prompt_token_ids"] + turns[1]["completion_token_ids"])

    def test_renderer_drift_is_reported(self):
        result = self._run(CharTokenizer(keep_reasoning=False))
        report = parity_report(CharTokenizer(), [result.run_directory])
        self.assertFalse(report["passed"])
        self.assertEqual(report["exact_matches_by_variant"][0]["matches"], 1)  # first prompt has no history
        self.assertIsNotNone(report["mismatches"][0]["first_difference"])

    def test_passing_run_is_selected_into_dataset(self):
        result = self._run(CharTokenizer())
        record = run_record(result.run_directory)
        self.assertEqual(record["outcome"], "PASS")
        selected, rejected = select_runs([record])
        self.assertEqual([r["run"] for r in selected], [record["run"]], rejected)
        rows = build_dataset(selected, contract_id="train_v1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_id"], "train_v1")
        self.assertEqual(len(rows[0]["input_ids"]), len(rows[0]["loss_mask"]))


def test_inconsistent_prefix_splits_sequences():
    turns = [
        {"agent": "a", "prompt_token_ids": [1, 2], "completion_token_ids": [3, 4]},
        {"agent": "a", "prompt_token_ids": [1, 2, 3, 4, 5], "completion_token_ids": [6]},
        {"agent": "a", "prompt_token_ids": [1, 2, 9, 5, 6, 7], "completion_token_ids": [8]},  # history re-rendered
    ]
    seqs = build_sequences(turns)
    assert [s["input_ids"] for s in seqs] == [[1, 2, 3, 4, 5, 6], [1, 2, 9, 5, 6, 7, 8]]
    assert seqs[0]["loss_mask"] == [0, 0, 1, 1, 0, 1]
    assert build_sequences(turns, max_len=6)[0]["input_ids"] == [1, 2, 3, 4, 5, 6]
    assert len(build_sequences(turns, max_len=6)) == 1


def test_filters_reject_failed_and_scratch_runs_and_fallback_keeps_pipeline_alive():
    base = {"task_id": "t", "outcome": "PASS", "explicit_submit": True, "malformed_calls": 0,
            "max_context_utilization": 0.5, "completion_tokens": 100, "scratch_files": [], "turns": 2,
            "turns_with_ids": 2, "leak_audit_passed": True}
    records = [
        {**base, "run": "ok_long", "completion_tokens": 900},
        {**base, "run": "ok_short"},
        {**base, "run": "ok_mid", "completion_tokens": 500},
        {**base, "run": "fail", "outcome": "FAIL"},
        {**base, "run": "scratch", "scratch_files": ["repro.py"]},
    ]
    selected, rejected = select_runs(records, per_task=2)
    assert [r["run"] for r in selected] == ["ok_mid", "ok_short"]
    assert rejected == {"fail": ["outcome=FAIL"], "scratch": ["scratch_files"]}
    fallback, _ = select_runs([records[3]], fallback=True)
    assert [r["run"] for r in fallback] == ["fail"] and fallback[0]["fallback"]
    assert select_runs([records[3]])[0] == []


def test_new_files_parses_created_paths():
    patch = "--- /dev/null\n+++ b/repro.py\n@@ -0,0 +1 @@\n+x\n--- a/pkg/m.py\n+++ b/pkg/m.py\n"
    assert new_files(patch) == ["repro.py"]


class AdapterRoutingTests(WorkspaceCase):
    """A submission with `adapter:` is served under the adapter name, as the competition's LiteLLM routing does."""

    def test_adapter_submission_requests_adapter_model(self):
        from gemma_agent.eval.package import stage_submission
        from test_package_parity import write_adapter

        stage = stage_submission(ROOT / "harnesses" / "train_v1", self.root / "stage",
                                 {"mvp_lora": write_adapter(self.root / "adapter")}, "mvp_lora")
        self.submission = stage
        task, verification, config = self.specifications()
        seen = []

        class ModelClient(FakeClient):
            model = "gemma-4-31b-it-qat-w4a16-ct"

            def chat(self, messages, **settings):
                seen.append(self.model)
                return super().chat(messages, **settings)

        client = ModelClient([tool_response("run_command", {"command": EDIT}, "e"), tool_response("submit_patch", {}, "s")])
        result = run_task(task, verification, config, client=client)
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(seen, ["mvp_lora", "mvp_lora"])
        self.assertEqual(client.model, "gemma-4-31b-it-qat-w4a16-ct")
