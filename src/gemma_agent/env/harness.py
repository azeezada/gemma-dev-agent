from __future__ import annotations

import hashlib
import contextlib
import email.parser
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ImportError:  # Python 3.10 local notebook smoke test.
    import tomli as tomllib


MAX_STDOUT_CHARS = 5_000
MAX_FILE_LINES = 150
MAX_FILE_CHARS = 10_000
CONTEXT_COMPACT_AT = 26_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def truncate(value: str, limit: int = MAX_STDOUT_CHARS) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit] + f"\n...[truncated {len(value) - limit} characters]", True


def run_process(
    command: list[str] | str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120,
    shell: bool = False,
    output_limit: int | None = MAX_STDOUT_CHARS,
    log_path: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=str(cwd) if cwd else None, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=shell,
        start_new_session=True,
    )
    timed_out = False
    try:
        full_stdout, full_stderr = process.communicate(timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        full_stdout, full_stderr = process.communicate()
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "$ " + (command if isinstance(command, str) else shlex.join(command))
            + "\n\nSTDOUT\n" + full_stdout + "\n\nSTDERR\n" + full_stderr,
            encoding="utf-8",
        )
    stdout, out_cut = truncate(full_stdout, output_limit) if output_limit is not None else (full_stdout, False)
    stderr, err_cut = truncate(full_stderr, output_limit) if output_limit is not None else (full_stderr, False)
    return {
        "returncode": 124 if timed_out else process.returncode,
        "stdout": stdout, "stderr": stderr, "output_truncated": out_cut or err_cut,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **({"timed_out": True} if timed_out else {}),
    }


class EventLogger:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        self.rows: list[dict[str, Any]] = []
        self.path.write_text("", encoding="utf-8")

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        row = json.loads(json_text({"time_utc": utc_now(), "event": event, **payload}))
        self.rows.append(row)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json_text(row) + "\n")
        return row


@dataclass
class Budget:
    max_tool_calls: int = 18
    max_time_minutes: float = 3.0
    max_turns: int = 50
    command_timeout_seconds: int = 60
    tool_calls_used: int = 0
    turns_used: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_time_minutes * 60 - self.elapsed_seconds)

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    @property
    def exhausted(self) -> bool:
        return (
            self.remaining_seconds <= 0
            or self.remaining_tool_calls <= 0
            or self.turns_used >= self.max_turns
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_remaining": self.remaining_tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "time_seconds_remaining": round(self.remaining_seconds, 3),
            "agent_elapsed_seconds": round(self.elapsed_seconds, 3),
            "max_time_minutes": self.max_time_minutes,
            "command_timeout_seconds": self.command_timeout_seconds,
        }


def safe_extract(archive_path: Path, destination: Path) -> Path:
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe archive member: {member.name}")
        archive.extractall(destination, filter="data")
    if not (destination / ".git").exists():
        children = [child for child in destination.iterdir() if child.name != "__MACOSX"]
        repo_dirs = [child for child in children if child.is_dir() and (child / ".git").exists()]
        if len(repo_dirs) == 1:
            nested = repo_dirs[0]
            staging = destination.parent / f"{destination.name}-flattened"
            if staging.exists():
                shutil.rmtree(staging)
            nested.rename(staging)
            shutil.rmtree(destination)
            staging.rename(destination)
    if not (destination / ".git").exists():
        raise FileNotFoundError(f"Snapshot did not contain a Git repository: {archive_path}")
    return destination


def configure_workspace(workspace: Path) -> None:
    exclude = workspace / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    rules = "\n__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n.coverage\n"
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    exclude.write_text(existing + rules, encoding="utf-8")
    run_process(["git", "config", "user.email", "agent@eval"], cwd=workspace)
    run_process(["git", "config", "user.name", "Agent"], cwd=workspace)
    run_process(["git", "add", "-A"], cwd=workspace)
    run_process(["git", "commit", "-m", "baseline", "--allow-empty", "-q"], cwd=workspace)


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _clean_requirement_name(value: str) -> str:
    value = re.sub(r";.*$", "", value.strip())
    value = re.sub(r"\[.*?\]", "", value)
    return re.split(r"[=<>!~@]", value)[0].strip()


def _workspace_requirement_specs(workspace: Path) -> list[str]:
    """Collect installable requirement specs without losing version constraints."""
    specs: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value.startswith(("#", "-")) or " @ " in value:
            return
        specs.append(value)

    pyproject = workspace / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {})
            for value in project.get("dependencies", []):
                add(value)
            for group in project.get("optional-dependencies", {}).values():
                for value in group:
                    add(value)
            for value in data.get("build-system", {}).get("requires", []):
                add(value)

            dependency_groups = data.get("dependency-groups", {})
            visited: set[str] = set()

            def add_group(name: str) -> None:
                if name in visited:
                    return
                visited.add(name)
                for value in dependency_groups.get(name, []):
                    if isinstance(value, dict) and value.get("include-group"):
                        add_group(str(value["include-group"]))
                    else:
                        add(value)

            for name in dependency_groups:
                if "test" in str(name).casefold():
                    add_group(str(name))
        except Exception:
            pass

    for path in list(workspace.glob("requirements*.txt")) + list(
        workspace.glob("test-requirements*.txt")
    ):
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                add(re.sub(r"\s+#.*$", "", line))
        except Exception:
            pass

    specs.extend(["pytest", "pytest-timeout", "setuptools", "wheel", "packaging"])
    return list(dict.fromkeys(specs))


def _workspace_requirements(workspace: Path) -> set[str]:
    requirements: set[str] = set()
    try:
        from packaging.requirements import Requirement

        for value in _workspace_requirement_specs(workspace):
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate():
                requirements.add(_normalize_package_name(requirement.name))
    except Exception:
        for value in _workspace_requirement_specs(workspace):
            name = _clean_requirement_name(value)
            if name:
                requirements.add(_normalize_package_name(name))
    return requirements


def _compatible_wheels(wheels_dir: Path, specs: list[str] | None = None) -> dict[str, Path]:
    """Select newest compatible wheels while honoring repository constraints."""
    try:
        from packaging.requirements import Requirement
        from packaging.tags import sys_tags
        from packaging.utils import parse_wheel_filename
    except ImportError:
        return {}
    supported = set(sys_tags())
    candidates: dict[str, list[tuple[Any, Path]]] = {}
    for path in wheels_dir.glob("*.whl"):
        try:
            distribution, version, _build, tags = parse_wheel_filename(path.name)
        except Exception:
            continue
        if not supported.intersection(tags):
            continue
        candidates.setdefault(_normalize_package_name(distribution), []).append((version, path))
    constraints: dict[str, list[Any]] = {}
    for value in specs or []:
        try:
            requirement = Requirement(value)
        except Exception:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        constraints.setdefault(_normalize_package_name(requirement.name), []).append(
            requirement.specifier
        )

    selected: dict[str, Path] = {}
    for name, rows in candidates.items():
        matching = [
            row for row in rows
            if all(row[0] in specifier for specifier in constraints.get(name, []))
        ]
        if matching:
            selected[name] = max(matching, key=lambda row: (row[0], row[1].name))[1]
    return selected


def _wheel_requirements(path: Path) -> list[str]:
    """Read Requires-Dist entries from a wheel without installing it."""
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_name = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            metadata = email.parser.Parser().parsestr(
                archive.read(metadata_name).decode("utf-8", "replace")
            )
            return metadata.get_all("Requires-Dist") or []
    except Exception:
        return []


