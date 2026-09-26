"""experiments/multi/lib/skills/swe_locate: argument tolerance, caller context, hypothesis detail."""
from __future__ import annotations

import unittest
from pathlib import Path

import os
import subprocess
import sys
import tempfile

from test_swe_tools import make_repo

SCRIPTS = Path(__file__).resolve().parents[1] / "experiments" / "multi" / "lib" / "skills" / "swe_locate" / "scripts"

REPO = {
    "pkg/__init__.py": "",
    "pkg/codec.py": '''
        def decode(text):
            """Split text into parts."""
            return text.splitlines()
        ''',
    "pkg/api.py": '''
        from pkg.codec import decode


        def render(text):
            sep = "|"
            parts = decode(text)
            return sep.join(parts)
        ''',
    "pkg/other.py": '''
        class Box:
            def decode(self):
                return 1
        ''',
    "tests/test_api.py": '''
        from pkg.api import render


        def test_render():
            assert render("a") == "a"
        ''',
}


def run(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "SWE_WORKSPACE": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=tempfile.gettempdir(),
                          env=env, capture_output=True, text=True, timeout=120)


class InspectSymbolArgumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo(REPO)

    def assert_decode_body(self, *args):
        result = run(self.repo, "inspect_symbol.py", *args)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("pkg/codec.py:2-4 decode", result.stdout)
        return result.stdout

    def test_every_spelling_models_used_resolves(self):
        self.assert_decode_body("--file", "pkg/codec.py", "--symbol", "decode")
        self.assert_decode_body("--symbol", "pkg/codec.py:decode")
        self.assert_decode_body("--symbol", "pkg/codec.py::decode")
        self.assert_decode_body("--symbol", "pkg.codec.decode")
        self.assert_decode_body("--file", "pkg/codec.py:4")
        self.assert_decode_body("--", "pkg/codec.py", "decode")
        self.assert_decode_body("--filepath", "/workspace/pkg/codec.py", "--name", "decode()")

    def test_symbol_alone_finds_its_file_or_lists_choices(self):
        self.assert_decode_body("--symbol", "pkg.codec.decode")
        stdout = run(self.repo, "inspect_symbol.py", "--symbol", "render").stdout
        self.assertIn("pkg/api.py", stdout)
        ambiguous = run(self.repo, "inspect_symbol.py", "--symbol", "decode")
        self.assertIn("Several files define decode", ambiguous.stdout)
        self.assertIn("pkg/codec.py", ambiguous.stdout)
        self.assertIn("pkg/other.py", run(self.repo, "inspect_symbol.py", "--symbol", "Box.decode").stdout)

    def test_used_by_shows_call_line_and_where_its_operands_come_from(self):
        stdout = self.assert_decode_body("--symbol", "pkg/codec.py:decode")
        used = stdout.split("USED BY", 1)[1]
        self.assertIn("pkg/api.py", used)
        self.assertIn('sep = "|"', used)
        self.assertIn("parts = decode(text)", used)


    def test_module_level_target_shows_numbered_window(self):
        result = run(self.repo, "inspect_symbol.py", "--symbol", "pkg/api.py:module")
        self.assertIn("module level", result.stdout)
        self.assertRegex(result.stdout, r"\n +[12]: from pkg.codec import decode")


class LocateOutputTests(unittest.TestCase):
    def test_detail_shows_site_code_and_caller_usage(self):
        repo = make_repo(REPO)
        plain = run(repo, "find_candidates.py", "--query", "trailing blank parts lost")
        self.assertEqual(plain.returncode, 0, plain.stdout)
        self.assertNotIn("DETAIL H1", plain.stdout)
        detailed = run(repo, "find_candidates.py", "--query", "trailing blank parts lost", "--detail", "1")
        self.assertIn("DETAIL H1 pkg/codec.py", detailed.stdout)
        self.assertIn("return text.splitlines()", detailed.stdout)
        block = detailed.stdout.split("DETAIL H1", 1)[1].split("RANKED FILES", 1)[0]
        self.assertNotIn("Split text into parts", block)  # docstring skipped
        self.assertIn("used by pkg/api.py", detailed.stdout)

    def test_entry_points_accepts_combined_target(self):
        repo = make_repo(REPO)
        result = run(repo, "entry_points.py", "--symbol", "pkg/codec.py::decode")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("render (pkg/api.py)", result.stdout)


if __name__ == "__main__":
    unittest.main()
