"""Session teardown: remove scratch state that skill-script tests leave in the real environment.

swe_edit's guard installs `swe_guard.pth` into the running Python's site-packages, and the
E_conductor tests use the fixed /tmp paths the official container would. Neither should outlive
the test session and leak into later runs.
"""
from __future__ import annotations

import shutil
import site
from pathlib import Path

SCRATCH = ("/tmp/swe_state", "/tmp/flow.py", "/tmp/spec.py", "/tmp/new.py")


def pytest_sessionfinish(session, exitstatus):
    for directory in site.getsitepackages():
        Path(directory, "swe_guard.pth").unlink(missing_ok=True)
    for path in map(Path, SCRATCH):
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
