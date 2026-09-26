from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schemas import SCHEMA_VERSION


TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, default=str).encode())


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    relative_path: str
    media_type: str
    byte_length: int
    encoding: str | None
    purpose: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "encoding": self.encoding,
            "purpose": self.purpose,
        }


class ArtifactStore:
    def __init__(self, run_root: Path):
        self.run_root = Path(run_root)
        self.root = self.run_root / "artifacts" / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self, data: bytes, *, media_type: str, purpose: str, encoding: str | None = None
    ) -> ArtifactRef:
        digest = sha256_bytes(data)
        suffix = ".json" if media_type == "application/json" else ".txt" if encoding else ".bin"
        path = self.root / f"{digest}{suffix}"
        if not path.exists():
            atomic_write(path, data)
        return ArtifactRef(
            sha256=digest,
            relative_path=path.relative_to(self.run_root).as_posix(),
            media_type=media_type,
            byte_length=len(data),
            encoding=encoding,
            purpose=purpose,
        )

    def put_json(self, value: Any, *, purpose: str) -> ArtifactRef:
        return self.put_bytes(
            json_bytes(value), media_type="application/json", purpose=purpose, encoding="utf-8"
        )

    def put_text(self, value: str, *, purpose: str, media_type: str = "text/plain") -> ArtifactRef:
        return self.put_bytes(
            value.encode("utf-8"), media_type=media_type, purpose=purpose, encoding="utf-8"
        )

    def import_file(self, path: Path, *, purpose: str, media_type: str = "text/plain") -> ArtifactRef:
        return self.put_bytes(
            path.read_bytes(), media_type=media_type, purpose=purpose,
            encoding="utf-8" if media_type.startswith("text/") else None,
        )


class TraceRecorder:
    """Append-only trace writer with content-addressed payload storage."""

    def __init__(
        self,
        run_root: Path,
        *,
        experiment_id: str,
        run_id: str,
        task_id: str,
        trace_id: str | None = None,
    ):
        self.root = Path(run_root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.experiment_id = experiment_id
        self.run_id = run_id
        self.task_id = task_id
        self.trace_id = trace_id or secrets.token_hex(16)
        self.sequence = 0
        # ParallelAgent branches emit from worker threads.
        self._lock = threading.RLock()
        self.path = self.root / "events.jsonl"
        self.path.touch()
        self.artifacts = ArtifactStore(self.root)
        self._stack: list[str] = []
        self._root_span_id = secrets.token_hex(8)
        atomic_json(
            self.root / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "experiment_id": experiment_id,
                "run_id": run_id,
                "task_id": task_id,
                "trace_id": self.trace_id,
                "started_utc": utc_now(),
            },
        )
        self.emit(
            "run.start", span_id=self._root_span_id, parent_span_id=None,
            attributes={"state": "RUNNING"},
        )

    @property
    def current_span_id(self) -> str:
        return self._stack[-1] if self._stack else self._root_span_id

    def emit(
        self,
        event_type: str,
        *,
        status: str = "ok",
        span_id: str | None = None,
        parent_span_id: str | None = None,
        agent_id: str = "solver",
        duration_ms: float | None = None,
        attributes: dict[str, Any] | None = None,
        artifact_refs: list[ArtifactRef | dict[str, Any]] | None = None,
        budget_before: dict[str, Any] | None = None,
        budget_after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._emit_locked(
                event_type, status=status, span_id=span_id, parent_span_id=parent_span_id,
                agent_id=agent_id, duration_ms=duration_ms, attributes=attributes,
                artifact_refs=artifact_refs, budget_before=budget_before, budget_after=budget_after,
            )

    def _emit_locked(self, event_type, *, status, span_id, parent_span_id, agent_id, duration_ms,
                     attributes, artifact_refs, budget_before, budget_after) -> dict[str, Any]:
        self.sequence += 1
        resolved_span = span_id or self.current_span_id
        if parent_span_id is None and resolved_span != self._root_span_id:
            parent_span_id = self._stack[-2] if len(self._stack) > 1 else self._root_span_id
        refs = [ref.as_dict() if isinstance(ref, ArtifactRef) else ref for ref in artifact_refs or []]
        row = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "span_id": resolved_span,
            "parent_span_id": parent_span_id,
            "agent_id": agent_id,
            "sequence": self.sequence,
            "event_type": event_type,
            "status": status,
            "timestamp_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "duration_ms": None if duration_ms is None else round(duration_ms, 3),
            "attributes": attributes or {},
            "artifact_refs": refs,
            "budget_before": budget_before,
            "budget_after": budget_after,
        }
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return row

    @contextlib.contextmanager
    def span(
        self, name: str, *, attributes: dict[str, Any] | None = None, agent_id: str = "solver"
    ) -> Iterator[str]:
        span_id = secrets.token_hex(8)
        parent = self.current_span_id
        started = time.monotonic_ns()
        self._stack.append(span_id)
        self.emit(
            f"{name}.start", span_id=span_id, parent_span_id=parent,
            attributes=attributes, agent_id=agent_id,
        )
        try:
            yield span_id
        except Exception as exc:
            self.emit(
                f"{name}.end", span_id=span_id, parent_span_id=parent, status="error",
                duration_ms=(time.monotonic_ns() - started) / 1_000_000,
                attributes={"error_type": type(exc).__name__, "error": str(exc)},
                agent_id=agent_id,
            )
            raise
        else:
            self.emit(
                f"{name}.end", span_id=span_id, parent_span_id=parent,
                duration_ms=(time.monotonic_ns() - started) / 1_000_000,
                agent_id=agent_id,
            )
        finally:
            self._stack.pop()

    def finish(self, *, status: str, termination_reason: str, outcome: str) -> None:
        self.emit(
            "run.end",
            span_id=self._root_span_id,
            parent_span_id=None,
            status="ok" if status == "COMPLETE" else "error",
            attributes={
                "state": status,
                "termination_reason": termination_reason,
                "outcome": outcome,
            },
        )
        atomic_json(
            self.root / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "status": status,
                "experiment_id": self.experiment_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "trace_id": self.trace_id,
                "finished_utc": utc_now(),
                "termination_reason": termination_reason,
                "outcome": outcome,
            },
        )


def file_manifest(root: Path, *, exclude: tuple[str, ...] = ("teacher/",)) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "run_manifest.json" or any(relative.startswith(prefix) for prefix in exclude):
            continue
        data = path.read_bytes()
        rows.append({"path": relative, "size_bytes": len(data), "sha256": sha256_bytes(data)})
    return rows
