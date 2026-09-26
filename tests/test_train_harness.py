"""Guards for the frozen training harness `harnesses/train_v1` (plan D5, section 5)."""
from __future__ import annotations

import json
from pathlib import Path

from gemma_agent.env.v3 import load_submission
from gemma_agent.rollout import run_task
from test_agent_harness import FakeClient, WorkspaceCase, tool_response

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "harnesses" / "train_v1"


def _files():
    return {p.relative_to(TRAIN).as_posix(): p.read_text() for p in TRAIN.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")}


EDIT = '''python - <<'PY'
from pathlib import Path
p = Path("sample.py"); s = p.read_text()
old = "return 1"
assert s.count(old) == 1, s.count(old)
p.write_text(s.replace(old, "return 2"))
PY'''


class TrainHarnessContractTests(WorkspaceCase):
    submission = TRAIN

    def test_contract_is_one_agent_builtin_tools_no_skills(self):
        config = load_submission(_files())
        self.assertEqual(config["tools"], ["run_command", "read_file", "get_status", "submit_patch"])
        self.assertFalse(config["agent_tools"])
        self.assertFalse(config.get("skills"))
        for forbidden in ("edit_file", "write_file", "run_skill_script"):
            self.assertNotIn(forbidden, config["tools"])
            self.assertNotIn(forbidden, config["instruction"])

    def test_sampling_matches_v22_for_comparability(self):
        v22 = (ROOT / "harnesses" / "v22" / "configs" / "sampling.yaml").read_text()
        self.assertEqual((TRAIN / "configs" / "sampling.yaml").read_text(), v22)

    def test_run_with_edit_through_run_command_passes(self):
        task, verification, config = self.specifications()
        client = FakeClient([
            tool_response("run_command", {"command": EDIT}, "edit-1"),
            tool_response("submit_patch", {}, "submit-1"),
        ])
        result = run_task(task, verification, config, client=client)
        self.assertEqual(result.outcome, "PASS")
        summary = json.loads((result.run_directory / "final" / "summary.json").read_text())
        self.assertEqual(summary["agent"]["termination_reason"], "submitted_patch")
        self.assertEqual(client.offered[0], ["run_command", "read_file", "get_status", "submit_patch"])
