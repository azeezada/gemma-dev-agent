"""D_evolve: swe_evolve scripts on a synthetic repo, plus the full agent tree with a scripted model.

    PYTHONPATH=tests:. $VENV -m pytest -q harnesses/candidates/D_evolve/tests
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[4]
SCRIPTS = HERE.parent / "skills" / "swe_evolve" / "scripts"
sys.path[:0] = [str(PROJECT), str(PROJECT / "src"), str(PROJECT / "tests"), str(PROJECT / "experiments" / "multi")]

from gemma_agent.rollout.runtime import run_task  # noqa: E402
from gemma_agent.rollout.schemas import RunConfig, TaskSpec, VerificationSpec  # noqa: E402
from assemble import assemble  # noqa: E402
from gemma_agent.env.harness import configure_workspace  # noqa: E402
from test_agent_harness import tool_response  # noqa: E402
from test_workflow import RouterClient, skill, text_response  # noqa: E402

LINES = '''def split(text):
    """Split text into lines."""
    return text.splitlines()


def public(text):
    return split(text)
'''
OWN_TEST = "from pkg.lines import split\n\ndef test_split():\n    assert split('a\\nb\\n') == ['a', 'b']\n"
OTHER_TEST = "from pkg.lines import public\n\ndef test_public():\n    assert public('a\\nb')[-1] == 'b'\n"
SPEC = '''import sys
from pkg.lines import public
fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + label + ": got " + repr(got) + " want " + repr(want))
check("trailing", "".join(public("a\\n")), "a\\n")
check("plain", "".join(public("a")), "a")
sys.exit(1 if fails else 0)
'''
GOOD = '''# target: pkg/lines.py::split
def split(text):
    return text.splitlines(keepends=True)
'''
WRONG_LAYER = '''# replace: pkg/lines.py
<<<<<<< OLD
    return split(text)
=======
    return [line + "\\n" for line in split(text)] if "\\n" in text else split(text)
>>>>>>> NEW
'''
BROKEN = "# target: pkg/lines.py::split\ndef split(text:\n    return 1\n"


def clean_tmp():
    for path in ("/tmp/d_evolve", "/tmp/d_cands"):
        shutil.rmtree(path, ignore_errors=True)
    Path("/tmp/d_spec.py").unlink(missing_ok=True)


def make_repo(root: Path) -> Path:
    repo = root / "source"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "lines.py").write_text(LINES)
    (repo / "tests" / "test_lines.py").write_text(OWN_TEST)
    (repo / "tests" / "test_api.py").write_text(OTHER_TEST)
    return repo


class ScriptTests(unittest.TestCase):
    def setUp(self):
        clean_tmp()
        self.temp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.temp.name))
        for command in (["init", "-q"], ["add", "-A"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base"]):
            subprocess.run(["git", *command], cwd=self.repo, check=True)

    def tearDown(self):
        clean_tmp()
        self.temp.cleanup()

    def run_script(self, name, *args):
        env = {**os.environ, "SWE_WORKSPACE": str(self.repo)}
        done = subprocess.run([sys.executable, str(SCRIPTS / name), *args], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def candidate(self, cid, text):
        Path("/tmp/d_cands").mkdir(exist_ok=True)
        Path(f"/tmp/d_cands/{cid}.py").write_text(text)

    def test_focus_accepts_argument_shapes(self):
        for args in (["--target", "pkg/lines.py::split"], ["--target", "pkg/lines.py:split"],
                     ["--file", "pkg/lines.py", "--symbol", "split"], ["--", "pkg/lines.py::split"]):
            code, out = self.run_script("focus.py", *args)
            self.assertEqual(code, 0, out)
            self.assertIn("return text.splitlines()", out)
            self.assertIn("pkg/lines.py:", out)  # caller line from public()
        code, out = self.run_script("focus.py", "--target", "pkg/lines.py::nope")
        self.assertEqual(code, 2)

    def test_spec_must_discriminate(self):
        self.run_script("focus.py", "--target", "pkg/lines.py::split")
        Path("/tmp/d_spec.py").write_text("print('PASS nothing')\n")
        code, out = self.run_script("spec.py")
        self.assertIn("NOT DISCRIMINATING", out)
        Path("/tmp/d_spec.py").write_text("import nope_missing_module\n")
        code, out = self.run_script("spec.py")
        self.assertIn("SPEC ERROR", out)
        Path("/tmp/d_spec.py").write_text(SPEC)
        code, out = self.run_script("spec.py")
        self.assertIn("DISCRIMINATING. Good", out)
        self.assertIn("1 PASS / 1 FAIL", out)

    def test_trial_ranks_own_test_changes_above_regressions_and_keeps_best(self):
        self.run_script("focus.py", "--target", "pkg/lines.py::split")
        Path("/tmp/d_spec.py").write_text(SPEC)
        self.run_script("spec.py")
        self.candidate("g1a", GOOD)          # passes spec; breaks the own test (pins old behavior)
        self.candidate("g1b", WRONG_LAYER)   # passes spec; breaks test_api.py (another module's test)
        self.candidate("g1c", BROKEN)
        code, out = self.run_script("trial.py")
        self.assertEqual(code, 0, out)
        self.assertIn("STATUS: SOLVED (best g1a = AMBER", out)
        self.assertIn("ORANGE", out)
        self.assertIn("BROKEN", out)
        self.assertIn("does not parse", out)
        self.assertIn("keepends=True", (self.repo / "pkg" / "lines.py").read_text())
        # The docstring the proposer omitted is kept.
        self.assertIn('"""Split text into lines."""', (self.repo / "pkg" / "lines.py").read_text())
        # A later, worse candidate never replaces the best one in the workspace.
        self.candidate("g2a", "# target: pkg/lines.py::split\ndef split(text):\n    return []\n")
        code, out = self.run_script("trial.py")
        self.assertIn("best applied to workspace: g1a", out)
        self.assertIn("keepends=True", (self.repo / "pkg" / "lines.py").read_text())
        code, out = self.run_script("context.py", "--id", "g3a")
        self.assertIn("BEST CANDIDATE SO FAR: /tmp/d_cands/g1a.py", out)
        self.assertIn("mkdir -p /tmp/d_cands && cat > /tmp/d_cands/g3a.py", out)
        (self.repo / "scratch_probe.py").write_text("x = 1\n")
        code, out = self.run_script("final.py")
        self.assertNotIn("scratch_probe.py", subprocess.run(["git", "status", "--short"], cwd=self.repo,
                                                            capture_output=True, text=True).stdout)
        self.assertIn("keepends=True", out)

    def test_no_spec_still_applies_focused_candidate(self):
        self.run_script("focus.py", "--target", "pkg/lines.py::split")
        self.candidate("g1a", GOOD)
        code, out = self.run_script("trial.py")
        self.assertIn("best applied to workspace: g1a", out)

    def test_new_workspace_discards_old_candidates(self):
        self.run_script("focus.py", "--target", "pkg/lines.py::split")
        self.candidate("g1a", GOOD)
        state = json.loads(Path("/tmp/d_evolve/state.json").read_text())
        state["root"] = "/somewhere/else"
        Path("/tmp/d_evolve/state.json").write_text(json.dumps(state))
        code, out = self.run_script("trial.py")
        self.assertIn("No candidates found", out)

    def test_imports_are_hoisted_and_methods_found(self):
        sys.path.insert(0, str(SCRIPTS))
        from common import apply_blocks, parse_candidate
        (self.repo / "pkg" / "cls.py").write_text('import os\n\n\nclass A:\n    """Doc."""\n\n    @staticmethod\n'
                                                  '    def f(x):\n        """F doc."""\n        return x\n')
        blocks = parse_candidate("```python\n# target: pkg/cls.py::A.f\nimport re\n\n@staticmethod\ndef f(x):\n"
                                 "    return re.sub('a', 'b', x)\n```\n")
        new = apply_blocks(str(self.repo), blocks)["pkg/cls.py"]
        self.assertIn("import re", new)
        self.assertIn("        return re.sub", new)
        self.assertEqual(new.count("@staticmethod"), 1)
        self.assertIn('"""F doc."""', new)
        compile(new, "cls.py", "exec")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        clean_tmp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = make_repo(self.root)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        configure_workspace(repo)
        self.snapshot = self.root / "snapshot.tgz"
        with tarfile.open(self.snapshot, "w:gz") as archive:
            archive.add(repo, arcname=".")
        self.test_patch = self.root / "private.patch"
        self.test_patch.write_text("--- /dev/null\n+++ b/tests/test_private.py\n@@ -0,0 +1 @@\n+x = 1\n")

    def tearDown(self):
        clean_tmp()
        self.temp.cleanup()

    def test_tree_runs_scout_generations_judges_and_submits_best(self):
        spec_cmd = f"cat > /tmp/d_spec.py <<'SPEC_EOF'\n{SPEC}SPEC_EOF"
        write = lambda cid, text: tool_response("run_command", {"command": f"mkdir -p /tmp/d_cands && cat > /tmp/d_cands/{cid}.py <<'CAND_EOF'\n{text}CAND_EOF"}, f"w{cid}")
        ctx = lambda cid: skill("swe_evolve", "context.py", {"id": cid}, f"c{cid}")
        trial = lambda i: skill("swe_evolve", "trial.py", {}, f"t{i}")
        client = RouterClient({
            "scout of a patch-search": [
                skill("swe_evolve", "focus.py", {"target": "pkg/lines.py::split"}, "f1"),
                tool_response("run_command", {"command": spec_cmd}, "s1"),
                skill("swe_evolve", "spec.py", {}, "s2"),
                text_response("FOCUS: pkg/lines.py::split | SPEC: 2 checks, 1 fail now")],
            "proposer g1a": [ctx("g1a"), write("g1a", WRONG_LAYER), text_response("g1a: caller")],
            "proposer g1b": [ctx("g1b"), write("g1b", BROKEN), text_response("g1b: broken")],
            "proposer g1c": [ctx("g1c"), text_response("g1c: nothing")],
            "scoring step": [trial(1), text_response("STATUS: SOLVED"), trial(2), text_response("STATUS: SOLVED")],
            "proposer g2a": [ctx("g2a"), write("g2a", GOOD), text_response("g2a: split keeps ends")],
            "proposer g2b": [ctx("g2b"), text_response("SOLVED")],
            "proposer g3a": [ctx("g3a"), text_response("SOLVED")],
            "proposer g3b": [ctx("g3b"), text_response("SOLVED")],
            "You finish a patch search": [trial(3), skill("swe_evolve", "final.py", {}, "fin"),
                                          tool_response("submit_patch", {}, "sub")],
        })
        submission = self.root / "D_evolve"
        for relative, content in assemble("D_evolve").items():
            path = submission / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        task = TaskSpec.from_dict({"schema_version": "1.0", "task_id": "d_1", "repository": "example/repo",
                                   "problem_statement": "public() must keep a trailing newline.", "hints": "",
                                   "snapshot_path": str(self.snapshot)})
        verification = VerificationSpec.from_dict({
            "schema_version": "1.0", "task_id": "d_1", "test_patch_path": str(self.test_patch),
            "verification_command": "python -c \"from pkg.lines import public; assert public('a\\n') == ['a\\n']\""})
        config = RunConfig.from_dict({"schema_version": "1.0", "submission_directory": str(submission),
                                      "output_root": str(self.root / "runs"), "work_root": str(self.root / "work"),
                                      "experiment_name": "D_evolve", "seed": 42})
        result = run_task(task, verification, config, client=client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        patch = result.patch_path.read_text()
        self.assertIn("keepends=True", patch)
        self.assertNotIn('+ "\\n"', patch)
        order = [key for key, _ in client.requests]
        self.assertEqual(order[0], "scout of a patch-search")
        self.assertEqual(order[-1], "You finish a patch search")
        g2a_seen = json.dumps([m for key, m in client.requests if key == "proposer g2a"][1])
        self.assertIn("LAST TRIAL REPORT", g2a_seen)       # generation 2 sees generation 1's scores
        self.assertIn("ORANGE", g2a_seen)
        g1a_first = next(m for key, m in client.requests if key == "proposer g1a")
        self.assertIn("FOCUS: pkg/lines.py::split", g1a_first[0]["content"])  # scout brief reached the instruction
        thinking = {key: value.get("chat_template_kwargs", {}).get("enable_thinking") for key, value in client.settings}
        self.assertFalse(thinking["scoring step"])
        self.assertTrue(thinking["proposer g1a"])


if __name__ == "__main__":
    unittest.main()