def _expand_wheel_dependencies(
    selected: dict[str, Path], wheels_dir: Path
) -> tuple[dict[str, Path], list[dict[str, str]]]:
    """Add compatible dependency wheels that exist; leave absent deps to the base image."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        return selected, []

    expanded = dict(selected)
    discovered: list[dict[str, str]] = []
    queue = list(selected.values())
    while queue:
        parent = queue.pop(0)
        for value in _wheel_requirements(parent):
            try:
                requirement = Requirement(value)
            except Exception:
                continue
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            name = _normalize_package_name(requirement.name)
            if name in expanded:
                continue
            candidate = _compatible_wheels(wheels_dir, [str(requirement)]).get(name)
            if candidate is None:
                continue
            expanded[name] = candidate
            queue.append(candidate)
            discovered.append({
                "required_by": parent.name,
                "requirement": str(requirement),
                "selected": candidate.name,
            })
    return expanded, discovered


def install_task_dependencies(python: Path, workspace: Path, wheels_dir: Path,
                              pytest_version: str | None = None) -> dict[str, Any]:
    specs = _workspace_requirement_specs(workspace)
    effective_specs = specs
    if pytest_version:
        # Historical development pins can require pytest 6, which cannot use
        # Kaggle's modern py compatibility shim on Python 3.12. This is a
        # verifier/runtime override, not a change to the repository or agent.
        effective_specs = [value for value in specs if _normalize_package_name(_clean_requirement_name(value)) != "pytest"]
        effective_specs.append(f"pytest=={pytest_version}")
    available = _compatible_wheels(wheels_dir, effective_specs)
    if pytest_version and "pytest" not in available:
        raise RuntimeError(f"Required offline pytest=={pytest_version} wheel is missing")
    requested = _workspace_requirements(workspace)
    roots = {name: available[name] for name in sorted(requested) if name in available}
    expanded, dependency_closure = _expand_wheel_dependencies(roots, wheels_dir)
    selected = [expanded[name] for name in sorted(expanded)]
    if not selected:
        return {
            "requirements": specs, "selected": [], "dependency_closure": [], "returncode": 0,
            "stdout": "", "stderr": "",
        }
    result = run_process(
        [
            str(python), "-m", "pip", "install", "--no-index",
            f"--find-links={wheels_dir}", "--no-build-isolation", "--no-deps",
            "--force-reinstall", *map(str, selected),
        ],
        timeout=300, output_limit=None,
    )
    return {
        "requirements": specs,
        "effective_requirements": effective_specs,
        "pytest_runtime_override": pytest_version,
        "selected": [path.name for path in selected],
        "dependency_closure": dependency_closure,
        **result,
    }


def create_task_venv(venv: Path, workspace: Path, wheels_dir: Path,
                     pytest_version: str | None = None) -> Path:
    if not (venv / "bin" / "python").exists():
        # Kaggle's Python 3.12 image can fail inside venv's bundled ensurepip
        # after the pinned vLLM wheel set updates the shared packaging stack.
        # The task only needs an isolated interpreter prefix; its dependencies
        # are intentionally inherited from the already-resolved notebook image.
        result = run_process(
            [
                os.environ.get("PYTHON", "python3"), "-m", "venv",
                "--system-site-packages", "--without-pip", str(venv),
            ],
            timeout=180,
        )
        if result["returncode"] != 0:
            raise RuntimeError(f"Could not create task environment: {result['stderr']}")
    python = venv / "bin" / "python"
    dependency_setup = install_task_dependencies(python, workspace, wheels_dir, pytest_version)
    (venv.parent / "task_dependency_setup.json").write_text(
        json.dumps(dependency_setup, indent=2, default=str), encoding="utf-8"
    )
    if dependency_setup["returncode"] != 0:
        raise RuntimeError(
            "Could not install compatible offline task dependencies: "
            + dependency_setup.get("stderr", "")
        )
    install = run_process(
        [
            str(python), "-m", "pip", "install", "--no-index", f"--find-links={wheels_dir}",
            "--no-build-isolation", "--no-deps", "-e", str(workspace),
        ],
        timeout=180, output_limit=None,
    )
    (venv.parent / "editable_install.json").write_text(json.dumps(install, indent=2), encoding="utf-8")
    if install["returncode"] != 0:
        # Source imports still work through the workspace .pth; retain the installation error in logs.
        (venv / "editable-install-error.log").write_text(
            install["stdout"] + "\n" + install["stderr"], encoding="utf-8"
        )
    install_workspace_paths(python, workspace)
    return python


def task_pytest_plugins(python: Path, workspace: Path) -> str:
    """Load only required test plugins, never Kaggle's unrelated global plugins."""
    if "pytest-httpbin" not in _workspace_requirements(workspace):
        return ""
    check = run_process([str(python), "-c", "import pytest_httpbin.plugin"], timeout=15)
    if check["returncode"] != 0:
        raise RuntimeError("Required pytest-httpbin fixture plugin is unavailable: "
                           + check.get("stderr", "")[-1000:])
    return "pytest_httpbin.plugin"


def install_workspace_paths(python: Path, workspace: Path) -> list[str]:
    """Expose the checkout like the official sandbox's workspace_paths.pth.

    Without this, src/ layouts import an unrelated installed release instead
    of the checkout (httpx 0.28 in v19). A .pth is appended after the standard
    library, so repository modules such as rich/json.py cannot shadow it the
    way PYTHONPATH entries would. In this --system-site-packages venv the .pth
    precedes the inherited system packages, so only the root and src/ are
    added (the official file lists every top-level directory, but after the
    installed packages); other directories could shadow unrelated packages.
    """
    workspace = Path(workspace).resolve()
    located = run_process(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        timeout=60,
    )
    if located["returncode"] != 0 or not located["stdout"].strip():
        raise RuntimeError(f"Could not locate task site-packages: {located['stderr']}")
    site_packages = Path(located["stdout"].strip())
    site_packages.mkdir(parents=True, exist_ok=True)
    paths = [str(workspace)] + ([str(workspace / "src")] if (workspace / "src").is_dir() else [])
    (site_packages / "workspace_paths.pth").write_text("\n".join(paths) + "\n", encoding="utf-8")
    return paths


