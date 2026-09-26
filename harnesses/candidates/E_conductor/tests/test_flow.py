"""swe_flow controller: spec gate, trace, put, first-green lock, deadline salvage, hygiene.

    PYTHONPATH=tests:. <venv>/bin/python -m pytest -q harnesses/candidates/E_conductor/tests
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "swe_flow" / "scripts"

LIB = '''def split_words(text):
    """Split text on commas."""
    return [w for w in text.split(",") if w]


def count(text):
    return len(split_words(text))
'''
TEST = '''from pkg.words import split_words


def test_split():
    assert split_words("a,b") == ["a", "b"]


def test_empty_dropped():
    assert split_words("a,,b") == ["a", "b"]
'''
SPEC = '''import sys
from pkg.words import count
bad = 0
for given, expected in [("a,b", 2), ("a,,b", 3), ("", 1)]:
    got = count(given)
    print("PASS" if got == expected else "FAIL", repr(given), "->", repr(got))
    bad += got != expected
sys.exit(1 if bad else 0)
'''
FIX = '''def split_words(text):
    return text.split(",")
'''


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "words.py").write_text(LIB)
    (repo / "tests" / "test_words.py").write_text(TEST)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=e@e", "-c", "user.name=e", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    values = {"SWE_WORKSPACE": str(repo), "FLOW_STATE_ROOT": str(scratch / "state"),
              "FLOW_SHIM": str(scratch / "flow.py"), "FLOW_SPEC": str(scratch / "spec.py"),
              "FLOW_NEW": str(scratch / "new.py")}
    return repo, scratch, {**os.environ, **values}


def flow(env_vars, *args, shim=True, cwd=None, extra=None):
    script = env_vars["FLOW_SHIM"] if shim else str(SCRIPTS / "flow.py")
    done = subprocess.run([sys.executable, script, *args], env={**env_vars, **(extra or {})}, cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_full_loop_locks_first_green(env):
    repo, scratch, vars_ = env
    started = flow(vars_, "--issue", "count split_words commas", shim=False)
    assert "swe_flow ready" in started and "NEXT: write the spec" in started
    assert Path(vars_["FLOW_SHIM"]).is_file()

    # a spec that passes on the original code is rejected
    Path(vars_["FLOW_SPEC"]).write_text('print("PASS 1")\n')
    assert "SPEC REJECTED" in flow(vars_)

    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    accepted = flow(vars_)
    assert "SPEC ACCEPTED" in accepted and "2 FAIL" in accepted
    assert "split_words('a,,b') -> ['a', 'b']" in accepted  # the trace shows where the value is lost

    Path(vars_["FLOW_NEW"]).write_text(FIX)
    green = flow(vars_, "put", "pkg/words.py::split_words")
    assert "PUT OK" in green and "SPEC PASSES" in green and "GREEN" in green
    assert "test_empty_dropped" in green and "changed expectation" in green  # old behaviour, not a crash

    # an edit after GREEN is reverted
    (repo / "pkg" / "words.py").write_text((repo / "pkg" / "words.py").read_text() + "\nX = 1\n")
    (repo / "repro.py").write_text("print(1)\n")
    locked = flow(vars_)
    assert "RESTORED" in locked and "submit_patch" in locked
    assert "X = 1" not in (repo / "pkg" / "words.py").read_text()
    assert not (repo / "repro.py").exists()


def test_put_refuses_code_that_does_not_compile(env):
    repo, scratch, vars_ = env
    flow(vars_, "--issue", "count", shim=False)
    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    flow(vars_)
    Path(vars_["FLOW_NEW"]).write_text("def split_words(text):\n    return text.split(',\n")
    refused = flow(vars_, "put", "pkg/words.py::split_words")
    assert "PUT REFUSED" in refused and "would not compile" in refused
    assert (repo / "pkg" / "words.py").read_text() == LIB


def test_crash_type_failure_is_not_green(env):
    repo, scratch, vars_ = env
    flow(vars_, "--issue", "count", shim=False)
    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    flow(vars_)
    # passes the spec, but test_split now crashes with a TypeError
    Path(vars_["FLOW_NEW"]).write_text(
        'def split_words(text):\n    if text == "a,b":\n        raise TypeError("boom")\n    return text.split(",")\n')
    out = flow(vars_, "put", "pkg/words.py::split_words")
    assert "SPEC" in out and "GREEN" not in out


def test_deadline_restores_best_partial(env):
    repo, scratch, vars_ = env
    flow(vars_, "--issue", "count", shim=False)
    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    flow(vars_)
    # partial fix: fixes 'a,,b' but not '' (1 FAIL instead of 2)
    Path(vars_["FLOW_NEW"]).write_text('def split_words(text):\n    return text.split(",") if text else []\n')
    partial = flow(vars_, "put", "pkg/words.py::split_words")
    assert "SPEC STILL FAILS" in partial and "2 -> 1" in partial
    # then the agent breaks it with a shell edit, and time runs out
    (repo / "pkg" / "words.py").write_text(LIB.replace('if w]', 'if w and False]'))
    out = flow(vars_, extra={"FLOW_SOFT_DEADLINE": "0", "FLOW_HARD_DEADLINE": "0"})
    assert "TIME IS UP" in out and "Restored the best saved state" in out
    assert 'text.split(",") if text else []' in (repo / "pkg" / "words.py").read_text()


def test_show_and_line_range_put(env):
    repo, scratch, vars_ = env
    flow(vars_, "--issue", "count", shim=False)
    shown = flow(vars_, "show", "pkg/words.py::split_words")
    assert "    1  def split_words(text):" in shown and "pkg/words.py:7" in shown  # caller in count()
    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    flow(vars_)
    Path(vars_["FLOW_NEW"]).write_text('    return text.split(",")\n')
    out = flow(vars_, "put", "pkg/words.py:3")
    assert "replaced lines 3-3" in out and "GREEN" in out


def test_tracer_shows_generator_yields(tmp_path):
    (tmp_path / "gen.py").write_text("def lines(s):\n    for part in s.split('|'):\n        yield part.upper()\n")
    script = tmp_path / "run.py"
    script.write_text("import sys\nsys.path.insert(0, %r)\nfrom gen import lines\nprint(list(lines('a|b')))\n"
                      % str(tmp_path))
    done = subprocess.run([sys.executable, str(SCRIPTS / "tracer.py"), str(script)],
                          env={**os.environ, "FLOW_ROOT": str(tmp_path)}, capture_output=True, text=True, timeout=30)
    assert "lines('a|b') yielded ['A', 'B']" in done.stdout


def test_lock_restores_from_head_despite_staged_and_intent_to_add(env):
    repo, scratch, vars_ = env
    flow(vars_, "--issue", "count", shim=False)
    Path(vars_["FLOW_SPEC"]).write_text(SPEC)
    flow(vars_)
    Path(vars_["FLOW_NEW"]).write_text(FIX)
    assert "GREEN" in flow(vars_, "put", "pkg/words.py::split_words")
    saved = subprocess.run(["git", "diff", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
    # the agent keeps going: stages a worse edit and adds a new module with intent-to-add
    (repo / "pkg" / "words.py").write_text(LIB)
    (repo / "pkg" / "extra.py").write_text("Y = 2\n")
    subprocess.run(["git", "add", "pkg/words.py"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-N", "pkg/extra.py"], cwd=repo, check=True)
    assert "RESTORED" in flow(vars_)
    subprocess.run(["git", "add", "-N", "."], cwd=repo, check=True)
    assert subprocess.run(["git", "diff", "HEAD"], cwd=repo, capture_output=True, text=True).stdout == saved
    assert not (repo / "pkg" / "extra.py").exists()
