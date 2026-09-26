from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from mvp_v2.harness import Budget, EventLogger, WorkspaceTools, inject_session_state
from mvp_v2.v3 import load_submission

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "harnesses" / "v3" / "skills"
SCRIPTS = {"apply_edit.py": SKILLS / "swe_edit" / "scripts", "run_tests.py": SKILLS / "swe_edit" / "scripts",
           "find_candidates.py": SKILLS / "swe_locate" / "scripts"}
HAS_PYTEST = importlib.util.find_spec("pytest") is not None

# Arguments recorded verbatim from the v20 fastapi_14258 trace: the model typed newlines
# and quotes as escape sequences, so edit_file never matched.
RECORDED_OLD = '        \\"\\"\\"\\n        if prefix:\\n'
RECORDED_NEW = (
    '        \\"\\"\\"\\n        if router is self:\\n'
    '            raise FastAPIError(\\"Cannot include a router into itself\\")\\n        if prefix:\\n'
)


def make_repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="swe-tools-"))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(command, cwd=root, check=True)
    return root


def run_script(name: str, repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "SWE_WORKSPACE": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(SCRIPTS[name] / name), *args], cwd=tempfile.gettempdir(),
                          env=env, capture_output=True, text=True, timeout=120)


class ApplyEditTests(unittest.TestCase):
    def test_recorded_escaped_arguments_apply(self):
        repo = make_repo({"routing.py": '''
            def include_router(self, router, prefix=""):
                """
                Include a router.
                """
                if prefix:
                    assert prefix.startswith("/")
                return router
            '''})
        result = run_script("apply_edit.py", repo, "--file", "routing.py",
                            "--old", RECORDED_OLD.replace("        ", "    "),
                            "--new", RECORDED_NEW.replace("        ", "    "))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("escape sequences decoded", result.stdout)
        text = (repo / "routing.py").read_text()
        self.assertIn('    if router is self:\n        raise FastAPIError("Cannot include a router into itself")\n', text)
        compile(text, "routing.py", "exec")

    def test_literal_backslash_escape_in_code_is_preserved(self):
        repo = make_repo({"mod.py": 'def lines(text):\n    return text.split("\\n")\n'})
        result = run_script("apply_edit.py", repo, "--file", "mod.py",
                            "--old", 'text.split("\\n")', "--new", 'text.split("\\n", 1)')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((repo / "mod.py").read_text(), 'def lines(text):\n    return text.split("\\n", 1)\n')

    def test_ambiguous_match_reports_lines_and_occurrence_selects(self):
        repo = make_repo({"mod.py": "x = 1\ny = 2\nx = 1\n"})
        result = run_script("apply_edit.py", repo, "--file", "mod.py", "--old", "x = 1", "--new", "x = 3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("lines 1, 3", result.stdout)
        result = run_script("apply_edit.py", repo, "--file", "mod.py", "--old", "x = 1", "--new", "x = 3",
                            "--occurrence", "2")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((repo / "mod.py").read_text(), "x = 1\ny = 2\nx = 3\n")

    def test_not_found_prints_closest_region(self):
        repo = make_repo({"mod.py": "def value(self):\n    return self._value + 1\n"})
        result = run_script("apply_edit.py", repo, "--file", "mod.py",
                            "--old", "return self.value + 1", "--new", "return 2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Closest regions", result.stdout)
        self.assertIn("return self._value + 1", result.stdout)

    def test_edit_that_breaks_compilation_is_refused(self):
        repo = make_repo({"mod.py": "def value():\n    return 1\n"})
        result = run_script("apply_edit.py", repo, "--file", "mod.py",
                            "--old", "    return 1", "--new", "    return (1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("would not compile", result.stdout)
        self.assertEqual((repo / "mod.py").read_text(), "def value():\n    return 1\n")

    def test_flexible_match_reindents_replacement(self):
        repo = make_repo({"mod.py": "class A:\n    def f(self):\n        if x:\n            return 1\n"})
        result = run_script("apply_edit.py", repo, "--file", "mod.py",
                            "--old", "if x:\n    return 1", "--new", "if x is not None:\n    return 1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("flexible", result.stdout)
        self.assertEqual((repo / "mod.py").read_text(),
                         "class A:\n    def f(self):\n        if x is not None:\n            return 1\n")


class FindCandidatesTests(unittest.TestCase):
    def test_ranks_files_matching_more_and_rarer_terms_and_maps_tests(self):
        repo = make_repo({
            "pkg/__init__.py": "",
            "pkg/decoder.py": "def decode(text):\n    return text.splitlines()\n",
            "pkg/other.py": "def decode_other(text):\n    return text.strip()\n",
            "tests/test_decoder.py": "from pkg.decoder import decode\n\ndef test_decode():\n    assert decode('a')\n",
            "tests/test_misc.py": "def test_strip():\n    assert 'a'.strip()\n",
        })
        result = run_script("find_candidates.py", repo, "--terms", "['decode', 'splitlines', 'strip']")
        self.assertEqual(result.returncode, 0, result.stdout)
        lines = result.stdout.splitlines()
        first = next(line for line in lines if line.startswith("1. "))
        self.assertIn("pkg/decoder.py", first)
        self.assertIn("tests: tests/test_decoder.py", result.stdout)
        self.assertLessEqual(len(result.stdout), 2600)


@unittest.skipUnless(HAS_PYTEST, "pytest is required to exercise run_tests.py")
class RunTestsTests(unittest.TestCase):
    def test_baseline_separates_new_and_existing_failures(self):
        repo = make_repo({
            "calc.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n",
            "tests/test_calc.py": (
                "from calc import add, sub\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_sub():\n    assert sub(3, 1) == 2\n"
            ),
            "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        })
        (repo / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n\ndef sub(a, b):\n    return a + b\n")
        result = run_script("run_tests.py", repo, "--targets", "tests/test_calc.py", "--baseline", "true")
        self.assertEqual(result.returncode, 1)
        self.assertIn("2 failed", result.stdout)
        self.assertIn("test_add [NEW: introduced by your change]", result.stdout)
        self.assertIn("test_sub [already failing before your change]", result.stdout)
        self.assertIn("return a - b", (repo / "calc.py").read_text(), "stash must be restored")


class SkillEmulationTests(unittest.TestCase):
    def tools(self, repo: Path, allowed=None) -> WorkspaceTools:
        skill = SKILLS / "swe_edit"
        files = {path.relative_to(skill).as_posix(): path.read_text()
                 for path in skill.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
        logger_root = Path(tempfile.mkdtemp(prefix="skill-log-"))
        return WorkspaceTools(
            repo, Budget(max_time_minutes=5, max_tool_calls=40), EventLogger(logger_root),
            python=Path(sys.executable), allowed_tools=allowed,
            skills={"swe_edit": {"description": "d", "files": files}},
        )

    def test_run_skill_script_uses_adk_argv_and_counts_budget(self):
        repo = make_repo({"mod.py": "value = 1\n"})
        tools = self.tools(repo, allowed=["run_command", "submit_patch"])
        result = tools.dispatch("run_skill_script", {
            "skill_name": "swe_edit", "file_path": "scripts/apply_edit.py",
            "args": {"file": "mod.py", "old": "value = 1", "new": "value = 2"},
        })
        self.assertEqual(result["status"], "success", result)
        self.assertEqual((repo / "mod.py").read_text(), "value = 2\n")
        self.assertEqual(tools.budget.tool_calls_used, 1)
        missing = tools.dispatch("run_skill_script", {"skill_name": "swe_edit", "file_path": "nope.py"})
        self.assertIn("not found in skill 'swe_edit'", missing["error_message"])

    def test_undeclared_builtin_tools_are_rejected(self):
        repo = make_repo({"mod.py": "value = 1\n"})
        tools = self.tools(repo, allowed=["run_command", "submit_patch"])
        result = tools.dispatch("edit_file", {"filepath": "mod.py", "old_string": "1", "new_string": "2"})
        self.assertEqual(result["error_type"], "UnknownTool")
        self.assertEqual((repo / "mod.py").read_text(), "value = 1\n")


class SubmissionSkillValidationTests(unittest.TestCase):
    def files(self) -> dict[str, str]:
        root = ROOT / "harnesses" / "v3"
        return {path.relative_to(root).as_posix(): path.read_text() for path in sorted(root.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".")}

    def test_submission_exposes_skill_tools_without_edit_tools(self):
        config = load_submission(self.files())
        self.assertIn("run_skill_script", config["tools"])
        self.assertNotIn("edit_file", config["tools"])
        self.assertNotIn("write_file", config["tools"])
        self.assertEqual(sorted(config["skills"]["swe_edit"]["files"]),
                         ["SKILL.md", "scripts/apply_edit.py", "scripts/run_tests.py"])

    def test_localizer_is_a_read_only_agent_tool(self):
        config = load_submission(self.files())
        self.assertIn("localize", config["tools"])
        localizer = config["agent_tools"]["localize"]
        self.assertEqual(sorted(localizer["skills"]["swe_locate"]["files"]),
                         ["SKILL.md", "scripts/find_candidates.py"])
        self.assertTrue(set(localizer["declared_tools"]).isdisjoint({"submit_patch", "edit_file", "write_file"}))
        self.assertEqual(localizer["model"], config["model"])

    def test_instructions_render_with_official_session_state(self):
        config = load_submission(self.files())
        state = {"problem_description": "Values {x} are wrong"}
        self.assertEqual(inject_session_state(config["instruction"], state), config["instruction"])
        rendered = inject_session_state(config["agent_tools"]["localize"]["instruction"], state)
        self.assertIn("Values {x} are wrong", rendered)
        self.assertNotIn("{hints?}", rendered)
        self.assertIn("{hints}", inject_session_state("{hints}", {"hints": "{hints}"}))
        with self.assertRaises(KeyError):
            inject_session_state("{problem_description}", {})
        self.assertEqual(inject_session_state('args={"terms": "..."}', {}), 'args={"terms": "..."}')

    def test_undeclared_skill_files_are_rejected(self):
        files = self.files()
        files["skills/stray/SKILL.md"] = "---\nname: stray\n---\n"
        with self.assertRaisesRegex(ValueError, "declared skill"):
            load_submission(files)


if __name__ == "__main__":
    unittest.main()
