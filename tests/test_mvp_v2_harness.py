from __future__ import annotations

import json
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

from mvp_v2.harness import (
    Budget,
    EventLogger,
    ModelHTTPError,
    VLLMServer,
    WorkspaceTools,
    compact_messages,
    configure_workspace,
    create_task_venv,
    _compatible_wheels,
    _expand_wheel_dependencies,
    _workspace_requirement_specs,
    _workspace_requirements,
    install_workspace_paths,
    is_context_length_error,
    run_agent,
    target_test_names,
    template_token_ids,
    verify_patch,
)


def command(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class ScriptedClient:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    def chat(self, messages, **_settings):
        self.requests.append(messages)
        return self.responses.pop(0)


def response(tool_name: str, arguments: dict, call_id: str) -> dict:
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "",
                "reasoning_content": "focused",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class HarnessTests(unittest.TestCase):
    def test_vllm_server_disables_optional_flashinfer_sampler(self):
        server = VLLMServer(
            model_path=Path("/model"),
            model_name="test-model",
            log_path=Path("/tmp/server.log"),
        )
        self.assertEqual(server.environment["VLLM_USE_FLASHINFER_SAMPLER"], "0")
        self.assertEqual(server.environment["VLLM_NO_USAGE_STATS"], "1")

    def test_vllm_server_reuses_healthy_matching_endpoint(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        counter = self.root / "starts.txt"
        fake_server = self.root / "fake_vllm.py"
        fake_server.write_text(textwrap.dedent(
            """
            import json
            import sys
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            from pathlib import Path

            port = int(sys.argv[1])
            counter = Path(sys.argv[2])
            model = sys.argv[3]
            starts = int(counter.read_text() or "0") if counter.exists() else 0
            counter.write_text(str(starts + 1))

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/health":
                        payload = {"status": "ok"}
                    elif self.path == "/v1/models":
                        payload = {"data": [{"id": model}]}
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
            """
        ), encoding="utf-8")

        def fake() -> VLLMServer:
            server = VLLMServer(
                model_path=Path("/fake-model"),
                model_name="fake-model",
                log_path=self.root / "fake-server.log",
                port=port,
                startup_timeout=10,
            )
            server.command = [sys.executable, str(fake_server), str(port), str(counter), "fake-model"]
            return server

        first = fake()
        try:
            first_profile = first.ensure_started()
            second_profile = fake().ensure_started()
            self.assertFalse(first_profile["reused"])
            self.assertTrue(second_profile["reused"])
            self.assertEqual(second_profile["startup_seconds"], 0.0)
            self.assertEqual(counter.read_text(), "1")
        finally:
            first.stop()

    def test_task_environment_skips_ensurepip(self):
        venv = self.root / "task-venv"
        python = create_task_venv(venv, self.repo, self.root / "missing-wheels")
        self.assertTrue(python.exists())
        result = subprocess.run(
            [str(python), "-c", "import sys; print(sys.prefix)"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(Path(result.stdout.strip()).resolve(), venv.resolve())

    def test_workspace_requirement_discovery(self):
        (self.repo / "pyproject.toml").write_text(
            """[project]
name = "sample"
dependencies = ["Starlette>=0.40"]

[project.optional-dependencies]
test = ["pytest[testing]>=8; python_version > '3.9'"]

[build-system]
requires = ["hatchling>=1.0"]

[dependency-groups]
tests = ["pytest<9"]
dev = [{include-group = "tests"}]
""",
            encoding="utf-8",
        )
        requirements = _workspace_requirements(self.repo)
        self.assertIn("starlette", requirements)
        self.assertIn("pytest", requirements)
        self.assertIn("hatchling", requirements)
        specs = _workspace_requirement_specs(self.repo)
        self.assertIn("pytest<9", specs)

    def test_wheel_selection_honors_repository_constraints(self):
        wheels = self.root / "wheels"
        wheels.mkdir()
        for name in (
            "starlette-0.50.0-py3-none-any.whl",
            "starlette-1.6.0-py3-none-any.whl",
            "pytest-8.3.4-py3-none-any.whl",
            "pytest-9.1.1-py3-none-any.whl",
        ):
            (wheels / name).touch()
        selected = _compatible_wheels(
            wheels, ["starlette>=0.40,<0.51", "pytest>=7,<9"]
        )
        self.assertEqual(selected["starlette"].name, "starlette-0.50.0-py3-none-any.whl")
        self.assertEqual(selected["pytest"].name, "pytest-8.3.4-py3-none-any.whl")

    def test_offline_dependency_closure_adds_available_wheels_only(self):
        wheels = self.root / "wheels"
        wheels.mkdir()

        def wheel(name: str, metadata: str) -> Path:
            path = wheels / name
            distribution = name.split("-", 1)[0]
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(f"{distribution}-1.0.dist-info/METADATA", metadata)
            return path

        parent = wheel(
            "parent-1.0-py3-none-any.whl",
            "Metadata-Version: 2.1\nName: parent\nVersion: 1.0\n"
            "Requires-Dist: child>=1\nRequires-Dist: missing>=1\n",
        )
        child = wheel(
            "child-2.0-py3-none-any.whl",
            "Metadata-Version: 2.1\nName: child\nVersion: 2.0\n",
        )
        expanded, closure = _expand_wheel_dependencies({"parent": parent}, wheels)
        self.assertEqual(expanded["child"], child)
        self.assertNotIn("missing", expanded)
        self.assertEqual(closure[0]["requirement"], "child>=1")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        command(["git", "init", "-q"], self.repo)
        (self.repo / "sample.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        configure_workspace(self.repo)
        self.logger = EventLogger(self.root / "events")

    def tearDown(self):
        self.temp.cleanup()

    def tools(self, **budget_overrides) -> WorkspaceTools:
        return WorkspaceTools(
            self.repo,
            Budget(max_time_minutes=1, command_timeout_seconds=1, **budget_overrides),
            self.logger,
            python=Path(sys.executable),
        )

    def test_workspace_tools_edit_and_submit(self):
        tools = self.tools()
        read = tools.dispatch("read_file", {"filepath": "sample.py"})
        self.assertEqual(read["status"], "ok")
        edit = tools.dispatch("edit_file", {
            "filepath": "sample.py", "old_string": "return 1", "new_string": "return 2",
        })
        self.assertEqual(edit["strategy"], "exact")
        submitted = tools.dispatch("submit_patch", {})
        self.assertGreater(submitted["patch_size"], 0)
        self.assertIn("+    return 2", tools.submitted_patch)

    def test_command_guard_and_timeout(self):
        tools = self.tools(max_tool_calls=3)
        blocked = tools.dispatch("run_command", {"command": "find / -name tasks.jsonl"})
        self.assertEqual(blocked["error_type"], "CommandPolicyError")
        timed_out = tools.dispatch("run_command", {"command": "sleep 2"})
        self.assertEqual(timed_out["error_type"], "TimeoutExceeded")

    def test_run_command_disables_global_pytest_plugins(self):
        result = self.tools().dispatch(
            "run_command",
            {"command": "python3 -c 'import os; print(os.environ[\"PYTEST_DISABLE_PLUGIN_AUTOLOAD\"])'"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stdout"].strip(), "1")

    def test_budget_exhaustion(self):
        tools = self.tools(max_tool_calls=1)
        self.assertEqual(tools.dispatch("read_file", {"filepath": "sample.py"})["status"], "ok")
        exhausted = tools.dispatch("read_file", {"filepath": "sample.py"})
        self.assertEqual(exhausted["error_type"], "ToolBudgetExceeded")
        self.assertEqual(tools.dispatch("get_status", {})["tool_calls_used"], 1)

    def test_context_compaction_keeps_last_two_results(self):
        messages = [{"role": "system", "content": "x"}]
        for index in range(4):
            messages.append({"role": "tool", "tool_call_id": str(index), "content": json.dumps({
                "status": "ok", "content": "x" * 2_000,
            })})
        compacted, changed = compact_messages(messages)
        self.assertEqual(changed, 2)
        self.assertTrue(json.loads(compacted[1]["content"])["compacted"])
        self.assertGreater(len(compacted[-1]["content"]), 1_000)

    def test_fake_model_tool_loop(self):
        tools = self.tools()
        client = ScriptedClient([
            response("edit_file", {
                "filepath": "sample.py", "old_string": "return 1", "new_string": "return 2",
            }, "edit-1"),
            response("submit_patch", {}, "submit-1"),
        ])
        task = {
            "instance_id": "demo_1", "repo": "demo/repo",
            "problem_statement": "Return two.", "hints_text": "",
            "patch": "SECRET_REFERENCE", "test_patch": "SECRET_TEST",
        }
        result = run_agent(client, task, "Fix the issue.", tools, self.logger, max_tokens=128)
        self.assertEqual(result["termination_reason"], "submitted_patch")
        self.assertIn("return 2", result["patch"])
        initial = json.dumps(client.requests[0])
        self.assertNotIn("SECRET_REFERENCE", initial)
        self.assertNotIn("SECRET_TEST", initial)

    def test_fresh_workspace_verification(self):
        (self.repo / "pytest.py").write_text(
            """import runpy, sys
targets = [arg for arg in sys.argv[1:] if arg.endswith('.py')]
for target in targets:
    namespace = runpy.run_path(target)
    for name, value in namespace.items():
        if name.startswith('test_') and callable(value):
            value()
""",
            encoding="utf-8",
        )
        command(["git", "add", "pytest.py"], self.repo)
        command(["git", "commit", "-m", "test shim", "-q"], self.repo)
        snapshot = self.root / "snapshot.tgz"
        with tarfile.open(snapshot, "w:gz") as archive:
            archive.add(self.repo, arcname=".")
        agent_patch = """--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""
        test_patch = """--- /dev/null
+++ b/test_sample.py
@@ -0,0 +1,4 @@
+from sample import value
+
+def test_value():
+    assert value() == 2
"""
        result = verify_patch(
            snapshot=snapshot,
            workspace=self.root / "verification",
            agent_patch=agent_patch,
            test_patch=test_patch,
            python=Path(sys.executable),
            output_log=self.root / "pytest.log",
        )
        self.assertTrue(result["resolved"], (self.root / "pytest.log").read_text())


class V19RegressionTests(unittest.TestCase):
    def test_template_token_ids_accepts_batch_encoding_mapping(self):
        class BatchEncoding(dict):
            pass

        encoded = BatchEncoding(input_ids=[5, 6, 7, 8], attention_mask=[1, 1, 1, 1])
        self.assertEqual(template_token_ids(encoded), [5, 6, 7, 8])
        self.assertEqual(template_token_ids(BatchEncoding(input_ids=[[1, 2, 3]])), [1, 2, 3])
        self.assertEqual(template_token_ids([9, 10]), [9, 10])

    def test_context_length_error_detection(self):
        overflow = ModelHTTPError(400, "This model's maximum context length is 32768 tokens.")
        self.assertTrue(is_context_length_error(overflow))
        self.assertFalse(is_context_length_error(ModelHTTPError(400, "invalid tool schema")))
        self.assertFalse(is_context_length_error(ModelHTTPError(503, "maximum context length")))

    def test_target_test_names_follow_hunk_context_and_skip_decorators(self):
        patch = textwrap.dedent("""\
            --- a/tests/test_console.py
            +++ b/tests/test_console.py
            @@ -1,3 +1,4 @@
             import os
            +import subprocess
             import sys
            @@ -10,3 +11,9 @@ def test_existing() -> None:
                 assert lines[1] == "x"
            +
            +
            +@pytest.mark.skipif(True, reason="x")
            +def test_new() -> None:
            +    assert True
            @@ -40,2 +47,2 @@ class TestThing:
                 def test_method(self):
            -        assert 1
            +        assert 2
            """)
        self.assertEqual(target_test_names(patch), ["test_new", "test_method"])

    def test_workspace_paths_expose_src_layout_without_shadowing_stdlib(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            (workspace / "src" / "demo_pkg").mkdir(parents=True)
            (workspace / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 7\n")
            (workspace / "src" / "json.py").write_text("raise RuntimeError('shadowed')\n")
            (workspace / "tests").mkdir()
            (workspace / "tests" / "demo_helper.py").write_text("")
            command([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], root)
            python = root / "venv" / "bin" / "python"
            install_workspace_paths(python, workspace)
            result = subprocess.run(
                [str(python), "-c", "import json, demo_pkg; print(demo_pkg.VALUE, json.__name__)"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "7 json")
            hidden = subprocess.run([str(python), "-c", "import demo_helper"], cwd=root, capture_output=True)
            self.assertNotEqual(hidden.returncode, 0)


if __name__ == "__main__":
    unittest.main()