class WorkspaceTools:
    COUNTED = {
        "run_command", "read_file", "edit_file", "write_file",
        "get_code_neighbors", "search_similar_code", "get_code_subgraph",
        # ADK SkillToolset tools; counted conservatively because the official budget
        # accounting for skill tools is undocumented.
        "load_skill", "load_skill_resource", "run_skill_script",
    }
    FREE = {"get_status", "submit_patch", "list_skills"}
    SKILL_SCRIPT_TIMEOUT_SECONDS = 300  # ADK SkillToolset default

    _FORBIDDEN_COMMAND_PATTERNS = (
        r"(?:^|\s)\.\.(?:/|\s|$)",
        r"/kaggle/input",
        r"tasks\.jsonl",
        r"test_patch",
        r"(?:^|[;&|]\s*)find\s+/",
        r"(?:^|[;&|]\s*)ls\s+/\s*(?:$|[;&|])",
        r"\b(?:curl|wget|ssh|scp|nc|ncat|telnet)\b",
        r"\b(?:pip|pip3|uv)\s+install\b",
        r"\bgit\s+clone\b",
        r"\b(?:apt|apt-get|conda|mamba)\b",
    )

    def __init__(
        self,
        workspace: Path,
        budget: Budget,
        logger: EventLogger,
        *,
        python: Path | None = None,
        graph_path: Path | None = None,
        embeddings_path: Path | None = None,
        allowed_tools: list[str] | set[str] | None = None,
        skills: dict[str, dict[str, Any]] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self._local = threading.local()
        self._skills = dict(skills or {})
        self._allowed_tools = None
        if allowed_tools is not None:
            self._allowed_tools = set(allowed_tools) | (set(SKILL_TOOL_NAMES) if self._skills else set())
        self.budget = budget
        self.logger = logger
        self.python = Path(python) if python else Path(os.environ.get("PYTHON", "python3"))
        self.pytest_plugins = task_pytest_plugins(self.python, self.workspace)
        self.graph_path = Path(graph_path) if graph_path else None
        self.embeddings_path = Path(embeddings_path) if embeddings_path else None
        self.patch_submitted = False
        self.submitted_patch = ""
        self.rejected_commands: list[dict[str, Any]] = []
        self._graph: dict[str, Any] | None = None

    # Scope is per thread: ParallelAgent branches run concurrently with different tool sets.
    @property
    def skills(self) -> dict[str, dict[str, Any]]:
        scope = getattr(self._local, "scope", None)
        return self._skills if scope is None else scope[1]

    @skills.setter
    def skills(self, value: dict[str, dict[str, Any]]) -> None:
        self._skills = value

    @property
    def allowed_tools(self) -> set[str] | None:
        scope = getattr(self._local, "scope", None)
        return self._allowed_tools if scope is None else scope[0]

    @allowed_tools.setter
    def allowed_tools(self, value: set[str] | None) -> None:
        self._allowed_tools = value

    def _resolve(self, filepath: str, *, must_exist: bool = False) -> Path:
        raw = filepath.removeprefix("/workspace/").removeprefix("/workspace")
        raw = raw.lstrip("/")
        if not raw or "\x00" in raw or ".." in Path(raw).parts:
            raise ValueError("Path must be a nonempty workspace-relative path without '..'")
        path = (self.workspace / raw).resolve()
        if self.workspace not in path.parents and path != self.workspace:
            raise ValueError("Path resolves outside the workspace")
        if must_exist and not path.exists():
            raise FileNotFoundError(raw)
        return path

    def _error(self, error_type: str, message: str, **details: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error", "error_type": error_type, "error_message": message,
        }
        if details:
            payload["details"] = details
        return payload

    def _charge(self, name: str) -> dict[str, Any] | None:
        if name not in self.COUNTED:
            return None
        if self.budget.remaining_seconds <= 0:
            return self._error("TimeBudgetExceeded", "The agent time budget is exhausted")
        if self.budget.remaining_tool_calls <= 0:
            return self._error("ToolBudgetExceeded", "The tool-call budget is exhausted")
        self.budget.tool_calls_used += 1
        return None

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        if name not in self.COUNTED | self.FREE or (
            self.allowed_tools is not None and name not in self.allowed_tools
        ):
            result = self._error("UnknownTool", f"Unknown tool: {name}")
        else:
            charged_error = self._charge(name)
            if charged_error:
                result = charged_error
            else:
                try:
                    result = getattr(self, name)(**arguments)
                except TypeError as exc:
                    result = self._error("ValidationError", str(exc))
                except Exception as exc:  # preserve failures as model-visible tool data
                    result = self._error(type(exc).__name__, str(exc))
        if self.budget.max_tool_calls >= 20 and self.budget.remaining_tool_calls <= 10:
            result["budget_warning"] = (
                f"Only {self.budget.remaining_tool_calls} tool call(s) remaining "
                f"({self.budget.tool_calls_used}/{self.budget.max_tool_calls} used)."
            )
        self.logger.emit(
            "tool_result",
            tool=name,
            arguments=arguments,
            result=result,
            elapsed_seconds=round(time.monotonic() - started, 3),
            budget=self.budget.snapshot(),
        )
        return result

    def run_command(self, command: str) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return self._error("ValidationError", "command must be a nonempty string")
        matched = [p for p in self._FORBIDDEN_COMMAND_PATTERNS if re.search(p, command, re.I)]
        if matched:
            rejection = {"command": command, "matched_rules": matched, "time_utc": utc_now()}
            self.rejected_commands.append(rejection)
            self.logger.emit("command_rejected", **rejection)
            return self._error(
                "CommandPolicyError",
                "Command rejected: remain inside /workspace; no network, installs, or host searches.",
            )
        env = {
            "PATH": str(self.python.parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.workspace),
            "PYTHONSAFEPATH": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGINS": self.pytest_plugins,
            "TEST_TMPDIR": "/tmp",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": "/tmp/agent-home",
        }
        timeout = min(self.budget.command_timeout_seconds, self.budget.remaining_seconds)
        completed = run_process(
            ["/bin/bash", "-c", command], cwd=self.workspace, env=env, timeout=timeout,
            log_path=self.logger.root / "commands" / f"{self.budget.tool_calls_used:04d}.log",
        )
        if completed.get("timed_out"):
            return self._error("TimeoutExceeded", f"Command exceeded {timeout}s", **completed)
        if completed["returncode"] != 0:
            return self._error(
                "CommandError",
                f"Command failed with exit code {completed['returncode']}",
                stdout=completed["stdout"], stderr=completed["stderr"],
                exit_code=completed["returncode"],
            )
        return {"status": "ok", "stdout": completed["stdout"], "exit_code": 0}

    def read_file(
        self, filepath: str, start_line: int | None = None, end_line: int | None = None
    ) -> dict[str, Any]:
        path = self._resolve(filepath, must_exist=True)
        if not path.is_file():
            return self._error("ValidationError", f"Not a file: {filepath}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        start = max(1, int(start_line or 1))
        requested_end = int(end_line or (start + MAX_FILE_LINES - 1))
        stop = min(len(lines), requested_end, start + MAX_FILE_LINES - 1)
        selected: list[str] = []
        chars = 0
        for line in lines[start - 1:stop]:
            if chars + len(line) > MAX_FILE_CHARS:
                if not selected:
                    selected.append(line[:MAX_FILE_CHARS])
                break
            selected.append(line)
            chars += len(line)
        content = "".join(selected)
        actual_end = start + len(selected) - 1 if selected else start - 1
        return {
            "status": "ok", "filepath": path.relative_to(self.workspace).as_posix(),
            "content": content, "start_line": start, "end_line": actual_end,
            "total_lines": len(lines),
            "is_truncated": actual_end < min(len(lines), requested_end) or len(content) < len("".join(lines[start - 1:stop])),
        }

    def edit_file(
        self, filepath: str, old_string: str, new_string: str, allow_multiple: bool = False
    ) -> dict[str, Any]:
        path = self._resolve(filepath, must_exist=True)
        if not old_string:
            return self._error("FileEditError", "old_string cannot be empty")
        content = path.read_text(encoding="utf-8", errors="strict").replace("\r\n", "\n")
        strategy = "exact"
        count = content.count(old_string)
        updated = content
        if count:
            if count > 1 and not allow_multiple:
                return self._error("FileEditError", f"old_string matched {count} times")
            updated = content.replace(old_string, new_string, -1 if allow_multiple else 1)
        else:
            old_lines = old_string.splitlines()
            content_lines = content.splitlines(keepends=False)
            matches: list[tuple[int, int]] = []
            for index in range(max(0, len(content_lines) - len(old_lines) + 1)):
                window = content_lines[index:index + len(old_lines)]
                if [line.strip() for line in window] == [line.strip() for line in old_lines]:
                    matches.append((index, index + len(old_lines)))
            if matches:
                if len(matches) > 1 and not allow_multiple:
                    return self._error("FileEditError", f"old_string matched {len(matches)} times")
                strategy = "flexible"
                chosen = matches if allow_multiple else matches[:1]
                for begin, finish in reversed(chosen):
                    indent = re.match(r"\s*", content_lines[begin]).group(0)
                    replacement = [indent + line if line else "" for line in textwrap.dedent(new_string).splitlines()]
                    content_lines[begin:finish] = replacement
                updated = "\n".join(content_lines) + ("\n" if content.endswith("\n") else "")
                count = len(chosen)
            else:
                tokens = [re.escape(token) for token in re.split(r"([():\[\]{}>=<])", old_string) if token]
                pattern = r"\s*".join(tokens)
                found = list(re.finditer(pattern, content, flags=re.DOTALL))
                if not found:
                    return self._error("FileEditError", "old_string was not found")
                if len(found) > 1 and not allow_multiple:
                    return self._error("FileEditError", f"old_string matched {len(found)} times")
                strategy = "regex"
                updated, count = re.subn(
                    pattern, lambda _match: new_string, content,
                    count=0 if allow_multiple else 1, flags=re.DOTALL,
                )
        path.write_text(updated, encoding="utf-8")
        diff = run_process(["git", "diff", "--", str(path.relative_to(self.workspace))], cwd=self.workspace)
        return {
            "status": "ok", "filepath": path.relative_to(self.workspace).as_posix(),
            "occurrences": count, "strategy": strategy, "diff": diff["stdout"],
            "is_truncated": diff.get("output_truncated", False),
        }

    def write_file(self, filepath: str, content: str) -> dict[str, Any]:
        path = self._resolve(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "status": "ok", "filepath": path.relative_to(self.workspace).as_posix(),
            "size": path.stat().st_size,
        }

    def get_status(self) -> dict[str, Any]:
        return {
            **self.budget.snapshot(), "patch_submitted": self.patch_submitted,
            "patch_size": len(self.submitted_patch),
        }

    def submit_patch(self) -> dict[str, Any]:
        added = run_process(["git", "add", "-N", "."], cwd=self.workspace)
        diff = run_process(["git", "diff", "HEAD"], cwd=self.workspace, timeout=30, output_limit=None)
        if added["returncode"] or diff["returncode"]:
            return self._error("PatchExtractionError", added["stderr"] + diff["stderr"])
        self.submitted_patch = diff["stdout"]
        self.patch_submitted = True
        changed = run_process(["git", "diff", "--name-only", "HEAD"], cwd=self.workspace, output_limit=None)
        files = [line for line in changed["stdout"].splitlines() if line.strip()]
        return {"status": "ok", "patch_size": len(self.submitted_patch.encode()), "files_changed": len(files)}

    @contextlib.contextmanager
    def scoped(self, allowed_tools: list[str], skills: dict[str, dict[str, Any]]):
        """Expose another agent's declared tools and skills; budget and workspace stay shared."""
        prior = getattr(self._local, "scope", None)
        chosen = dict(skills)
        self._local.scope = (set(allowed_tools) | (set(SKILL_TOOL_NAMES) if chosen else set()), chosen)
        try:
            yield self
        finally:
            self._local.scope = prior

    # ADK SkillToolset emulation (google/adk-python tools/skill_toolset.py).
    def _skill(self, skill_name: str) -> dict[str, Any]:
        if skill_name not in self.skills:
            raise ValueError(f"Skill '{skill_name}' not found.")
        return self.skills[skill_name]

    def list_skills(self) -> dict[str, Any]:
        return {"status": "ok", "skills": [
            {"name": name, "description": skill["description"]} for name, skill in sorted(self.skills.items())
        ]}

    def load_skill(self, skill_name: str) -> dict[str, Any]:
        skill = self._skill(skill_name)
        return {"status": "ok", "skill_name": skill_name, "instructions": skill["files"]["SKILL.md"]}

    def load_skill_resource(self, skill_name: str, file_path: str) -> dict[str, Any]:
        files = self._skill(skill_name)["files"]
        relative = file_path.lstrip("/")
        if relative not in files or relative == "SKILL.md":
            return self._error("FileNotFoundError", f"Resource '{file_path}' not found in skill '{skill_name}'.")
        content, truncated = truncate(files[relative], MAX_FILE_CHARS)
        return {"status": "ok", "skill_name": skill_name, "file_path": relative,
                "content": content, "is_truncated": truncated}

    def run_skill_script(
        self,
        skill_name: str,
        file_path: str,
        args: dict[str, Any] | list[Any] | None = None,
        short_options: dict[str, Any] | None = None,
        positional_args: list[Any] | None = None,
        command: str | None = None,
    ) -> dict[str, Any]:
        files = self._skill(skill_name)["files"]
        script = file_path[len("scripts/"):] if file_path.startswith("scripts/") else file_path
        relative = f"scripts/{script}"
        if relative not in files:
            return self._error("FileNotFoundError", f"Script '{file_path}' not found in skill '{skill_name}'.")
        if not script.endswith(".py"):
            return self._error("ValidationError", "Only Python skill scripts are supported")
        # Same argv construction as ADK RunSkillScriptTool.
        argv: list[str] = []
        if isinstance(args, list):
            argv.extend(str(value) for value in args)
        else:
            if isinstance(args, dict):
                for key, value in args.items():
                    argv.extend([f"--{key}", str(value)])
            if short_options:
                for key, value in short_options.items():
                    argv.extend([f"-{key}", str(value)])
            if positional_args:
                argv.append("--")
                argv.extend(str(value) for value in positional_args)
        env = {
            "PATH": str(self.python.parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGINS": self.pytest_plugins,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": "/tmp/agent-home",
            # Local stand-in for the official /workspace mount point.
            "SWE_WORKSPACE": str(self.workspace),
        }
        timeout = min(self.SKILL_SCRIPT_TIMEOUT_SECONDS, self.budget.remaining_seconds)
        with tempfile.TemporaryDirectory(prefix=f"skill-{skill_name}-") as materialized:
            root = Path(materialized)
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            completed = run_process(
                [str(self.python), str(root / relative), *argv], cwd=root, env=env, timeout=timeout,
                log_path=self.logger.root / "commands" / f"{self.budget.tool_calls_used:04d}.log",
            )
        result = {
            "skill_name": skill_name, "file_path": file_path,
            "stdout": completed["stdout"], "stderr": completed["stderr"],
            "status": "success" if completed["returncode"] == 0 else "error",
        }
        if completed.get("timed_out"):
            result["status"] = "error"
            result["stderr"] = (result["stderr"] + f"\nScript exceeded {timeout:.0f}s").strip()
        return result

    def _load_graph(self) -> dict[str, Any]:
        if self._graph is None:
            if not self.graph_path or not self.graph_path.is_file():
                raise FileNotFoundError("Code graph is unavailable")
            self._graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
        return self._graph

    def _node_map(self) -> dict[str, dict[str, Any]]:
        return {str(node.get("id") or node.get("name")): node for node in self._load_graph().get("nodes", [])}

    def _resolve_node(self, query: str) -> str:
        nodes = self._node_map()
        if query in nodes:
            return query
        lowered = query.casefold()
        candidates = [name for name in nodes if name.endswith("." + query) or name.endswith("/" + query)]
        if not candidates:
            candidates = [name for name in nodes if name.casefold() == lowered]
        if not candidates:
            candidates = [name for name in nodes if lowered in name.casefold()]
        if not candidates:
            raise KeyError(f"Unknown graph node: {query}")
        return sorted(candidates, key=lambda item: (len(item), item))[0]

    def get_code_neighbors(
        self, node: str, edge_type: str | None = None, max_neighbors: int = 50
    ) -> dict[str, Any]:
        resolved = self._resolve_node(node)
        neighbors: set[str] = set()
        for edge in self._load_graph().get("edges", []):
            if edge_type and str(edge.get("type", "")).casefold() != edge_type.casefold():
                continue
            if edge.get("source") == resolved:
                neighbors.add(str(edge.get("target")))
            if edge.get("target") == resolved:
                neighbors.add(str(edge.get("source")))
        values = sorted(neighbors)[:max(1, min(int(max_neighbors), 200))]
        return {"status": "ok", "node": resolved, "neighbors": values, "count": len(values)}

    def search_similar_code(self, query: str, k: int = 10) -> dict[str, Any]:
        import numpy as np

        if not self.embeddings_path or not self.embeddings_path.is_file():
            raise FileNotFoundError("Code embeddings are unavailable")
        nodes = self._node_map()
        resolved = self._resolve_node(query)
        with np.load(self.embeddings_path, allow_pickle=False) as archive:
            keys = list(archive.files)
            source_key = resolved if resolved in archive else next(
                (key for key in keys if key.endswith(resolved) or resolved.endswith(key)), None
            )
            if source_key is None:
                raise KeyError(f"No embedding for graph node: {resolved}")
            source = np.asarray(archive[source_key], dtype=np.float32).reshape(-1)
            source_norm = float(np.linalg.norm(source)) or 1.0
            scores: list[tuple[float, str]] = []
            for key in keys:
                vector = np.asarray(archive[key], dtype=np.float32).reshape(-1)
                denom = source_norm * (float(np.linalg.norm(vector)) or 1.0)
                scores.append((float(np.dot(source, vector) / denom), key))
        results = []
        for score, name in sorted(scores, reverse=True)[:max(1, min(int(k), 50))]:
            node_data = nodes.get(name, {})
            results.append({
                "node_name": name, "code": str(node_data.get("text", ""))[:2_000],
                "similarity": round(score, 4),
            })
        return {"status": "ok", "query": resolved, "results": results, "count": len(results)}

    def get_code_subgraph(self, nodes: list[str]) -> dict[str, Any]:
        resolved = [self._resolve_node(node) for node in nodes]
        chosen = set(resolved)
        edges = [
            {"from": edge.get("source"), "to": edge.get("target"), "type": edge.get("type")}
            for edge in self._load_graph().get("edges", [])
            if edge.get("source") in chosen and edge.get("target") in chosen
        ]
        return {
            "status": "ok", "nodes": resolved, "edges": edges,
            "node_count": len(resolved), "edge_count": len(edges),
        }


TOOL_SCHEMAS = [
    {"name": "run_command", "description": "Run a focused offline shell command in /workspace.", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    {"name": "read_file", "description": "Read up to 150 lines from a workspace file.", "properties": {"filepath": {"type": "string"}, "start_line": {"type": ["integer", "null"]}, "end_line": {"type": ["integer", "null"]}}, "required": ["filepath"]},
    {"name": "edit_file", "description": "Replace a unique source snippet in a workspace file.", "properties": {"filepath": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "allow_multiple": {"type": "boolean"}}, "required": ["filepath", "old_string", "new_string"]},
    {"name": "write_file", "description": "Create or overwrite a workspace file.", "properties": {"filepath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filepath", "content"]},
    {"name": "get_status", "description": "Return current time, turn, tool-call, and patch status.", "properties": {}, "required": []},
    {"name": "submit_patch", "description": "Capture git diff HEAD and finish the task.", "properties": {}, "required": []},
    {"name": "get_code_neighbors", "description": "Find graph neighbors of a code symbol.", "properties": {"node": {"type": "string"}, "edge_type": {"type": ["string", "null"]}, "max_neighbors": {"type": "integer"}}, "required": ["node"]},
    {"name": "search_similar_code", "description": "Find graph nodes similar to an exact code identifier.", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]},
    {"name": "get_code_subgraph", "description": "Return edges among selected code symbols.", "properties": {"nodes": {"type": "array", "items": {"type": "string"}}}, "required": ["nodes"]},
]


# Mirrors the tools ADK SkillToolset adds when an agent declares skills.
SKILL_TOOL_SCHEMAS = [
    {"name": "list_skills", "description": "Lists all available skills with their names and descriptions.", "properties": {}, "required": []},
    {"name": "load_skill", "description": "Loads the SKILL.md instructions for a given skill.", "properties": {"skill_name": {"type": "string", "description": "The name of the skill to load."}}, "required": ["skill_name"]},
    {"name": "load_skill_resource", "description": "Loads a resource file (from references/, assets/, or scripts/) from within a skill.", "properties": {"skill_name": {"type": "string"}, "file_path": {"type": "string"}}, "required": ["skill_name", "file_path"]},
    {"name": "run_skill_script", "description": "Executes a script from a skill's scripts/ directory.", "properties": {"skill_name": {"type": "string"}, "file_path": {"type": "string"}, "args": {"type": "object", "description": "Arguments passed to the script as --key value pairs."}, "short_options": {"type": "object"}, "positional_args": {"type": "array", "items": {"type": "string"}}}, "required": ["skill_name", "file_path"]},
]
SKILL_TOOL_NAMES = tuple(row["name"] for row in SKILL_TOOL_SCHEMAS)


def skill_catalog_instruction(skills: dict[str, dict[str, Any]]) -> str:
    """Approximates the skill catalog ADK SkillToolset appends to the system instruction."""
    if not skills:
        return ""
    rows = "\n".join(f"- {name}: {skill['description']}" for name, skill in sorted(skills.items()))
    return (
        "\n\n## Skills\n"
        "You can use specialized 'skills' to help you with complex tasks. You MUST use the skill "
        "tools to interact with these skills. Each skill folder contains SKILL.md (the main "
        "instruction file) and optionally references/, assets/, and scripts/ (executable scripts run "
        "with run_skill_script). If a skill seems relevant to the current task, you MUST use the "
        "load_skill tool to read its instructions, then follow them exactly.\n\n"
        f"Available skills:\n{rows}"
    )


def agent_tool_schema(name: str, description: str) -> dict[str, Any]:
    """ADK AgentTool declaration for an agent without input_schema: one string `request`."""
    return {"name": name, "description": description,
            "properties": {"request": {"type": "string"}}, "required": ["request"]}


_STATE_PLACEHOLDER = re.compile(r"{+[^{}]*}+")


def inject_session_state(template: str, state: dict[str, Any]) -> str:
    """Mirror ADK instructions_utils.inject_session_state for plain session keys.

    `{key}` is replaced from state and raises KeyError when missing; `{key?}` becomes
    empty when missing; text that is not a valid state name (JSON examples) is kept.
    """
    def replace(match: re.Match) -> str:
        name = match.group().lstrip("{").rstrip("}").strip()
        optional = name.endswith("?")
        name = name.removesuffix("?")
        if name.startswith("artifact."):
            raise ValueError("Artifact placeholders are not emulated")
        prefix, _, rest = name.partition(":")
        valid = rest.isidentifier() if prefix in {"app", "user", "temp"} and rest else name.isidentifier()
        if not valid:
            return match.group()
        if name in state:
            return str(state[name])
        if optional:
            return ""
        raise KeyError(f"Context variable not found: `{name}`.")
    return _STATE_PLACEHOLDER.sub(replace, template)


def openai_tools(schemas: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": row["name"], "description": row["description"],
                "parameters": {
                    "type": "object", "properties": row["properties"],
                    "required": row["required"], "additionalProperties": False,
                },
            },
        }
        for row in (TOOL_SCHEMAS if schemas is None else schemas)
    ]


class ModelHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"vLLM HTTP {status}: {detail}")
        self.status = status


