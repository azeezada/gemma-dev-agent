"""wt.py (experiments/multi swe_branch skill): forgiving arguments, deadline, auto-adopt, cross-checking judge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "experiments" / "multi"))
from assemble import skill_files  # noqa: E402


class WtTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.skill = base / "skill"
        for relative, content in skill_files("swe_branch").items():
            path = self.skill / relative.split("swe_branch/", 1)[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.tmp = base / "tmp"
        self.tmp.mkdir()
        self.repo = base / "repo"
        files = {
            "pkg/__init__.py": "",
            "pkg/lines.py": "def split(text):\n    return text.splitlines()\n\n\ndef public(text):\n    return split(text)\n",
            "tests/test_lines.py": "from pkg.lines import public\n\n\ndef test_basic():\n    assert public('a\\nb') == ['a', 'b']\n\n\n"
                                   "def test_old_behavior():\n    assert public('a\\n') == ['a']\n",
        }
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
            subprocess.run(command, cwd=self.repo, check=True)
        self.repro = self.tmp / "repro_1.py"
        self.repro.write_text("import sys\nfrom pkg.lines import public\nsys.exit(0 if public('a\\n') == ['a\\n'] else 1)\n")

    def tearDown(self):
        self.temp.cleanup()

    def wt(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "SWE_WORKSPACE": str(self.repo), "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(self.skill / "scripts" / "wt.py"), *args], cwd=str(self.tmp),
                              env=env, capture_output=True, text=True, timeout=120)

    def workspace_diff(self) -> str:
        return subprocess.run(["git", "diff", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout

    def test_aliases_seen_in_run_1_are_understood(self):
        self.assertIn("exit 0", self.wt("--op", "run", "--id", "1", "--command", "echo hi").stdout)
        self.assertIn("hi", self.wt("--op", "run", "--id", "1", "--positional_args", "['echo', 'hi']").stdout)
        read = self.wt("--op", "read", "--id", "2", "--filepath", "pkg/lines.py", "--start_line", "5", "--end_line", "6")
        self.assertIn("6:     return split(text)", read.stdout)
        nested = self.wt("--args", "{'op': 'read', 'id': '3', 'file': 'pkg/lines.py', 'symbol': 'split'}")
        self.assertIn("return text.splitlines()", nested.stdout)
        self.assertNotIn("public", nested.stdout)
        tmp_read = self.wt("--op", "read", "--id", "3", "--file", str(self.repro))
        self.assertIn("sys.exit", tmp_read.stdout)

    def test_green_edit_is_adopted_and_judge_prefers_the_minimal_fix(self):
        registered = self.wt("--op", "repro", "--id", "1", "--file", str(self.repro))
        self.assertIn("ON THE CLEAN CODE: FAIL", registered.stdout)
        good = self.wt("--op", "edit", "--id", "1", "--file", "pkg/lines.py",
                       "--old", "return text.splitlines()", "--new", "return text.splitlines(keepends=True)")
        self.assertIn("GREEN", good.stdout)
        self.assertIn("ADOPTED", good.stdout)
        # The old-behavior test fails as an assertion, not a crash.
        self.assertIn("assertion-type new failures", good.stdout)
        self.assertIn("keepends=True", self.workspace_diff())
        # A larger workaround in the caller also passes, but it does not replace the adopted fix.
        workaround = self.wt("--op", "edit", "--id", "2", "--file", "pkg/lines.py", "--old", "    return split(text)",
                             "--new", "    parts = split(text)\n    if text.endswith('\\n'):\n        parts[-1] += '\\n'\n    return parts")
        self.assertIn("GREEN", workaround.stdout)
        self.assertIn("workspace keeps candidate 1", workaround.stdout)
        crash = self.wt("--op", "edit", "--id", "3", "--file", "pkg/lines.py",
                        "--old", "return text.splitlines()", "--new", "return text.splitlines() + None")
        self.assertIn("CRASH-TYPE", crash.stdout)
        judged = self.wt("--op", "judge")
        self.assertIn("RECOMMENDED: adopt candidate 1", judged.stdout, judged.stdout + judged.stderr)
        self.assertIn("candidate 3: passes 0/1", judged.stdout)
        self.assertIn("keepends=True", self.workspace_diff())
        self.assertNotIn("None", self.workspace_diff())

    def test_best_checkpoint_survives_a_later_worse_edit(self):
        self.wt("--op", "repro", "--id", "1", "--file", str(self.repro))
        self.wt("--op", "edit", "--id", "1", "--file", "pkg/lines.py",
                "--old", "return text.splitlines()", "--new", "return text.splitlines(keepends=True)")
        self.wt("--op", "edit", "--id", "1", "--file", "pkg/lines.py",
                "--old", "return text.splitlines(keepends=True)", "--new", "return []")
        judged = self.wt("--op", "judge")
        self.assertIn("RECOMMENDED: adopt candidate 1@best", judged.stdout, judged.stdout)
        self.assertIn("keepends=True", self.workspace_diff())

    def test_deadline_stops_work_but_not_the_judge(self):
        self.wt("--op", "create", "--id", "1")
        state = next(self.tmp.glob("swe_state/*/wt_run.json"))
        state.write_text(json.dumps({"start": time.time() - 1000}))
        self.assertIn("TIME UP", self.wt("--op", "read", "--id", "1", "--file", "pkg/lines.py").stdout)
        self.assertIn("No candidate produced a patch", self.wt("--op", "judge").stdout)

    def test_after_judging_a_rerun_solver_is_told_to_stop(self):
        self.wt("--op", "repro", "--id", "1", "--file", str(self.repro))
        self.wt("--op", "edit", "--id", "1", "--file", "pkg/lines.py",
                "--old", "return text.splitlines()", "--new", "return text.splitlines(keepends=True)")
        self.wt("--op", "judge")
        self.assertIn("DONE", self.wt("--op", "read", "--id", "1", "--file", "pkg/lines.py").stdout)

    def test_repro_that_passes_on_clean_code_is_flagged(self):
        weak = self.tmp / "repro_2.py"
        weak.write_text("print('ok')\n")
        self.assertIn("does not show the bug", self.wt("--op", "repro", "--id", "2", "--file", str(weak)).stdout)


if __name__ == "__main__":
    unittest.main()
