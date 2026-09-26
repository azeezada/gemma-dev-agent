"""F_desperate: survive.py mechanics + the full last_stand tree with a scripted model.

    PYTHONPATH=tests:. $VENV -m pytest -q experiments/multi/candidates/F_desperate/tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(PROJECT), str(PROJECT / "tests")]
from test_agent_harness import tool_response  # noqa: E402
from test_workflow import RouterClient, skill, text_response  # noqa: E402

from agent_harness.runtime import run_task  # noqa: E402
from agent_harness.schemas import RunConfig, TaskSpec, VerificationSpec  # noqa: E402
from mvp_v2.harness import configure_workspace  # noqa: E402

sys.path.insert(0, str(PROJECT / "experiments" / "multi"))
from assemble import assemble  # noqa: E402

SURVIVE = Path(__file__).resolve().parents[1] / "skills" / "swe_survive" / "scripts" / "survive.py"
EDIT = SURVIVE.with_name("edit.py")
GOOD = {"file": "pkg/lines.py", "old": "return text.splitlines()", "new": "return text.splitlines(keepends=True)"}
WORSE = {"file": "pkg/lines.py", "old": "return text.splitlines(keepends=True)", "new": "return [text]"}
REPRO = ("import sys\nfrom pkg.lines import public\n"
         "bad = 0\n"
         "for s, want in [('a\\n', ['a\\n']), ('a\\nb', ['a\\n', 'b']), ('', [])]:\n"
         "    got = public(s)\n"
         "    print(('ok: ' if got == want else 'FAIL: ') + repr(s) + ' -> ' + repr(got))\n"
         "    bad += got != want\n"
         "sys.exit(1 if bad else 0)\n")


def make_repo(root: Path) -> Path:
    repo = root / "source"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "lines.py").write_text(
        "def split(text):\n    return text.splitlines()\n\n\ndef public(text):\n    return split(text)\n")
    (repo / "tests" / "test_lines.py").write_text(
        "from pkg.lines import public\n\ndef test_basic():\n    assert public('a\\nb') == ['a', 'b']\n")
    return repo


class SurviveScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base"],
                       cwd=self.repo, check=True)
        self.env = {**os.environ, "SWE_WORKSPACE": str(self.repo), "TMPDIR": str(self.root / "tmp")}
        (self.root / "tmp").mkdir()
        self.repro = self.root / "tmp" / "repro_issue.py"
        self.repro.write_text(REPRO)

    def tearDown(self):
        self.temp.cleanup()

    def survive(self, *argv, script=SURVIVE):
        result = subprocess.run([sys.executable, str(script), *argv], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=120)
        return result.stdout

    def edit(self, old, new):
        path = self.repo / "pkg" / "lines.py"
        path.write_text(path.read_text().replace(old, new))

    def test_arm_refuses_repro_that_already_passes(self):
        passing = self.root / "tmp" / "passing.py"
        passing.write_text("print('ok')\n")
        out = self.survive("--op", "arm", "--repro", str(passing))
        self.assertIn("NOT ARMED", out)
        self.assertIn("CLOCK:", out)

    def test_passing_state_is_never_lost(self):
        self.assertIn("ARMED", self.survive("--op", "arm", "--repro", str(self.repro)))
        self.edit(GOOD["old"], GOOD["new"])
        first = self.survive("--op", "check")
        self.assertIn("AMBER", first)                     # repro passes, test_basic asserts the old behavior
        self.assertIn("NEW FAILURE tests/test_lines.py::test_basic", first)
        self.edit(WORSE["old"], WORSE["new"])             # a "refinement" that breaks the repro
        second = self.survive("check")                    # op as positional
        self.assertIn("REVERTED", second)
        self.assertIn("keepends=True", (self.repo / "pkg" / "lines.py").read_text())

    def test_hygiene_reverts_test_edits_and_deletes_scratch(self):
        self.survive("--op", "arm", "--repro", str(self.repro))
        (self.repo / "tests" / "test_lines.py").write_text("def test_basic():\n    pass\n")
        (self.repo / "probe.py").write_text("print(1)\n")
        out = self.survive(json.dumps({"op": "check"}))  # whole-args-as-JSON form
        self.assertIn("REVERTED edits to test files", out)
        self.assertIn("DELETED scratch", out)
        self.assertFalse((self.repo / "probe.py").exists())
        self.assertIn("['a', 'b']", (self.repo / "tests" / "test_lines.py").read_text())

    def test_guard_actions(self):
        self.survive("--op", "arm", "--repro", str(self.repro))
        self.assertIn("ACTION: reply CONTINUE", self.survive("--op", "guard"))   # RED, time left
        self.edit(GOOD["old"], GOOD["new"])
        self.assertIn("ACTION: reply CONTINUE", self.survive("--op", "guard"))   # first AMBER, time left
        self.assertIn("ACTION: call submit_patch", self.survive("--op", "gate"))  # second AMBER → submit

    def test_edit_script_checks_and_handles_escapes(self):
        self.survive("--op", "arm", "--repro", str(self.repro))
        # old text typed with a literal backslash-n; new text with an escape inside a string literal
        out = self.survive("--path", "pkg/lines.py", "--old", "def split(text):\\n    return text.splitlines()",
                           "--new", "def split(text):\n    sep = '\\t'\n    return text.splitlines(keepends=True)",
                           script=EDIT)
        self.assertIn("EDITED pkg/lines.py", out)
        self.assertIn("--- automatic check ---", out)
        self.assertIn("AMBER", out)
        body = (self.repo / "pkg" / "lines.py").read_text()
        self.assertIn("sep = '\\t'", body)            # escape inside the literal kept as typed
        refused = self.survive("--file", "tests/test_lines.py", "--old", "assert", "--new", "pass", script=EDIT)
        self.assertIn("test files are frozen", refused)
        broken = self.survive("--file", "pkg/lines.py", "--old", "def public(text):", "--new", "def public(text", script=EDIT)
        self.assertIn("would not compile", broken)
        numbered = self.survive("--file", "pkg/lines.py", "--old", "5: def public(text):\n6:     return split(text)",
                                "--new", "def public(text):\n    return list(split(text))", script=EDIT)
        self.assertIn("EDITED", numbered)

    def test_rearm_same_repro_keeps_history_and_clock_uses_baseline_commit(self):
        stamp = str(int(__import__("time").time()) - 100)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "--allow-empty", "-qm", "baseline"],
                       cwd=self.repo, check=True, env={**os.environ, "GIT_COMMITTER_DATE": stamp})
        first = self.survive("--op", "arm", "--repro", str(self.repro))
        self.assertRegex(first, r"CLOCK: ~1(0[0-9]|1[0-9])s used")
        self.edit(GOOD["old"], GOOD["new"])
        self.survive("--op", "check")
        again = self.survive("--op", "arm", "--repro", str(self.repro))   # nudge re-run of recon
        self.assertIn("ARMED already with this same repro", again)
        self.assertIn("BEST SO FAR", self.survive("--op", "check").replace("This is the BEST", "BEST SO FAR"))
        self.assertIn("AMBER repro=PASS", self.survive("--op", "final"))

    def test_final_salvages_a_non_empty_attempt(self):
        self.survive("--op", "arm", "--repro", str(self.repro))
        self.edit("return split(text)", "return split(text)  # attempt")
        out = self.survive("--op", "final")
        self.assertIn("ACTION: call submit_patch now", out)
        diff = subprocess.run(["git", "diff"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertIn("# attempt", diff)


class LastStandTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = make_repo(self.root)
        configure_workspace(repo)
        self.snapshot = self.root / "snapshot.tgz"
        with tarfile.open(self.snapshot, "w:gz") as archive:
            archive.add(repo, arcname=".")
        self.test_patch = self.root / "private.patch"
        self.test_patch.write_text("--- /dev/null\n+++ b/tests/test_private.py\n@@ -0,0 +1 @@\n+x = 1\n")
        self.repro = self.root / "repro_issue.py"

    def tearDown(self):
        self.temp.cleanup()

    def run_tree(self, client):
        submission = self.root / "F"
        for relative, content in assemble("F_desperate").items():
            path = submission / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        task = TaskSpec.from_dict({
            "schema_version": "1.0", "task_id": "f_1", "repository": "example/repo",
            "problem_statement": "public() must keep line endings.", "hints": "", "snapshot_path": str(self.snapshot)})
        verification = VerificationSpec.from_dict({
            "schema_version": "1.0", "task_id": "f_1", "test_patch_path": str(self.test_patch),
            "verification_command": "python -c \"from pkg.lines import public; assert public('a\\n') == ['a\\n']\""})
        config = RunConfig.from_dict({
            "schema_version": "1.0", "submission_directory": str(submission), "output_root": str(self.root / "runs"),
            "work_root": str(self.root / "work"), "experiment_name": "F_desperate", "seed": 42})
        return run_task(task, verification, config, client=client)

    def recon(self):
        command = f"cat > {self.repro} <<'PY'\n{REPRO}PY\npython {self.repro} || true"
        return [tool_response("run_command", {"command": command}, "r1"),
                skill("swe_survive", "survive.py", {"op": "arm", "repro": str(self.repro)}, "a1"),
                text_response("SUSPECTS: pkg/lines.py:split\nREPRO: trailing\nFIRST EDIT: keepends")]

    def guard(self, identity, submit):
        calls = [skill("swe_survive", "survive.py", {"op": "guard"}, identity)]
        return calls + [tool_response("submit_patch", {}, identity + "s") if submit else text_response("CONTINUE")]

    def test_collapse_is_reverted_and_best_fix_submitted(self):
        client = RouterClient({
            "STEP: RECON": self.recon(),
            "STRIKE 1 of 3": [skill("swe_survive", "edit.py", GOOD, "e1"),
                              skill("swe_survive", "edit.py", WORSE, "e2"),       # tries to "polish"
                              tool_response("run_command", {"command": "sed -i 's/keepends=True/keepends=False/' pkg/lines.py"}, "e3"),
                              skill("swe_survive", "survive.py", {"op": "check"}, "c3"),
                              text_response("PASS")],
            "You are the survival guard": self.guard("g1", submit=False) + self.guard("g2", submit=True),
            "STRIKE 2 of 3": [text_response("PASS")],
        })
        result = self.run_tree(client)
        self.assertEqual(result.outcome, "PASS", result.run_directory)
        self.assertEqual(result.termination_reason, "submitted_patch")
        patch = result.patch_path.read_text()
        self.assertIn("keepends=True", patch)
        self.assertNotIn("[text]", patch)
        order = [key for key, _ in client.requests]
        self.assertEqual(order, ["STEP: RECON"] * 3 + ["STRIKE 1 of 3"] * 5 + ["You are the survival guard"] * 2
                         + ["STRIKE 2 of 3"] + ["You are the survival guard"] * 2)
        guard_view = json.dumps([m for k, m in client.requests if k == "You are the survival guard"][1])
        self.assertIn("ACTION: reply CONTINUE", guard_view)
        strike_prompt = [m for k, m in client.requests if k == "STRIKE 1 of 3"][0][0]["content"]
        self.assertIn("FIRST EDIT: keepends", strike_prompt)   # recon output_key reached the strike

    def test_last_will_submits_salvage_when_nothing_passes(self):
        salvage = {"file": "pkg/lines.py", "old": "return split(text)", "new": "return split(text)  # attempt"}
        red = lambda k: ([skill("swe_survive", "edit.py", salvage, f"e{k}"), text_response("RED")] if k == 1
                         else [text_response("RED")])
        client = RouterClient({
            "STEP: RECON": self.recon(),
            "STRIKE 1 of 3": red(1), "STRIKE 2 of 3": red(2), "STRIKE 3 of 3": red(3),
            "You are the survival guard": self.guard("g1", False) + self.guard("g2", False) + self.guard("g3", False),
            "You carry out the last will": [skill("swe_survive", "survive.py", {"op": "final"}, "f1"),
                                            tool_response("submit_patch", {}, "s1")],
        })
        result = self.run_tree(client)
        self.assertEqual(result.termination_reason, "submitted_patch")
        self.assertIn("# attempt", result.patch_path.read_text())


if __name__ == "__main__":
    unittest.main()