def is_context_length_error(exc: BaseException) -> bool:
    """vLLM rejects over-long prompts with HTTP 400 before any generation."""
    text = str(exc).casefold()
    return (
        isinstance(exc, ModelHTTPError) and exc.status == 400
        and ("maximum context length" in text or "reduce the length" in text)
    )


@contextlib.contextmanager
def request_deadline(seconds: float):
    """Enforce total wall time, including a server that slowly streams response bytes."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield  # socket timeout still bounds blocking IO on platforms without alarms
        return
    prior_handler = signal.getsignal(signal.SIGALRM)
    prior_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(_signum, _frame):
        raise TimeoutError("Model request exceeded its wall-clock deadline")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, min(seconds, prior_timer[0]) if prior_timer[0] else seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)
        if prior_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, prior_timer[0] - (time.monotonic() - started)), prior_timer[1])


def template_token_ids(encoded: Any) -> list[int]:
    """Normalize apply_chat_template(tokenize=True) across transformers versions.

    Newer releases return a BatchEncoding (a mapping with input_ids and
    attention_mask); len() of that mapping is 2, which silently disabled
    context compaction in v19.
    """
    if hasattr(encoded, "keys") and "input_ids" in encoded:
        encoded = encoded["input_ids"]
    if hasattr(encoded, "tolist"):
        encoded = encoded.tolist()
    encoded = list(encoded)
    if encoded and isinstance(encoded[0], (list, tuple)):
        encoded = list(encoded[0])
    return [int(token) for token in encoded]


def template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages as vLLM hands them to the chat template: tool-call arguments parsed from JSON strings."""
    normalized = json.loads(json.dumps(messages))
    for message in normalized:
        for call in message.get("tool_calls") or []:
            raw = call["function"].get("arguments", "{}")
            if isinstance(raw, str):
                try:
                    call["function"]["arguments"] = json.loads(raw)
                except ValueError:
                    # Keep malformed calls observable; model receives their tool error.
                    call["function"]["arguments"] = {"invalid_arguments": raw}
    return normalized


