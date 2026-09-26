"""Assemble experiment submissions: candidate configs plus the shared skill library."""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
LIB = HERE / "lib" / "skills"
# swe_branch runs the same checks inside candidate checkouts, so it carries the edit scripts.
BRANCH_SHARED = ("apply_edit.py", "run_tests.py", "verify.py", "finalize.py")

# name -> source directory. Every directory under candidates/ with an agent.yaml is a candidate
# (sorted by name, so A_..F_ run in order); the unchanged v22 submission is the control.
CANDIDATES = {
    **{path.parent.name: path.parent for path in sorted((PROJECT / "harnesses" / "candidates").glob("*/agent.yaml"))},
    "baseline_v22": PROJECT / "harnesses" / "v22",
}


# Candidate-side notes and tests stay out of the submission.
NOT_SUBMITTED = {"CANDIDATE.md", "notes", "tests"}


def _read_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".pyc" and path.relative_to(root).parts[0] not in NOT_SUBMITTED
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
    }


def describe(name: str) -> tuple[str, str]:
    """(design, basis) from the candidate's CANDIDATE.md `design:` / `basis:` lines, for the notebook table."""
    if name == "baseline_v22":
        return "Unchanged v22 submission (single agent)", "control"
    card = CANDIDATES[name] / "CANDIDATE.md"
    text = card.read_text(encoding="utf-8") if card.is_file() else ""
    field = lambda key: next((line.split(":", 1)[1].strip() for line in text.splitlines()
                              if line.lower().startswith(key + ":")), "-")
    return field("design"), field("basis")


def skill_files(name: str) -> dict[str, str]:
    files = {f"skills/{name}/{rel}": text for rel, text in _read_tree(LIB / name).items()}
    if name == "swe_branch":
        for script in BRANCH_SHARED:
            files[f"skills/swe_branch/scripts/{script}"] = (LIB / "swe_edit" / "scripts" / script).read_text()
    return files


def assemble(name: str) -> dict[str, str]:
    source = CANDIDATES[name]
    files = _read_tree(source)
    if name == "baseline_v22":
        return files
    declared = sorted({match for text in files.values() if isinstance(text, str)
                       for match in re.findall(r"^\s*-\s*skills/(\w+)\s*$", text, re.M)})
    for skill in declared:
        # A candidate may ship its own skills/<name>/; those win over the shared library.
        if not any(path.startswith(f"skills/{skill}/") for path in files):
            files.update(skill_files(skill))
    return files


def assemble_all() -> dict[str, dict[str, str]]:
    return {name: assemble(name) for name in CANDIDATES}
