"""E_conductor through the local traced runtime with a scripted model (wiring only, no Gemma).

    PYTHONPATH=tests:. <venv>/bin/python -m pytest -q harnesses/candidates/E_conductor/tests

Uses the real /tmp/flow.py, /tmp/spec.py and /tmp/new.py paths, as the official container would.
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from test_agent_harness import tool_response
import test_workflow
from test_workflow import RouterClient, skill

MARK = "A controller script does the bookkeeping"
SPEC = ("cat > /tmp/spec.py <<'EOF'\nimport sys\nfrom pkg.lines import public\n"
        "got = public('a\\n')\nprint('PASS' if got == ['a\\n'] else 'FAIL', repr('a\\n'), '->', repr(got))\n"
        "sys.exit(0 if got == ['a\\n'] else 1)\nEOF\npython3 /tmp/flow.py")
NEW = ("cat > /tmp/new.py <<'EOF'\ndef split(text):\n    return text.splitlines(keepends=True)\nEOF\n"
       "python3 /tmp/flow.py put pkg/lines.py::split")


class ConductorRuntimeTests(test_workflow.WorkflowTests):
    def setUp(self):
        super().setUp()
        for stale in ("/tmp/spec.py", "/tmp/new.py", "/tmp/flow.py"):
            Path(stale).unlink(missing_ok=True)
        shutil.rmtree("/tmp/swe_state/flow", ignore_errors=True)

    def test_conductor_spec_put_green_submit(self):
        client = RouterClient({MARK: [
            skill("swe_flow", "flow.py", {"issue": "public() must keep a trailing newline"}, "c1"),
            tool_response("run_command", {"command": SPEC}, "c2"),
            tool_response("run_command", {"command": NEW}, "c3"),
            tool_response("submit_patch", {}, "c4"),
        ]})
        result = self.run_candidate("E_conductor", client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        seen = json.dumps([messages for _, messages in client.requests])
        self.assertIn("swe_flow ready", seen)
        self.assertIn("SPEC ACCEPTED", seen)
        self.assertIn("TRACE on the original code", seen)
        self.assertRegex(seen, r"    split\(.{1,12}\) -> \[.a.\]")  # the trace shows what split() returned
        self.assertIn("GREEN", seen)
        patch = (Path(result.run_directory) / "final" / "agent.patch").read_text()
        self.assertIn("keepends=True", patch)
        self.assertNotIn("spec.py", patch)



for name in [n for n in dir(test_workflow.WorkflowTests) if n.startswith("test_")]:
    if name != "test_conductor_spec_put_green_submit":
        setattr(ConductorRuntimeTests, name, None)

if __name__ == "__main__":
    unittest.main()