class OpenAIHTTPClient:
    def __init__(self, base_url: str, model: str, timeout: int = 240):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.tokenizer = None
        # vLLM >= 0.10.2 returns prompt_token_ids and choices[].token_ids; generation is unaffected.
        self.return_token_ids = True
        self._local = threading.local()
        self._tool_definitions = openai_tools()
        self.reasoning_field = "reasoning"

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        # Per-thread override so concurrent ParallelAgent branches advertise their own tools.
        local = getattr(self._local, "tool_definitions", None)
        return self._tool_definitions if local is None else local

    @tool_definitions.setter
    def tool_definitions(self, value: list[dict[str, Any]] | None) -> None:
        if threading.current_thread() is threading.main_thread():
            self._tool_definitions = value if value is not None else openai_tools()
            self._local.tool_definitions = None
        else:
            self._local.tool_definitions = value

    def configure_tokenizer(self, model_path: Path, tool_names: list[str],
                            extra_schemas: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
        self.tool_definitions = [
            tool for tool in openai_tools(TOOL_SCHEMAS + SKILL_TOOL_SCHEMAS + list(extra_schemas or []))
            if tool["function"]["name"] in tool_names
        ]
        # Probe the attached template rather than assuming a particular revision.
        probe = [
            {"role": "user", "content": "Inspect status."},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "probe", "type": "function",
                "function": {"name": "get_status", "arguments": {}},
            }]},
            {"role": "tool", "tool_call_id": "probe", "name": "get_status", "content": "{}"},
        ]
        for key in ("reasoning", "reasoning_content"):
            probe[1][key] = "REASONING_TEMPLATE_SENTINEL"
            rendered = self.tokenizer.apply_chat_template(
                probe, tools=self.tool_definitions, tokenize=False, add_generation_prompt=True,
                enable_thinking=True,
            )
            del probe[1][key]
            if "REASONING_TEMPLATE_SENTINEL" in rendered:
                self.reasoning_field = key
                break
        else:
            self.reasoning_field = "content_channels"
        return {"reasoning_history_representation": self.reasoning_field,
                "chat_template_sha256": sha256_text(str(self.tokenizer.chat_template))}

    def assistant_history(self, message: dict[str, Any]) -> dict[str, Any]:
        result = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
            reasoning = message.get("reasoning") or message.get("reasoning_content")
            if reasoning:
                if self.reasoning_field == "content_channels":
                    result["content"] = "<|channel>thought\n" + reasoning + "<channel|>" + result["content"]
                else:
                    result[self.reasoning_field] = reasoning
        return result

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        if self.tokenizer is None:
            raise RuntimeError("Configure the attached tokenizer before inference")
        encoded = self.tokenizer.apply_chat_template(
            template_messages(messages), tools=self.tool_definitions, tokenize=True,
            add_generation_prompt=True, enable_thinking=True,
        )
        return len(template_token_ids(encoded))

    def health(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url.removesuffix("/v1") + "/health", timeout=5) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip().startswith("{") else {"status": raw or "ok"}

    def models(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url + "/models", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def chat(self, messages: list[dict[str, Any]], **settings: Any) -> dict[str, Any]:
        timeout = min(self.timeout, settings.pop("request_timeout", self.timeout))
        if timeout <= 0:
            raise TimeoutError("No model request time remains")
        payload = {"model": self.model, "messages": messages, "tools": self.tool_definitions, **settings}
        if self.return_token_ids:
            payload["return_token_ids"] = True
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
            method="POST",
        )
        try:
            with request_deadline(timeout), urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:4_000]
            raise ModelHTTPError(exc.code, detail) from exc


