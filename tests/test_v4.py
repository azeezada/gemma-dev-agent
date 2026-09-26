from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_harness.reporting import audit_private_leaks
from agent_harness.tracing import TraceRecorder
from mvp_v2.v3 import load_submission
from test_swe_tools import make_repo

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'harnesses' / 'v22' / 'skills'


def run_script(name, repo, *args):
    skill = 'swe_locate' if name in ('find_candidates.py', 'inspect_symbol.py') else 'swe_edit'
    return subprocess.run([sys.executable, str(SKILLS / skill / 'scripts' / name), *args],
                          cwd=repo, env={**os.environ, 'SWE_WORKSPACE': str(repo),
                          'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}, capture_output=True, text=True, timeout=60)


class V4Tests(unittest.TestCase):
    def test_submission_is_one_editor_with_both_skills(self):
        root = ROOT / 'harnesses' / 'v22'
        files = {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('.')}
        config = load_submission(files)
        self.assertFalse(config['agent_tools'])
        self.assertEqual(set(config['skills']), {'swe_edit', 'swe_locate'})

    def test_batch_edit_rejects_every_change_if_later_edit_is_invalid(self):
        repo = make_repo({'a.py': 'a = 1\n', 'b.py': 'b = 2\n'})
        edits = [{'file': 'a.py', 'old': '1', 'new': '3'},
                 {'file': 'b.py', 'old': '2', 'new': '('}]
        result = run_script('apply_edit.py', repo, '--edits', json.dumps(edits))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((repo/'a.py').read_text(), 'a = 1\n')
        self.assertEqual((repo/'b.py').read_text(), 'b = 2\n')

    def test_batch_edits_accept_adk_list_repr_and_dependent_replacements(self):
        repo = make_repo({'a.py': 'a = 1\n', 'b.py': 'b = 2\n'})
        edits = [{'file': 'a.py', 'old': '1', 'new': '3'},
                 {'file': 'a.py', 'old': '3', 'new': '4'},
                 {'file': 'b.py', 'old': '2', 'new': '5'}]
        result = run_script('apply_edit.py', repo, '--edits', str(edits))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((repo/'a.py').read_text(), 'a = 4\n')
        self.assertEqual((repo/'b.py').read_text(), 'b = 5\n')

    def test_symbol_inspection_skips_long_docs_and_reports_actual_body_and_test(self):
        source = 'class Decoder:\n    def decode(self, text):\n        """\n' + '        long docs\n'*160 + '        """\n        return text.splitlines()\n'
        repo = make_repo({'pkg.py': source, 'tests/test_pkg.py':
                         'from pkg import Decoder\ndef test_decode():\n    assert Decoder().decode("x") == ["x"]\n'})
        result = run_script('inspect_symbol.py', repo, '--file', 'pkg.py', '--symbol', 'Decoder.decode')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('return text.splitlines()', result.stdout)
        self.assertIn('tests/test_pkg.py::test_decode', result.stdout)
        self.assertNotIn('long docs', result.stdout)
        self.assertIn('165:', result.stdout)

    def test_plain_language_query_searches_operations_without_identifiers(self):
        repo = make_repo({'pkg.py': 'def unpack(text):\n    return text.splitlines()\n'})
        result = run_script('find_candidates.py', repo, '--query', 'preserve newlines')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pkg.py', result.stdout)
        self.assertIn('unpack', result.stdout)

    def test_no_tests_selected_is_failure(self):
        repo = make_repo({'test_value.py': 'def test_value():\n    assert True\n'})
        result = run_script('run_tests.py', repo, '--targets', 'test_value.py', '--k', 'absent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('NO TESTS SELECTED', result.stdout)

    def test_baseline_keeps_staged_and_untracked_changes_intact(self):
        repo = make_repo({'calc.py': 'def value():\n    return 1\n', 'test_calc.py':
                         'from calc import value\ndef test_value():\n    assert value() == 1\n'})
        (repo/'calc.py').write_text('def value():\n    return 2\n')
        subprocess.run(['git', 'add', 'calc.py'], cwd=repo, check=True)
        (repo/'notes.txt').write_text('keep me')
        before = subprocess.check_output(['git','diff','--cached'],cwd=repo)
        result = run_script('run_tests.py', repo, '--targets', 'test_calc.py', '--baseline', 'true')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('NEW: introduced', result.stdout)
        self.assertEqual(before, subprocess.check_output(['git','diff','--cached'],cwd=repo))
        self.assertEqual((repo/'notes.txt').read_text(), 'keep me')
        self.assertEqual((repo/'calc.py').read_text(), 'def value():\n    return 2\n')

    def test_audit_ignores_controller_diff_but_detects_escaped_model_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "trace"
            trace = TraceRecorder(root, experiment_id='e', run_id='r', task_id='t')
            secret = '--- a/test.py\n+++ b/test.py\n+assert x == 2\n'
            visible = trace.artifacts.put_json({'stdout': 'edited source'}, purpose='model-visible tool result: edit')
            diagnostic = trace.artifacts.put_text(secret, purpose='workspace diff after edit')
            trace.emit('tool_result', artifact_refs=[visible, diagnostic])
            audit = audit_private_leaks(root, private_values={'test_patch':secret}, private_hashes={})
            self.assertTrue(audit['passed'])
            exposed = trace.artifacts.put_json({'stdout': secret}, purpose='model-visible tool result: read')
            trace.emit('tool_result', artifact_refs=[exposed])
            audit = audit_private_leaks(root, private_values={'test_patch':secret}, private_hashes={})
            self.assertFalse(audit['passed'])
            self.assertEqual(audit['hits'][0]['sequence'], 3)


if __name__ == '__main__':
    unittest.main()
