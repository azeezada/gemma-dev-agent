from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

from gemma_agent.rollout.runtime import run_task
from gemma_agent.rollout.schemas import RunConfig, TaskSpec, VerificationSpec
from gemma_agent.rollout.workflow import _contents
from gemma_agent.env.harness import configure_workspace
from test_agent_harness import tool_response

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "experiments" / "multi"))
from assemble import assemble  # noqa: E402


def text_response(text: str) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": text, "reasoning": "done"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10}}


def skill(name, script, args, identity):
    return tool_response("run_skill_script", {"skill_name": name, "file_path": f"scripts/{script}", "args": args},
                         identity)


class RouterClient:
    """Serves scripted responses per agent, identified by a marker in its system prompt."""

    def __init__(self, routes: dict[str, list[dict]]):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.requests: list[tuple[str, list]] = []
        self.settings: list[tuple[str, dict]] = []
        self.tokenizer = None
        self._lock = threading.Lock()

    def count_tokens(self, messages):
        return len(json.dumps(messages)) // 4

    def chat(self, messages, **settings):
        system = messages[0]["content"]
        with self._lock:
            for key, queue in self.routes.items():
                if key in system:
                    self.requests.append((key, json.loads(json.dumps(messages))))
                    self.settings.append((key, settings))
                    return queue.pop(0)
        raise AssertionError("No route for system prompt: " + system[:120])

    def assistant_history(self, message):
        return {**message, "role": "assistant"}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "source"
        (repo / "pkg").mkdir(parents=True)
        (repo / "tests").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "pkg" / "lines.py").write_text(
            "def split(text):\n    return text.splitlines()\n\n\ndef public(text):\n    return split(text)\n",
            encoding="utf-8")
        (repo / "tests" / "test_lines.py").write_text(
            "from pkg.lines import public\n\ndef test_basic():\n    assert public('a\\nb') == ['a', 'b']\n",
            encoding="utf-8")
        configure_workspace(repo)
        self.snapshot = self.root / "snapshot.tgz"
        with tarfile.open(self.snapshot, "w:gz") as archive:
            archive.add(repo, arcname=".")
        self.test_patch = self.root / "private.patch"
        self.test_patch.write_text("--- /dev/null\n+++ b/tests/test_private.py\n@@ -0,0 +1 @@\n+x = 1\n")
        self.repro = self.root / "repro_issue.py"

    def tearDown(self):
        self.temp.cleanup()

    def run_candidate(self, name, client):
        submission = self.root / name
        for relative, content in assemble(name).items():
            path = submission / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        task = TaskSpec.from_dict({
            "schema_version": "1.0", "task_id": "wf_1", "repository": "example/repo",
            "problem_statement": "public() must keep a trailing newline.", "hints": "",
            "snapshot_path": str(self.snapshot),
        })
        verification = VerificationSpec.from_dict({
            "schema_version": "1.0", "task_id": "wf_1", "test_patch_path": str(self.test_patch),
            "verification_command": "python -c \"from pkg.lines import public; assert public('a\\n') == ['a\\n']\"",
        })
        config = RunConfig.from_dict({
            "schema_version": "1.0", "submission_directory": str(submission),
            "output_root": str(self.root / "runs"), "work_root": str(self.root / "work"),
            "experiment_name": name, "seed": 42,
        })
        return run_task(task, verification, config, client=client)

    def repro_command(self):
        body = "from pkg.lines import public\nimport sys\nsys.exit(0 if public('a\\n') == ['a\\n'] else 1)\n"
        return f"cat > {self.repro} <<'PY'\n{body}PY\npython {self.repro} || true"

    def test_pipeline_runs_phases_in_order_and_passes_state(self):
        edit = {"file": "pkg/lines.py", "old": "return text.splitlines()",
                "new": "return text.splitlines(keepends=True)"}
        client = RouterClient({
            "step 1 of 4": [skill("swe_locate", "find_candidates.py", {"query": "trailing newline", "detail": 2}, "l1"),
                            text_response("SITE: pkg/lines.py:split\nENTRY: public")],
            "step 2 of 4": [tool_response("run_command", {"command": self.repro_command()}, "r1"),
                            text_response(f"REPRO: {self.repro}\nCONFIRMED SITE: pkg/lines.py:split")],
            "step 3 of 4": [skill("swe_locate", "inspect_symbol.py", {"symbol": "pkg/lines.py:split"}, "i1"),
                            skill("swe_edit", "apply_edit.py", edit, "e1"),
                            skill("swe_edit", "verify.py", {"repro": str(self.repro)}, "v1"),
                            text_response("CHANGED: split\nVERIFY: GREEN")],
            "step 4 of 4": [skill("swe_edit", "finalize.py", {}, "f1"),
                            tool_response("submit_patch", {}, "s1")],
        })
        result = self.run_candidate("A_pipeline", client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        order = [key for key, _ in client.requests]
        self.assertEqual(order, ["step 1 of 4"] * 2 + ["step 2 of 4"] * 2 + ["step 3 of 4"] * 4 + ["step 4 of 4"] * 2)
        repair_first = next(messages for key, messages in client.requests if key == "step 3 of 4")
        # output_key state reached the instruction; include_contents=none dropped earlier tool calls.
        self.assertIn("CONFIRMED SITE: pkg/lines.py:split", repair_first[0]["content"])
        self.assertEqual([m["role"] for m in repair_first], ["system", "user"])
        self.assertIn("For context:\n[reproduce] said:", repair_first[1]["content"])
        repair_calls = [messages for key, messages in client.requests if key == "step 3 of 4"]
        # inspect_symbol showed how the caller uses the site; verify reported the repro result.
        self.assertIn("USED BY", json.dumps(repair_calls[1]))
        self.assertIn("REPRO PASS", json.dumps(repair_calls[-1]))
        submit_first = next(messages for key, messages in client.requests if key == "step 4 of 4")
        self.assertIn("Make exactly two tool calls", submit_first[0]["content"])
        # Per-phase thinking: the mechanical submit step runs without thinking, repair keeps it.
        thinking = {key: value.get("chat_template_kwargs", {}).get("enable_thinking") for key, value in client.settings}
        self.assertEqual(thinking["step 4 of 4"], False)
        self.assertEqual(thinking["step 3 of 4"], True)
        self.assertIn("agent_output", (result.run_directory / "events.jsonl").read_text())

    def c_parallel_client(self, judge_steps):
        good = {"op": "edit", "file": "pkg/lines.py", "old": "return text.splitlines()",
                "new": "return text.splitlines(keepends=True)"}
        # Solver 2 uses argument shapes seen in run 1 (alias keys, no op for the edit).
        bad = {"candidate": "2", "path": "pkg/lines.py", "old_string": "return split(text)", "new_string": "return list(text)"}
        return RouterClient({
            "solver 1 of 3": [tool_response("run_command", {"command": self.repro_command()}, "r1"),
                              skill("swe_branch", "wt.py", {"op": "repro", "id": "1", "file": str(self.repro)}, "p1"),
                              skill("swe_branch", "wt.py", {**good, "id": "1"}, "e1"),
                              text_response("CANDIDATE 1: pkg/lines.py:split")],
            "solver 2 of 3": [skill("swe_branch", "wt.py", bad, "e2"), text_response("CANDIDATE 2: done")],
            "solver 3 of 3": [text_response("CANDIDATE 3: no change")],
            "You finalize the patch": judge_steps,
        })

    def test_parallel_solvers_isolated_then_judge_adopts(self):
        client = self.c_parallel_client([skill("swe_branch", "wt.py", {"op": "judge"}, "j1"),
                                         tool_response("submit_patch", {}, "s1")])
        result = self.run_candidate("C_parallel", client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        patch = result.patch_path.read_text()
        self.assertIn("keepends=True", patch)
        self.assertNotIn("list(text)", patch)
        judge = [messages for key, messages in client.requests if key == "You finalize the patch"]
        judged = json.dumps(judge[-1])
        self.assertIn("RECOMMENDED: adopt candidate 1", judged)
        # Solvers start at once: no scout, and the judge sees only its own turn.
        self.assertNotIn("scout", json.dumps(client.requests[0][1]))
        self.assertEqual([m["role"] for m in judge[0]][:1], ["system"])
        validation = json.loads((result.run_directory / "trace_validation.json").read_text())
        self.assertTrue(validation["valid"], validation["errors"][:3])
        sequences = [json.loads(line)["sequence"] for line in
                     (result.run_directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sequences, sorted(sequences))

    def test_parallel_green_candidate_is_in_workspace_even_without_judging(self):
        # The judge skips the judge op; the solver's green edit was already adopted into /workspace.
        client = self.c_parallel_client([tool_response("submit_patch", {}, "s1")])
        result = self.run_candidate("C_parallel", client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        self.assertIn("keepends=True", result.patch_path.read_text())

    def test_root_rerun_after_nudge_starts_from_first_agent(self):
        client = RouterClient({
            "step 1 of 4": [text_response("SITE: a"), text_response("SITE: b")],
            "step 2 of 4": [text_response("REPRO: none"), text_response("REPRO: none")],
            "step 3 of 4": [text_response("CHANGED: none"), text_response("CHANGED: none")],
            "step 4 of 4": [text_response("nothing"), tool_response("submit_patch", {}, "s1")],
        })
        result = self.run_candidate("A_pipeline", client)
        order = [key for key, _ in client.requests]
        self.assertEqual(order, ["step 1 of 4", "step 2 of 4", "step 3 of 4", "step 4 of 4"] * 2)
        rerun_localize = [messages for key, messages in client.requests if key == "step 1 of 4"][1]
        self.assertIn("Please continue your work", json.dumps(rerun_localize))
        self.assertIn(result.termination_reason, {"submitted_patch"})

    def test_branch_visibility(self):
        events = [
            {"author": "user", "branch": None, "message": {"role": "user", "content": "task"}},
            {"author": "a", "branch": "p.a", "message": {"role": "assistant", "content": "from a"}},
            {"author": "b", "branch": "p.b", "message": {"role": "assistant", "content": "from b"}},
        ]
        seen = _contents(events, "b", "p.b", "default")
        self.assertEqual([m["content"] for m in seen], ["task", "from b"])
        judge = _contents(events, "judge", None, "none")
        self.assertEqual(judge, [{"role": "user", "content": "For context:\n[b] said: from b"}])


if __name__ == "__main__":
    unittest.main()
