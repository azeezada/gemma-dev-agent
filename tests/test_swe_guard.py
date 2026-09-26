"""Guard layer of the shared experiment skill experiments/multi/lib/skills/swe_edit.

Replays B_guarded's run-1 collapse on a toy repository: the reproduction passes after an edit made
outside apply_edit (as `sed` did), the agent then "refines" the fix into a worse one, and finalize or
the deadline salvage must put the passing state back. Scripts run with a throwaway venv so the repro
hook's .pth never lands in a shared interpreter.
"""
from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "experiments" / "multi" / "lib" / "skills" / "swe_edit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guard  # noqa: E402

MODULE = '''
def split_words(text):
    return text.split()
'''
# The fix under test: keep empty fields. Old tests pin the pre-fix behavior (one of them will fail).
TESTS = '''
from mod import split_words

def test_basic():
    assert split_words("a b") == ["a", "b"]

def test_pins_old_behavior():
    assert split_words("a  b") == ["a", "b"]
'''
REPRO = '''
import sys
from mod import split_words
sys.exit(0 if split_words("a  b") == ["a", "", "b"] else 1)
'''
GOOD = 'return text.split(" ")'
WORSE = 'return text.split(" ") if "  " in text else None'


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="swe-guard-"))
    (root / "mod.py").write_text(textwrap.dedent(MODULE))
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(textwrap.dedent(TESTS))
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "baseline"]):
        subprocess.run(command, cwd=root, check=True)
    return root


class GuardFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="swe-guard-env-"))
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(cls.tmp / "venv")], check=True)
        cls.python = cls.tmp / "venv" / "bin" / "python"
        venv_site = subprocess.run([str(cls.python), "-c", "import site; print(site.getsitepackages()[0])"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        # pytest comes from the interpreter running these tests.
        Path(venv_site, "host_packages.pth").write_text("\n".join(site.getsitepackages()) + "\n")
        cls.venv_site = Path(venv_site)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.repo = make_repo()
        self.repro = self.tmp / "repro_issue.py"
        self.repro.write_text(textwrap.dedent(REPRO))
        self.env = {**os.environ, "SWE_WORKSPACE": str(self.repo), "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": f"{self.python.parent}{os.pathsep}{os.environ['PATH']}", "SWE_GUARD_WATCHDOG": "0"}
        self.env.pop("SWE_GUARD", None)

    def tearDown(self):
        shutil.rmtree(guard.state_dir(self.repo), ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def script(self, name, *args, **env):
        return subprocess.run([str(self.python), str(SCRIPTS / name), *args], cwd=tempfile.gettempdir(),
                              env={**self.env, **env}, capture_output=True, text=True, timeout=120)

    def edit(self, body):  # an edit made outside apply_edit, like sed through run_command
        (self.repo / "mod.py").write_text(textwrap.dedent(MODULE).replace("return text.split()", body))

    def run_repro(self, **env):
        return subprocess.run([str(self.python), str(self.repro)], cwd=self.repo, env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def test_collapse_is_undone_by_finalize(self):
        first = self.script("verify.py", "--repro", str(self.repro))
        self.assertIn("VERDICT: BUG REPRODUCED", first.stdout)
        self.assertTrue((self.venv_site / "swe_guard.pth").is_file())

        self.edit(GOOD)
        green = self.run_repro()
        self.assertEqual(green.returncode, 0)
        self.assertIn("[guard]", green.stdout)
        self.assertIn("PASSES with your change", green.stdout)

        self.edit(WORSE)  # the "refinement": repro still passes, more old tests break
        worse = self.run_repro()
        self.assertEqual(worse.returncode, 0)

        final = self.script("finalize.py")
        self.assertIn("Restored checkpoint", final.stdout, final.stdout + final.stderr)
        self.assertIn(GOOD, (self.repo / "mod.py").read_text())

    def test_correct_fix_gets_submit_verdict_despite_pinned_assertion(self):
        self.script("verify.py", "--repro", str(self.repro))
        self.edit(GOOD)
        result = self.script("verify.py", "--script", str(self.repro))  # alias for --repro
        self.assertIn("(0 crash, 1 assertion)", result.stdout, result.stdout)
        self.assertIn("VERDICT: SUBMIT", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_crash_is_not_submit(self):
        self.script("verify.py", "--repro", str(self.repro))
        self.edit('return text.split(" ") if text.count(" ") > 1 else len(None)')
        result = self.script("verify.py", "--repro", str(self.repro))
        self.assertIn("1 crash", result.stdout, result.stdout)
        self.assertIn("VERDICT: FIX", result.stdout)

    def test_hook_is_noop_when_not_armed_and_for_other_scripts(self):
        self.script("verify.py", "--repro", str(self.repro))
        other = make_repo()
        try:
            env = {**self.env, "SWE_WORKSPACE": str(other)}
            result = subprocess.run([str(self.python), str(self.repro)], cwd=other, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertNotIn("[guard]", result.stdout)
            probe = self.tmp / "probe_x.py"
            probe.write_text("print('hi')\n")
            result = subprocess.run([str(self.python), str(probe)], cwd=self.repo, env=self.env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.stdout, "hi\n")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_deadline_salvage_inside_apply_edit(self):
        # Budget 50 s: the deadline margin (45 s) is already reached once the checkpoint exists.
        self.script("verify.py", "--repro", str(self.repro), SWE_GUARD_BUDGET="50")
        self.edit(GOOD)
        self.run_repro()
        self.edit(WORSE)
        time.sleep(max(0.0, guard.read_armed(self.repo)["origin"] + 5.5 - time.time()))
        result = self.script("apply_edit.py", "--file", "mod.py", "--old", "def split_words", "--new", "def split_words")
        self.assertIn("time is almost up", result.stdout, result.stdout + result.stderr)
        self.assertIn("NOT applied", result.stdout)
        self.assertIn(GOOD, (self.repo / "mod.py").read_text())


class ApplyEditArgumentTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        (self.repo / "esc.py").write_text("def f(line):\n    return line.rstrip('\\r\\n')\n")
        self.env = {**os.environ, "SWE_WORKSPACE": str(self.repo), "SWE_GUARD": "0"}

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def run_edit(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "apply_edit.py"), *args], cwd=tempfile.gettempdir(),
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_aliases(self):
        result = self.run_edit("--path", "mod.py", "--search", "return text.split()", "--replace", "return text.split(' ')")
        self.assertIn("OK: edited", result.stdout, result.stdout)

    def test_new_kept_as_typed_when_old_needed_decoding(self):
        # Recorded from A_pipeline run 1: --old copied from an escaped display (double backslashes),
        # --new typed correctly. Decoding --new turned '\r\n' into a real line break.
        old = "    return line.rstrip('\\\\r\\\\n')"
        new = "    return line.rstrip('\\r\\n').strip()"
        result = self.run_edit("--file", "esc.py", "--old", old, "--new", new)
        self.assertIn("OK: edited", result.stdout, result.stdout)
        self.assertIn("rstrip('\\r\\n').strip()", (self.repo / "esc.py").read_text())

    def test_classify_by_exception_type(self):
        output = textwrap.dedent('''
            ______ test_decode[\\x1b[31mx\\n-y] ______
            E   AssertionError: assert 'a' == 'b'
            ______ test_other ______
            E   TypeError: bad
        ''')
        failed = {"tests/t.py::test_decode[\\x1b[31mx\\n-y]": ("FAILED", ""),
                  "tests/t.py::test_other": ("FAILED", "TypeError: bad"),
                  "tests/t.py::test_err": ("ERROR", "")}
        self.assertEqual(guard.classify(failed, output), (2, 1))


class ChooseTests(unittest.TestCase):
    def row(self, index, hsh, crash, assertion, lines=2, repro="PASS"):
        return {"index": index, "hash": hsh, "repro": repro, "changed": True, "crash": crash,
                "assert_fail": assertion, "lines": lines}

    def test_current_state_wins_ties(self):
        rows = [self.row(1, "a", 0, 1), self.row(2, "b", 0, 1, lines=6)]
        self.assertEqual(guard.choose(rows, "b")["index"], 2)

    def test_fewer_assertion_failures_beat_current(self):
        rows = [self.row(1, "a", 0, 3, lines=2), self.row(2, "b", 0, 4, lines=4)]
        self.assertEqual(guard.choose(rows, "b")["index"], 1)

    def test_crash_ranks_last_and_failing_repro_never_wins(self):
        rows = [self.row(1, "a", 1, 0), self.row(2, "b", 0, 2), self.row(3, "c", 0, 0, repro="FAIL")]
        self.assertEqual(guard.choose(rows, "c")["index"], 2)


if __name__ == "__main__":
    unittest.main()