def gpu_inventory() -> list[dict[str, Any]]:
    try:
        result = run_process([
            "nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,driver_version",
            "--format=csv,noheader,nounits",
        ])
    except FileNotFoundError:
        return []
    rows = []
    if result["returncode"] != 0:
        return rows
    for line in result["stdout"].splitlines():
        parts = [piece.strip() for piece in line.split(",")]
        if len(parts) == 6:
            rows.append({
                "index": int(parts[0]), "name": parts[1], "memory_used_mib": int(parts[2]),
                "memory_total_mib": int(parts[3]), "utilization_percent": int(parts[4]),
                "driver_version": parts[5],
            })
    return rows


class VLLMServer:
    def __init__(
        self,
        *,
        model_path: Path,
        model_name: str,
        log_path: Path,
        port: int = 8000,
        startup_timeout: int = 1_800,
        max_model_len: int = 32_768,
        enable_lora: bool = False,
        lora_modules: dict[str, Path] | None = None,
    ):
        self.model_path = Path(model_path)
        self.model_name = model_name
        self.log_path = Path(log_path)
        self.port = port
        self.startup_timeout = startup_timeout
        self.max_model_len = max_model_len
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        # FlashInfer 0.6.x JIT-compiles its fused top-k/top-p sampler during
        # vLLM warm-up.  Kaggle exposes the NVIDIA driver as libcuda.so.1 but
        # not the unversioned libcuda.so linker name, so that optional sampler
        # fails at link time.  vLLM's documented opt-out keeps the same sampling
        # semantics through its native CUDA/Triton implementation.
        self.environment = {
            "VLLM_USE_FLASHINFER_SAMPLER": "0",
            "VLLM_NO_USAGE_STATS": "1",
        }
        self.command = [
            sys.executable, "-m", "vllm.entrypoints.openai.api_server",
            "--model", str(self.model_path),
            "--served-model-name", self.model_name,
            "--tensor-parallel-size", "4",
            "--max-model-len", str(self.max_model_len),
            "--gpu-memory-utilization", "0.90",
            "--enable-auto-tool-choice",
            "--tool-call-parser", "gemma4",
            "--reasoning-parser", "gemma4",
            "--default-chat-template-kwargs", '{"enable_thinking":true}',
            "--host", "127.0.0.1",
            "--port", str(self.port),
        ]
        if enable_lora or lora_modules:
            # Competition serving flags (HARNESS_README 3.1); each adapter is served under its directory name.
            self.command += ["--enable-lora", "--max-loras", "8", "--max-lora-rank", "128"]
            for name, path in (lora_modules or {}).items():
                self.command += ["--lora-modules", f"{name}={path}"]

    @property
    def client(self) -> OpenAIHTTPClient:
        return OpenAIHTTPClient(f"http://127.0.0.1:{self.port}/v1", self.model_name)

    def start(self, status_callback: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("w", encoding="utf-8")
        started = time.monotonic()
        server_env = os.environ.copy()
        server_env.update(self.environment)
        self.process = subprocess.Popen(
            self.command,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=server_env,
            start_new_session=True,
        )
        deadline = started + self.startup_timeout
        next_status = started
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log_handle.flush()
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-8_000:]
                self.stop()
                raise RuntimeError(f"vLLM exited during startup:\n{tail}")
            try:
                health = self.client.health()
                elapsed = time.monotonic() - started
                return {
                    "health": health,
                    "startup_seconds": round(elapsed, 3),
                    "reused": False,
                    "command": self.command,
                    "environment": self.environment,
                    "gpu": gpu_inventory(),
                }
            except Exception:
                pass
            if time.monotonic() >= next_status:
                self.log_handle.flush()
                lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                status = {
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "last_log": lines[-1][-500:] if lines else "waiting for server output",
                    "gpu": gpu_inventory(),
                }
                if status_callback:
                    status_callback(status)
                next_status = time.monotonic() + 30
            time.sleep(1)
        self.stop()
        raise TimeoutError(f"vLLM did not become healthy within {self.startup_timeout}s")

    def ensure_started(
        self,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Attach to the expected healthy server, or start it once for this session."""
        try:
            health = self.client.health()
            models = self.client.models()
        except Exception:
            return self.start(status_callback=status_callback)

        model_ids = {
            str(row.get("id"))
            for row in models.get("data", [])
            if isinstance(row, dict) and row.get("id") is not None
        }
        if self.model_name not in model_ids:
            raise RuntimeError(
                f"Port {self.port} already serves {sorted(model_ids) or 'an unknown model'}, "
                f"not {self.model_name!r}"
            )
        return {
            "health": health,
            "models": sorted(model_ids),
            "startup_seconds": 0.0,
            "reused": True,
            "command": self.command,
            "environment": self.environment,
            "gpu": gpu_inventory(),
        }

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=15)
        if self.log_handle is not None and not self.log_handle.closed:
            self.log_handle.close()


def compact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    tool_indexes = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
    compactable = set(tool_indexes[:-2])
    changed = 0
    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in compactable:
            compacted.append(message)
            continue
        content = str(message.get("content", ""))
        try:
            value = json.loads(content)
            summary = {
                "status": value.get("status"),
                "error_type": value.get("error_type"),
                "filepath": value.get("filepath"),
                "exit_code": value.get("exit_code") or value.get("details", {}).get("exit_code"),
                "original_chars": len(content),
                "sha256": sha256_text(content),
                "compacted": True,
            }
        except Exception:
            summary = {"original_chars": len(content), "sha256": sha256_text(content), "compacted": True}
        compacted.append({**message, "content": json_text(summary)})
        changed += 1
    return compacted, changed


def workspace_tree(workspace: Path, limit: int = 150) -> str:
    rows = []
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if len(relative.parts) <= 3:
            rows.append("./" + relative.as_posix() + ("/" if path.is_dir() else ""))
        if len(rows) >= limit:
            break
    return "\n".join(rows)


def build_task_prompt(task: dict[str, Any], workspace: Path, budget: Budget) -> str:
    hints = str(task.get("hints_text") or "").strip()
    hint_section = f"\n\n## Hints\n{hints}" if hints else ""
    return f"""You are evaluating a software engineering task for repository {task['repo']}.

## Problem Statement
{task['problem_statement']}{hint_section}

## Task Budget
- Time allowance: {budget.max_time_minutes:g} minutes
- Tool calls allowance: {budget.max_tool_calls}
- Max turns: {budget.max_turns}
- Single command timeout: {budget.command_timeout_seconds} seconds
- Command output limit: {MAX_STDOUT_CHARS} characters
- File view limit: {MAX_FILE_LINES} lines / {MAX_FILE_CHARS} characters

The environment is offline. Work only inside /workspace. Dependencies are already prepared.

## Workspace layout
{workspace_tree(workspace)}
"""


def run_agent(
    client: Any,
    task: dict[str, Any],
    system_prompt: str,
    tools: WorkspaceTools,
    logger: EventLogger,
    *,
    temperature: float = 0.2,
    top_p: float = 0.95,
    max_tokens: int = 8192,
    thinking_budget: int = 2048,
) -> dict[str, Any]:
    allowed_task_keys = {"instance_id", "repo", "problem_statement", "hints_text"}
    exposed_task = {key: task.get(key, "") for key in allowed_task_keys}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_task_prompt(exposed_task, tools.workspace, tools.budget)},
    ]
    transcript: list[str] = []
    consecutive_nudges = 0
    prompt_tokens_max = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    compactions = 0
    termination = "unknown"
    model_calls = 0

    while True:
        if tools.patch_submitted:
            termination = "submitted_patch"
            break
        if tools.budget.remaining_seconds <= 0:
            termination = "time_budget_exhausted"
            break
        if tools.budget.turns_used >= tools.budget.max_turns:
            termination = "turn_budget_exhausted"
            break
        tools.budget.turns_used += 1
        request_started = time.monotonic()
        try:
            response = client.chat(
                messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                tool_choice="auto",
                chat_template_kwargs={"enable_thinking": True, "thinking_budget": thinking_budget},
            )
        except RuntimeError as exc:
            if "context" in str(exc).casefold() and len(messages) > 4:
                messages, changed = compact_messages(messages)
                compactions += int(changed > 0)
                logger.emit("context_compaction", reason="api_context_error", messages_compacted=changed)
                if changed:
                    continue
            raise
        model_calls += 1
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        tool_calls = message.get("tool_calls") or []
        usage = response.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        prompt_tokens_max = max(prompt_tokens_max, prompt_tokens)
        total_prompt_tokens += prompt_tokens
        total_completion_tokens += completion_tokens
        logger.emit(
            "model_turn",
            turn=tools.budget.turns_used,
            latency_seconds=round(time.monotonic() - request_started, 3),
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_chars=len(reasoning),
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            budget=tools.budget.snapshot(),
        )
        transcript.append(
            f"## Turn {tools.budget.turns_used}\n\n"
            f"**Reasoning**\n\n{reasoning or '(none)'}\n\n"
            f"**Assistant**\n\n{content or '(tool call)'}\n"
        )
        assistant_message = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        if not tool_calls:
            consecutive_nudges += 1
            if consecutive_nudges > 3:
                termination = "max_nudges"
                break
            nudge = "Continue using the available tools, or call submit_patch when the fix is ready."
            messages.append({"role": "user", "content": nudge})
            transcript.append(f"**Harness nudge**: {nudge}\n")
            continue

        consecutive_nudges = 0
        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception as exc:
                result = {
                    "status": "error", "error_type": "ToolArgumentsJSONError",
                    "error_message": str(exc), "raw_arguments": str(raw_args)[:1_000],
                }
            else:
                result = tools.dispatch(name, arguments)
            messages.append({
                "role": "tool", "tool_call_id": call.get("id", name),
                "name": name, "content": json_text(result),
            })
            transcript.append(
                f"**Tool `{name}`**\n\n```json\n{json.dumps(arguments if 'arguments' in locals() else {}, indent=2)}\n```\n\n"
                f"```json\n{json.dumps(result, indent=2, default=str)[:8_000]}\n```\n"
            )
            if tools.patch_submitted:
                break
        if prompt_tokens >= CONTEXT_COMPACT_AT:
            messages, changed = compact_messages(messages)
            if changed:
                compactions += 1
                logger.emit(
                    "context_compaction", reason="prompt_threshold",
                    prompt_tokens=prompt_tokens, messages_compacted=changed,
                )

    if not tools.submitted_patch:
        recovered = tools.submit_patch()
        logger.emit("automatic_patch_recovery", result=recovered)
    return {
        "termination_reason": termination,
        "patch": tools.submitted_patch,
        "patch_submitted_explicitly": tools.patch_submitted and termination == "submitted_patch",
        "model_calls": model_calls,
        "prompt_tokens_total": total_prompt_tokens,
        "prompt_tokens_max": prompt_tokens_max,
        "completion_tokens_total": total_completion_tokens,
        "context_compactions": compactions,
        "budget": tools.budget.snapshot(),
        "messages": messages,
        "transcript": "\n".join(transcript),
        "rejected_commands": tools.rejected_commands,
    }


def parse_patch_targets(patch_text: str) -> list[str]:
    targets = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in targets:
                targets.append(path)
    return targets


def target_test_names(patch_text: str) -> list[str]:
    """Test functions a test patch adds or modifies (the task's FAIL_TO_PASS candidates).

    Uses the enclosing-function context git writes into hunk headers, updated
    by any def test_* line seen inside the hunk before a changed line.
    """
    names: list[str] = []
    current: str | None = None
    in_test_file = False
    definition = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip().removeprefix("b/")
            in_test_file = path.endswith(".py") and "test" in Path(path).name
            current = None
            continue
        if not in_test_file or line.startswith("--- "):
            continue
        if line.startswith("@@"):
            header = definition.search(line.split("@@", 2)[-1])
            current = header.group(1) if header else None
            continue
        body = line[1:] if line[:1] in {" ", "+", "-"} else line
        found = definition.match(body)
        if found:
            current = found.group(1)
        if line[:1] in {"+", "-"} and current and current not in names:
            # Decorators belong to the following definition, not the enclosing one.
            if body.strip() and not body.lstrip().startswith("@"):
                names.append(current)
    return names


def apply_patch(workspace: Path, patch_text: str, patch_file: Path) -> dict[str, Any]:
    if not patch_text.strip():
        return {"ok": True, "attempts": [], "empty_patch": True}
    normalized_patch = patch_text if patch_text.endswith("\n") else patch_text + "\n"
    patch_file.write_text(normalized_patch, encoding="utf-8")
    attempts = [
        ["git", "apply", "--unsafe-paths", str(patch_file)],
        ["git", "apply", "-3", "--unsafe-paths", str(patch_file)],
        ["git", "apply", "--ignore-space-change", "--ignore-whitespace", "--unsafe-paths", str(patch_file)],
        ["git", "apply", "--recount", "--unsafe-paths", str(patch_file)],
        ["patch", "--batch", "--forward", "-p1", "-i", str(patch_file)],
    ]
    results = []
    for command in attempts:
        result = run_process(command, cwd=workspace, timeout=60)
        results.append({"command": command, **result})
        if result["returncode"] == 0:
            return {"ok": True, "attempts": results}
    return {"ok": False, "attempts": results}


def verify_patch(
    *,
    snapshot: Path,
    workspace: Path,
    agent_patch: str,
    test_patch: str,
    python: Path,
    output_log: Path,
    wheels_dir: Path | None = None,
    venv: Path | None = None,
    pytest_version: str | None = None,
) -> dict[str, Any]:
    safe_extract(snapshot, workspace)
    configure_workspace(workspace)
    if venv is not None:
        if wheels_dir is None:
            raise ValueError("Independent verification needs a wheel directory")
        python = create_task_venv(venv, workspace, wheels_dir, pytest_version)
    install_command = [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps"]
    if wheels_dir is not None:
        install_command.extend(["--no-index", f"--find-links={wheels_dir}"])
    install_command.extend(["-e", str(workspace)])
    editable_install = run_process(install_command, timeout=180, output_limit=None,
                                   log_path=output_log.with_suffix(".install.log"))
    work = workspace.parent
    agent_patch_path = work / "agent.patch"
    test_patch_path = work / "tests.patch"
    agent_apply = apply_patch(workspace, agent_patch, agent_patch_path)
    if not agent_apply["ok"]:
        result = {"resolved": False, "error": "agent_patch_apply_failed", "agent_apply": agent_apply}
        output_log.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return result
    targets = parse_patch_targets(test_patch)
    if not targets:
        raise ValueError("Gold test patch contains no verification targets")
    if targets:
        for target in targets:
            run_process(["git", "checkout", "HEAD", "--", target], cwd=workspace)
            run_process(["git", "clean", "-f", "--", target], cwd=workspace)
    test_apply = apply_patch(workspace, test_patch, test_patch_path)
    if not test_apply["ok"]:
        result = {
            "resolved": False, "error": "test_patch_apply_failed",
            "agent_apply": agent_apply, "test_apply": test_apply, "pytest_targets": targets,
        }
        output_log.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return result
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(workspace),
        "PYTHONSAFEPATH": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": task_pytest_plugins(python, workspace),
        "TEST_TMPDIR": "/tmp",
    })
    dependency_preflight = run_process(
        [
            str(python), "-c",
            """import importlib.metadata as m
import importlib.util as u

def version(name):
    try:
        return m.version(name)
    except m.PackageNotFoundError:
        return "unknown"

import pytest
print("pytest=" + version("pytest"))
if u.find_spec("pydantic"):
    import pydantic
    print("pydantic=" + version("pydantic"))
    print("pydantic-core=" + version("pydantic-core"))
else:
    print("pydantic=not-required")
""",
        ],
        cwd=workspace,
        env=env,
        timeout=60,
    )
    if dependency_preflight["returncode"] != 0:
        result = {
            "resolved": False,
            "error": "dependency_preflight_failed",
            "dependency_preflight": dependency_preflight,
            "editable_install_exit_code": editable_install["returncode"],
            "agent_apply": agent_apply,
            "test_apply": test_apply,
            "pytest_targets": targets,
        }
        output_log.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return result
    command = [
        str(python), "-m", "pytest", *targets, "-p", "no:anyio", "-o", "timeout=0",
        "-o", "norecursedirs=.* build dist venv", "-o", "python_classes=Test* *Test", "-q",
    ]
    test_result = run_process(command, cwd=workspace, env=env, timeout=600,
                              output_limit=None, log_path=output_log)
    output = test_result["stdout"] + test_result["stderr"]
    failed_tests = bool(re.search(r"\b[1-9][0-9]* failed\b", output))
    test_errors = bool(re.search(r"\b[1-9][0-9]* errors?\b|ERROR collecting|ModuleNotFoundError", output))
    return {
        "resolved": test_result["returncode"] == 0,
        "pytest_targets": targets,
        "pytest_exit_code": test_result["returncode"],
        "test_failure": test_result["returncode"] == 1 and failed_tests and not test_errors,
        "infrastructure_error": test_result["returncode"] != 0 and not (
            test_result["returncode"] == 1 and failed_tests and not test_errors),
        "python": str(python),
        "pytest_elapsed_seconds": test_result["elapsed_seconds"],
        "editable_install_exit_code": editable_install["returncode"],
        "dependency_preflight": dependency_preflight,
        "pytest_plugin_autoload_disabled": True,
        "agent_apply": agent_apply,
        "test_apply": test_apply,
    }


def build_submission_zip(files: dict[str, str], destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            archive.writestr(member, content)
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        bad = [name for name in names if name.startswith("/") or ".." in Path(name).parts]
        if bad or "agent.yaml" not in names:
            raise ValueError(f"Invalid submission archive: bad={bad}, names={names}")
        if any(name.startswith("adapters/") for name in names):
            raise ValueError("MVP v2 must not contain adapters")
    return {
        "path": str(destination), "size_bytes": destination.stat().st_size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "files": names,
    }


def write_leak_audit(
    destination: Path,
    *,
    task: dict[str, Any],
    agent_result: dict[str, Any],
) -> dict[str, Any]:
    first_messages = agent_result.get("messages", [])[:2]
    exposed = json_text(first_messages)
    forbidden_literals = ["tasks.jsonl", "/kaggle/input", "test_patch"]
    audit = {
        "instance_id": task["instance_id"],
        "controller_fields_exposed": ["instance_id", "repo", "problem_statement", "hints_text"],
        "reference_patch_sha256": sha256_text(str(task.get("patch", ""))),
        "test_patch_sha256": sha256_text(str(task.get("test_patch", ""))),
        "forbidden_literal_hits_in_initial_messages": [item for item in forbidden_literals if item in exposed],
        "rejected_commands": agent_result.get("rejected_commands", []),
        "reference_material_passed_to_model": False,
        "verifier_started_after_agent": True,
    }
    audit["passed"] = not audit["forbidden_literal_hits_in_initial_messages"]
    destination.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def zip_directory(root: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root))
