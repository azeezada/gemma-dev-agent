from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "1.0"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SchemaError(ValueError):
    """Raised when a public harness document is invalid."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{label} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise SchemaError(f"Unsupported {label} fields: {sorted(unknown)}")


def _text(value: Any, label: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise SchemaError(f"{label} must be a nonempty string")
    return value


def _path(value: Any, label: str, *, required: bool = True) -> Path | None:
    if value in (None, "") and not required:
        return None
    text = _text(value, label)
    return Path(text).expanduser().resolve()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class TaskSpec:
    schema_version: str
    task_id: str
    repository: str
    problem_statement: str
    snapshot_path: Path
    hints: str = ""
    graph_path: Path | None = None
    embeddings_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskSpec":
        raw = _mapping(raw, "TaskSpec")
        allowed = {
            "schema_version", "task_id", "repository", "problem_statement", "hints",
            "snapshot_path", "graph_path", "embeddings_path",
        }
        _reject_unknown(raw, allowed, "TaskSpec")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SchemaError(f"Unsupported TaskSpec schema_version: {raw.get('schema_version')!r}")
        task_id = _text(raw.get("task_id"), "task_id")
        if not _TASK_ID.fullmatch(task_id):
            raise SchemaError("task_id contains unsupported characters")
        spec = cls(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            repository=_text(raw.get("repository"), "repository"),
            problem_statement=_text(raw.get("problem_statement"), "problem_statement"),
            hints=_text(raw.get("hints", ""), "hints", required=False),
            snapshot_path=_path(raw.get("snapshot_path"), "snapshot_path"),
            graph_path=_path(raw.get("graph_path"), "graph_path", required=False),
            embeddings_path=_path(raw.get("embeddings_path"), "embeddings_path", required=False),
        )
        if not spec.snapshot_path.exists():
            raise SchemaError(f"snapshot_path does not exist: {spec.snapshot_path}")
        for name in ("graph_path", "embeddings_path"):
            path = getattr(spec, name)
            if path is not None and not path.is_file():
                raise SchemaError(f"{name} is not a file: {path}")
        return spec

    @classmethod
    def load(cls, path: Path | str) -> "TaskSpec":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def public_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class VerificationSpec:
    schema_version: str
    task_id: str
    test_patch_path: Path
    verification_command: str
    setup_command: str = ""
    reference_bundle_path: Path | None = None
    timeout_seconds: int = 600

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VerificationSpec":
        raw = _mapping(raw, "VerificationSpec")
        allowed = {
            "schema_version", "task_id", "test_patch_path", "verification_command",
            "setup_command", "reference_bundle_path", "timeout_seconds",
        }
        _reject_unknown(raw, allowed, "VerificationSpec")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SchemaError(f"Unsupported VerificationSpec schema_version: {raw.get('schema_version')!r}")
        timeout = raw.get("timeout_seconds", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 7200:
            raise SchemaError("timeout_seconds must be an integer from 1 to 7200")
        spec = cls(
            schema_version=SCHEMA_VERSION,
            task_id=_text(raw.get("task_id"), "task_id"),
            test_patch_path=_path(raw.get("test_patch_path"), "test_patch_path"),
            verification_command=_text(raw.get("verification_command"), "verification_command"),
            setup_command=_text(raw.get("setup_command", ""), "setup_command", required=False),
            reference_bundle_path=_path(
                raw.get("reference_bundle_path"), "reference_bundle_path", required=False
            ),
            timeout_seconds=timeout,
        )
        if not spec.test_patch_path.is_file():
            raise SchemaError(f"test_patch_path is not a file: {spec.test_patch_path}")
        if spec.reference_bundle_path is not None and not spec.reference_bundle_path.exists():
            raise SchemaError(f"reference_bundle_path does not exist: {spec.reference_bundle_path}")
        return spec

    @classmethod
    def load(cls, path: Path | str) -> "VerificationSpec":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def private_hashes(self) -> dict[str, str]:
        from hashlib import sha256

        values = {"test_patch_sha256": sha256(self.test_patch_path.read_bytes()).hexdigest()}
        if self.reference_bundle_path is not None:
            if self.reference_bundle_path.is_file():
                values["reference_bundle_sha256"] = sha256(
                    self.reference_bundle_path.read_bytes()
                ).hexdigest()
            else:
                rows = []
                for path in sorted(self.reference_bundle_path.rglob("*")):
                    if path.is_file():
                        rows.append(
                            path.relative_to(self.reference_bundle_path).as_posix()
                            + ":"
                            + sha256(path.read_bytes()).hexdigest()
                        )
                values["reference_bundle_sha256"] = sha256("\n".join(rows).encode()).hexdigest()
        return values


@dataclass(frozen=True)
class RunConfig:
    schema_version: str
    submission_directory: Path
    output_root: Path
    experiment_name: str
    work_root: Path
    model: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    sampling: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    seed: int = 42
    wheels_directory: Path | None = None
    pytest_version: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RunConfig":
        raw = _mapping(raw, "RunConfig")
        allowed = {
            "schema_version", "submission_directory", "output_root", "experiment_name",
            "work_root", "model", "server", "sampling", "budgets", "seed",
            "wheels_directory", "pytest_version",
        }
        _reject_unknown(raw, allowed, "RunConfig")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SchemaError(f"Unsupported RunConfig schema_version: {raw.get('schema_version')!r}")
        seed = raw.get("seed", 42)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise SchemaError("seed must be a uint32")
        experiment = _text(raw.get("experiment_name"), "experiment_name")
        if not _TASK_ID.fullmatch(experiment):
            raise SchemaError("experiment_name contains unsupported characters")
        for label in ("model", "server", "sampling", "budgets"):
            _mapping(raw.get(label, {}), label)
        budgets = dict(raw.get("budgets", {}))
        for name, value in budgets.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SchemaError(f"Invalid budget override: {name}")
        submission = _path(raw.get("submission_directory"), "submission_directory")
        if not submission.is_dir():
            raise SchemaError(f"submission_directory is not a directory: {submission}")
        output = _path(raw.get("output_root"), "output_root")
        work = _path(raw.get("work_root", str(output / "work")), "work_root")
        wheels = _path(raw.get("wheels_directory"), "wheels_directory", required=False)
        if wheels is not None and not wheels.is_dir():
            raise SchemaError(f"wheels_directory is not a directory: {wheels}")
        return cls(
            schema_version=SCHEMA_VERSION,
            submission_directory=submission,
            output_root=output,
            experiment_name=experiment,
            work_root=work,
            model=dict(raw.get("model", {})),
            server=dict(raw.get("server", {})),
            sampling=dict(raw.get("sampling", {})),
            budgets=budgets,
            seed=seed,
            wheels_directory=wheels,
            pytest_version=(str(raw["pytest_version"]) if raw.get("pytest_version") else None),
        )

    @classmethod
    def load(cls, path: Path | str) -> "RunConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class RunResult:
    experiment_id: str
    run_id: str
    task_id: str
    run_directory: Path
    completed: bool
    outcome: str
    termination_reason: str
    patch_path: Path

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))
