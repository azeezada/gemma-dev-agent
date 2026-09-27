from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

from gemma_agent.env.harness import (
    SKILL_TOOL_SCHEMAS,
    TOOL_SCHEMAS,
    Budget,
    ModelHTTPError,
    OpenAIHTTPClient,
    VLLMServer,
    WorkspaceTools,
    apply_patch,
    build_task_prompt,
    configure_workspace,
    create_task_venv,
    parse_patch_targets,
    inject_session_state,
    template_messages,
    install_workspace_paths,
    is_context_length_error,
    openai_tools,
    run_process,
    safe_extract,
    skill_catalog_instruction,
    target_test_names,
    task_pytest_plugins,
    template_token_ids,
)
from gemma_agent.env.v3 import (
    agent_tool_schemas,
    budget_from_submission,
    classify_outcome,
    load_submission,
    submission_request_settings,
)

from .reporting import (
    audit_private_leaks,
    export_atif,
    materialize_trace,
    render_reports,
    seal_manifest,
    validate_trace,
)
from .schemas import RunConfig, RunResult, TaskSpec, VerificationSpec
from .tracing import ArtifactRef, TraceRecorder, atomic_json, atomic_write, json_bytes, sha256_bytes, utc_now


class _QuietToolLogger:
    """WorkspaceTools compatibility shim; canonical logging happens at dispatch boundaries."""

    def __init__(self, root: Path):
        self.root = root
        self.rows: list[dict[str, Any]] = []

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        row = {"event": event, **json.loads(json.dumps(payload, default=str))}
        self.rows.append(row)
        return row


def _submission_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".pyc" and not any(
            part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts
        ) and path.relative_to(root).parts[0] != "adapters"  # binary weights; checked by eval.package
    }


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.") or "run"


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        rows.append(relative.as_posix() + ":" + hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _workspace_diff(workspace: Path) -> str:
    run_process(["git", "add", "-N", "."], cwd=workspace, timeout=30, output_limit=None)
    result = run_process(["git", "diff", "HEAD"], cwd=workspace, timeout=30, output_limit=None)
    return result.get("stdout", "")


def _materialize_snapshot(snapshot: Path, destination: Path) -> None:
    if snapshot.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(snapshot, destination, symlinks=True)
        if not (destination / ".git").exists():
            raise FileNotFoundError(f"Snapshot directory is not a Git repository: {snapshot}")
    else:
        safe_extract(snapshot, destination)


def _effective_configuration(run_config: RunConfig, submission: dict[str, Any]) -> dict[str, Any]:
    effective = json.loads(json.dumps(submission))
    effective["evaluation"].update(run_config.budgets)
    if effective.get("tree"):
        # Workflow agents keep their declared sampling; parallel branches rely on distinct seeds.
        return effective
    effective["sampling"].update(run_config.sampling)
    effective["sampling"]["seed"] = run_config.seed
    for child in effective.get("agent_tools", {}).values():
        child["sampling"].update(run_config.sampling)
        child["sampling"]["seed"] = run_config.seed
    # Reuse the strict v3 range checks by rebuilding the four source files is unnecessary;
    # validate explicit overrides here and let the loaded submission remain the source format.
    limits = effective["evaluation"]
    for key in ("max_tool_calls", "max_turns", "timeout_seconds"):
        value = limits[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Invalid effective budget {key}: {value!r}")
    if not isinstance(limits["max_time_minutes"], (int, float)) or limits["max_time_minutes"] <= 0:
        raise ValueError("Invalid effective max_time_minutes")
    return effective


def _budget(config: dict[str, Any]) -> Budget:
    return budget_from_submission(config)


def _render_prompt(
    client: Any, messages: list[dict[str, Any]], settings: dict[str, Any] | None = None,
) -> tuple[str | None, list[int] | None]:
    tokenizer = getattr(client, "tokenizer", None)
    tools = getattr(client, "tool_definitions", None)
    if tokenizer is None:
        return None, None
    kwargs = {"enable_thinking": True, **((settings or {}).get("chat_template_kwargs") or {})}
    try:
        rendered = tokenizer.apply_chat_template(
            template_messages(messages), tools=tools, tokenize=False, add_generation_prompt=True, **kwargs,
        )
        tokens = tokenizer.apply_chat_template(
            template_messages(messages), tools=tools, tokenize=True, add_generation_prompt=True, **kwargs,
        )
        return str(rendered), template_token_ids(tokens)
    except Exception:
        return None, None


def _record_tokens(
    trace: TraceRecorder, *, agent_name: str, turn: int, refs: list[ArtifactRef],
    tools_ref: ArtifactRef, response_ref: ArtifactRef, response: dict[str, Any],
) -> None:
    """Plan 6.2.2: one row per model call with the exact token ids vLLM saw and produced.

    Training examples are cut from these ids (`gemma_agent.data.tokens`), so they match serving by
    construction; the stored messages/tools let the parity check re-render every prompt.
    """
    choice = response["choices"][0]
    row = {
        "agent": agent_name, "turn": turn,
        "messages": refs[0].relative_path, "settings": refs[1].relative_path,
        "tools": tools_ref.relative_path, "response": response_ref.relative_path,
        "finish_reason": choice.get("finish_reason"), "usage": response.get("usage") or {},
        "prompt_token_ids": response.get("prompt_token_ids"),
        "completion_token_ids": choice.get("token_ids"),
    }
    path = trace.root / "tokens" / "turns.jsonl"
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


@contextlib.contextmanager
def _served_model(client: Any, config: dict[str, Any]):
    """An agent with `adapter:` is served by vLLM under the adapter name (HARNESS_README 3.4)."""
    adapter = config.get("adapter")
    prior = getattr(client, "model", None)
    if adapter:
        client.model = adapter
    try:
        yield
    finally:
        if adapter:
            client.model = prior


def _model_visible_summary(value: Any, limit: int = 800) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:500] + f"...[{len(text)-800} chars]..." + text[-300:]


class _TracedToolExecutor:
    def __init__(self, tools: WorkspaceTools, trace: TraceRecorder):
        self.tools = tools
        self.trace = trace
        self.agent_tools: dict[str, Any] = {}

    def dispatch(self, name: str, arguments: dict[str, Any], correlation_id: str,
                 agent_name: str = "") -> dict[str, Any]:
        budget_before = self.tools.budget.snapshot()
        before_diff = _workspace_diff(self.tools.workspace)
        before_hash = sha256_bytes(before_diff.encode())
        args_ref = self.trace.artifacts.put_json(arguments, purpose=f"tool arguments: {name}")
        self.trace.emit(
            "tool_call",
            attributes={
                "correlation_id": correlation_id,
                "agent": agent_name,
                "tool": name,
                "arguments_sha256": args_ref.sha256,
                "working_directory": str(self.tools.workspace),
            },
            artifact_refs=[args_ref],
            budget_before=budget_before,
        )
        started = time.monotonic_ns()
        # AgentTools are not budget-gated themselves; the sub-agent's own tool calls are.
        allowed = self.tools.allowed_tools
        if name in self.agent_tools and (allowed is None or name in allowed):
            result = self.agent_tools[name](name, arguments)
        else:
            result = self.tools.dispatch(name, arguments)
        duration = (time.monotonic_ns() - started) / 1_000_000
        after_diff = _workspace_diff(self.tools.workspace)
        refs: list[ArtifactRef] = [
            self.trace.artifacts.put_json(result, purpose=f"model-visible tool result: {name}")
        ]
        diff_path = None
        if after_diff != before_diff:
            safe_correlation = re.sub(r"[^A-Za-z0-9_.-]+", "-", correlation_id)[:80]
            diff_path = self.trace.root / "workspace" / "diffs" / (
                f"{self.trace.sequence + 1:04d}-{safe_correlation}.patch"
            )
            atomic_write(diff_path, after_diff.encode())
            refs.append(self.trace.artifacts.put_text(after_diff, purpose=f"workspace diff after {name}"))
        if name == "run_command":
            command_log = self.trace.root / "commands" / f"{self.tools.budget.tool_calls_used:04d}.log"
            if command_log.is_file():
                refs.append(self.trace.artifacts.import_file(command_log, purpose="complete command output"))
        error_type = result.get("error_type") if isinstance(result, dict) else None
        self.trace.emit(
            "tool_result",
            status="error" if isinstance(result, dict) and result.get("status") == "error" else "ok",
            duration_ms=duration,
            attributes={
                "correlation_id": correlation_id,
                "agent": agent_name,
                "tool": name,
                "model_visible_summary": _model_visible_summary(result),
                "error_type": error_type,
                "workspace_hash_before": before_hash,
                "workspace_hash_after": sha256_bytes(after_diff.encode()),
                "workspace_changed": after_diff != before_diff,
                "workspace_diff_path": (
                    diff_path.relative_to(self.trace.root).as_posix() if diff_path else None
                ),
                "result_sha256": refs[0].sha256,
            },
            artifact_refs=refs,
            budget_before=budget_before,
            budget_after=self.tools.budget.snapshot(),
        )
        return result


def _tool_definitions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Only the tools this agent declares, as ADK advertises them to the model."""
    pool = TOOL_SCHEMAS + SKILL_TOOL_SCHEMAS + agent_tool_schemas(config)
    return [tool for tool in openai_tools(pool) if tool["function"]["name"] in config["tools"]]


@contextlib.contextmanager
def _advertised_tools(client: Any, definitions: list[dict[str, Any]]):
    prior = getattr(client, "tool_definitions", None)
    client.tool_definitions = definitions
    try:
        yield
    finally:
        client.tool_definitions = prior


def _session_state(task: TaskSpec) -> dict[str, str]:
    """ADK session state the official runner creates (HARNESS_README section 5.1)."""
    state = {"problem_description": task.problem_statement}
    if (task.hints or "").strip():
        state["hints"] = task.hints.strip()
    return state


def _llm_loop(
    *, client: Any, messages: list[dict[str, Any]], settings: dict[str, Any], tools: WorkspaceTools,
    trace: TraceRecorder, executor: "_TracedToolExecutor", max_model_len: int, agent_name: str,
    root: bool, stats: dict[str, int],
) -> dict[str, Any]:
    """One LlmAgent's model/tool loop.

    The root agent follows the official outer loop (continuation nudges, stop on
    submit_patch). An AgentTool sub-agent ends at its first text-only response, which
    ADK returns to the caller as the tool result.
    """
    termination = "unknown"
    final_text = ""
    nudges = 0
    while not tools.patch_submitted:
        if tools.budget.remaining_seconds <= 0:
            termination = "time_budget_exhausted"
            break
        if tools.budget.turns_used >= tools.budget.max_turns:
            termination = "turn_budget_exhausted"
            break
        if tools.budget.remaining_tool_calls == 0 and nudges == 0 and root:
            messages.append({
                "role": "user",
                "content": "The counted tool budget is exhausted. Call submit_patch now; it is free.",
            })
            nudges += 1
            trace.emit("harness_intervention", attributes={"kind": "tool_budget_nudge"})
        prompt_size = client.count_tokens(messages)
        utilization = (prompt_size + settings["max_tokens"]) / max_model_len
        trace.emit(
            "context_measurement",
            attributes={
                "agent": agent_name,
                "prompt_tokens": prompt_size,
                "completion_headroom": settings["max_tokens"],
                "context_window": max_model_len,
                "utilization": round(utilization, 6),
            },
            budget_after=tools.budget.snapshot(),
        )
        if utilization > 1:
            # The official harness never compacts (ADK's token threshold equals max_model_len), so a run
            # that outgrows the window ends here and the caller submits the git diff.
            termination = "context_capacity_exceeded"
            break
        tools.budget.turns_used += 1
        response = None
        for attempt in range(3):
            remaining = tools.budget.remaining_seconds
            if remaining <= 0:
                raise TimeoutError("Task deadline reached before model request")
            correlation = f"model-{tools.budget.turns_used}-{attempt}-{secrets.token_hex(4)}"
            rendered, token_ids = _render_prompt(client, messages, settings)
            refs = [
                trace.artifacts.put_json(messages, purpose="complete outbound model messages"),
                trace.artifacts.put_json(settings, purpose="effective model request settings"),
            ]
            if rendered is not None:
                refs.append(trace.artifacts.put_text(rendered, purpose="rendered model prompt"))
            if token_ids is not None:
                refs.append(trace.artifacts.put_json(token_ids, purpose="rendered model token ids"))
            trace.emit(
                "model_request",
                attributes={
                    "correlation_id": correlation,
                    "agent": agent_name,
                    "turn": tools.budget.turns_used,
                    "attempt": attempt,
                    "prompt_tokens_measured": prompt_size,
                    "request_timeout_seconds": round(remaining, 3),
                    "messages_sha256": refs[0].sha256,
                    "settings_sha256": refs[1].sha256,
                },
                artifact_refs=refs,
                budget_before=tools.budget.snapshot(),
            )
            started = time.monotonic_ns()
            try:
                response = client.chat(messages, **settings, request_timeout=remaining)
            except Exception as exc:
                transient = (
                    isinstance(exc, ModelHTTPError) and exc.status in {429, 500, 502, 503, 504}
                ) or isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
                error_ref = trace.artifacts.put_text(
                    f"{type(exc).__name__}: {exc}", purpose="model request error"
                )
                trace.emit(
                    "model_response", status="error",
                    duration_ms=(time.monotonic_ns() - started) / 1_000_000,
                    attributes={
                        "correlation_id": correlation,
                        "agent": agent_name,
                        "error_type": type(exc).__name__,
                        "transient": transient,
                        "attempt": attempt,
                    },
                    artifact_refs=[error_ref], budget_after=tools.budget.snapshot(),
                )
                if not transient or attempt == 2 or tools.budget.remaining_seconds <= 0:
                    raise
                time.sleep(min(0.5 * 2**attempt, tools.budget.remaining_seconds))
                continue
            response_ref = trace.artifacts.put_json(response, purpose="raw model response")
            _record_tokens(
                trace, agent_name=agent_name, turn=tools.budget.turns_used, refs=refs,
                tools_ref=trace.artifacts.put_json(
                    getattr(client, "tool_definitions", None), purpose="advertised tool definitions"),
                response_ref=response_ref, response=response,
            )
            choice = response["choices"][0]
            message = choice["message"]
            usage = response.get("usage", {})
            trace.emit(
                "model_response",
                duration_ms=(time.monotonic_ns() - started) / 1_000_000,
                attributes={
                    "correlation_id": correlation,
                    "agent": agent_name,
                    "turn": tools.budget.turns_used,
                    "finish_reason": choice.get("finish_reason"),
                    "usage": usage,
                    "tool_call_count": len(message.get("tool_calls") or []),
                    "has_exposed_reasoning": bool(
                        message.get("reasoning") or message.get("reasoning_content")
                    ),
                    "response_sha256": response_ref.sha256,
                },
                artifact_refs=[response_ref], budget_after=tools.budget.snapshot(),
            )
            break
        if response is None:
            raise RuntimeError("Model request completed without a response")
        stats["calls"] += 1
        choice = response["choices"][0]
        message = choice["message"]
        usage = response.get("usage", {})
        stats["prompt_total"] += int(usage.get("prompt_tokens") or prompt_size)
        stats["completion_total"] += int(usage.get("completion_tokens") or 0)
        stats["prompt_max"] = max(stats["prompt_max"], int(usage.get("prompt_tokens") or prompt_size))
        history = (
            client.assistant_history(message)
            if hasattr(client, "assistant_history")
            else {**message, "role": "assistant"}
        )
        messages.append(history)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not root:
                final_text = str(message.get("content") or "")
                termination = "final_response"
                break
            nudges += 1
            if nudges > 3:
                termination = "max_nudges"
                break
            messages.append({
                "role": "user",
                "content": (
                    "Continue with tools or call submit_patch when the fix is ready. "
                    "A prose answer does not submit the workspace diff."
                ),
            })
            trace.emit("harness_intervention", attributes={"kind": "prose_nudge", "count": nudges})
            continue
        nudges = 0
        for index, call in enumerate(tool_calls):
            function = call.get("function", {})
            name = str(function.get("name", ""))
            raw = function.get("arguments", "{}")
            tool_correlation = str(call.get("id") or f"tool-{tools.budget.turns_used}-{index}")
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
            except (TypeError, ValueError) as exc:
                raw_ref = trace.artifacts.put_json(raw, purpose=f"malformed tool arguments: {name}")
                trace.emit(
                    "tool_call", status="error",
                    attributes={
                        "correlation_id": tool_correlation,
                        "agent": agent_name,
                        "tool": name,
                        "arguments_sha256": raw_ref.sha256,
                    },
                    artifact_refs=[raw_ref], budget_before=tools.budget.snapshot(),
                )
                result = {
                    "status": "error",
                    "error_type": "ToolArgumentsJSONError",
                    "error_message": str(exc),
                }
                result_ref = trace.artifacts.put_json(result, purpose="malformed tool result")
                trace.emit(
                    "tool_result", status="error",
                    attributes={
                        "correlation_id": tool_correlation,
                        "agent": agent_name,
                        "tool": name,
                        "error_type": "ToolArgumentsJSONError",
                        "result_sha256": result_ref.sha256,
                    },
                    artifact_refs=[result_ref],
                    budget_before=tools.budget.snapshot(), budget_after=tools.budget.snapshot(),
                )
                for history_call in history.get("tool_calls", []):
                    if history_call.get("id") == call.get("id"):
                        history_call["function"]["arguments"] = "{}"
            else:
                result = executor.dispatch(name, arguments, tool_correlation, agent_name=agent_name)
            messages.append({
                "role": "tool", "tool_call_id": tool_correlation,
                "name": name, "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
            })
            if tools.patch_submitted:
                termination = "submitted_patch"
                break
        if tools.budget.remaining_tool_calls == 0 and not tools.patch_submitted:
            exhausted = [
                row for row in trace.path.read_text(encoding="utf-8").splitlines()
                if "ToolBudgetExceeded" in row
            ]
            if len(exhausted) >= 2 or not root:
                termination = "tool_budget_exhausted"
                break
    if tools.patch_submitted:
        termination = "submitted_patch"
    return {"termination_reason": termination, "final_text": final_text}


def _run_agent(
    *, client: Any, task: TaskSpec, config: dict[str, Any], tools: WorkspaceTools,
    trace: TraceRecorder, max_model_len: int,
) -> dict[str, Any]:
    public_task = {
        "instance_id": task.task_id,
        "repo": task.repository,
        "problem_statement": task.problem_statement,
        "hints_text": task.hints,
    }
    state = _session_state(task)
    messages = [
        {"role": "system", "content": inject_session_state(config["instruction"], state)
         + skill_catalog_instruction(config.get("skills") or {})},
        {"role": "user", "content": build_task_prompt(public_task, tools.workspace, tools.budget)},
    ]
    stats = {"calls": 0, "prompt_total": 0, "completion_total": 0, "prompt_max": 0, "compactions": 0}
    executor = _TracedToolExecutor(tools, trace)

    def run_agent_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """ADK AgentTool: a fresh session holding only `request` and copied session state."""
        child = config["agent_tools"][name]
        request = arguments.get("request")
        if set(arguments) != {"request"} or not isinstance(request, str) or not request.strip():
            return {"status": "error", "error_type": "ValidationError",
                    "error_message": f"{name} takes one non-empty string argument: request"}
        child_messages = [
            {"role": "system", "content": inject_session_state(child["instruction"], state)
             + skill_catalog_instruction(child["skills"])},
            {"role": "user", "content": request},
        ]
        trace.emit("agent_transfer", attributes={"agent": name, "direction": "enter"},
                   budget_after=tools.budget.snapshot())
        with (tools.scoped(child["tools"], child["skills"]),
              _advertised_tools(client, _tool_definitions(child)), _served_model(client, child)):
            outcome = _llm_loop(
                client=client, messages=child_messages, settings=submission_request_settings(child),
                tools=tools, trace=trace, executor=executor, max_model_len=max_model_len,
                agent_name=name, root=False, stats=stats,
            )
        messages_ref = trace.artifacts.put_json(child_messages, purpose=f"agent tool {name} message history")
        trace.emit("agent_transfer", attributes={
            "agent": name, "direction": "exit", "termination_reason": outcome["termination_reason"],
            "result_chars": len(outcome["final_text"]),
        }, artifact_refs=[messages_ref], budget_after=tools.budget.snapshot())
        # ADK wraps a non-dict function result as {"result": value}.
        return {"result": outcome["final_text"]}

    executor.agent_tools = {name: run_agent_tool for name in config.get("agent_tools", {})}
    termination = "unknown"
    error: str | None = None
    try:
        if config.get("tree"):
            from .workflow import WorkflowRunner
            termination = WorkflowRunner(
                client=client, task=task, config=config, tools=tools, trace=trace, executor=executor,
                max_model_len=max_model_len, stats=stats, llm_loop=_llm_loop,
                tool_definitions=_tool_definitions, advertised_tools=_advertised_tools, state=state,
            ).run()
        else:
            with _advertised_tools(client, _tool_definitions(config)), _served_model(client, config):
                termination = _llm_loop(
                    client=client, messages=messages, settings=submission_request_settings(config),
                    tools=tools, trace=trace, executor=executor, max_model_len=max_model_len,
                    agent_name=config["name"], root=True, stats=stats,
                )["termination_reason"]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        termination = (
            "time_budget_exhausted" if tools.budget.remaining_seconds <= 0
            else "context_capacity_exceeded" if is_context_length_error(exc)
            else "model_error"
        )
        error_ref = trace.artifacts.put_text(error, purpose="agent loop error")
        trace.emit(
            "agent_error", status="error",
            attributes={"termination_reason": termination, "error_type": type(exc).__name__},
            artifact_refs=[error_ref], budget_after=tools.budget.snapshot(),
        )
    finally:
        if not tools.patch_submitted:
            try:
                tools.submit_patch()
                trace.emit("automatic_patch_recovery", attributes={"patch_size": len(tools.submitted_patch)})
            except Exception as exc:
                error = (error + "; " if error else "") + f"patch recovery failed: {exc}"
                trace.emit(
                    "patch_recovery_error", status="error",
                    attributes={"error_type": type(exc).__name__, "error": str(exc)},
                )
    messages_ref = trace.artifacts.put_json(messages, purpose="final solver message history")
    patch_ref = trace.artifacts.put_text(tools.submitted_patch, purpose="frozen agent patch")
    trace.emit(
        "patch_capture",
        attributes={
            "patch_size": len(tools.submitted_patch.encode()),
            "patch_sha256": patch_ref.sha256,
            "explicit": termination == "submitted_patch",
            "messages_sha256": messages_ref.sha256,
        },
        artifact_refs=[patch_ref, messages_ref], budget_after=tools.budget.snapshot(),
    )
    return {
        "termination_reason": termination,
        "error": error,
        "patch": tools.submitted_patch,
        "patch_submitted_explicitly": termination == "submitted_patch",
        "model_calls": stats["calls"],
        "prompt_tokens_total": stats["prompt_total"],
        "prompt_tokens_max": stats["prompt_max"],
        "completion_tokens_total": stats["completion_total"],
        "context_compactions": stats["compactions"],
        "budget": tools.budget.snapshot(),
        "rejected_commands": tools.rejected_commands,
    }


def _verification_environment(workspace: Path, environment_root: Path) -> tuple[Path, dict[str, str]]:
    result = run_process(
        [sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(environment_root)],
        timeout=180, output_limit=None,
    )
    python = environment_root / "bin" / "python"
    if result["returncode"] != 0 or not python.is_file():
        raise RuntimeError(f"Could not create verification environment: {result.get('stderr', '')}")
    install_workspace_paths(python, workspace)
    env = os.environ.copy()
    env.update({
        "PATH": str(python.parent) + os.pathsep + env.get("PATH", ""),
        "PYTHONPATH": str(workspace),
        "PYTHONSAFEPATH": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": task_pytest_plugins(python, workspace),
        "TEST_TMPDIR": "/tmp",
    })
    return python, env


def _target_test_status(test_patch: str, output: str) -> dict[str, Any]:
    """Separate the task's own tests from unrelated file-level environment errors."""
    names = target_test_names(test_patch)
    collection_error = "ERROR collecting" in output or "errors during collection" in output
    reported = re.findall(r"^(?:FAILED|ERROR) \S*?::(?:\w+::)*(\w+)", output, flags=re.M)
    failed = sorted({name for name in names if collection_error or name in reported})
    return {
        "names": names,
        "failed": failed,
        "all_passed": bool(names) and not failed and bool(re.search(r"\b\d+ passed\b", output)),
    }


def _verify_phase(
    *, phase: str, task: TaskSpec, verification: VerificationSpec, agent_patch: str,
    work_root: Path, trace: TraceRecorder, run_config: RunConfig,
) -> dict[str, Any]:
    phase_root = work_root / phase
    workspace = phase_root / "workspace"
    _materialize_snapshot(task.snapshot_path, workspace)
    configure_workspace(workspace)
    if run_config.wheels_directory is not None:
        _python = create_task_venv(
            phase_root / "venv", workspace, run_config.wheels_directory,
            pytest_version=run_config.pytest_version,
        )
        env = os.environ.copy()
        env.update({
            "PATH": str(_python.parent) + os.pathsep + env.get("PATH", ""),
            "PYTHONPATH": str(workspace), "PYTHONSAFEPATH": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "TEST_TMPDIR": "/tmp",
            "PYTEST_PLUGINS": task_pytest_plugins(_python, workspace),
        })
        for setup_record in (phase_root / "task_dependency_setup.json", phase_root / "editable_install.json"):
            if setup_record.is_file():
                trace.emit(
                    "verification_environment",
                    attributes={"phase": phase, "record": setup_record.name},
                    artifact_refs=[trace.artifacts.import_file(
                        setup_record, purpose=f"{phase} dependency environment record",
                        media_type="application/json",
                    )],
                )
    else:
        _python, env = _verification_environment(workspace, phase_root / "venv")
    patch_file = phase_root / "agent.patch"
    agent_apply = apply_patch(workspace, agent_patch, patch_file)
    if not agent_apply["ok"]:
        return {"resolved": False, "infrastructure_error": True, "error": "agent_patch_apply_failed"}
    test_patch = verification.test_patch_path.read_text(encoding="utf-8")
    for target in parse_patch_targets(test_patch):
        run_process(["git", "checkout", "HEAD", "--", target], cwd=workspace, timeout=30)
        run_process(["git", "clean", "-f", "--", target], cwd=workspace, timeout=30)
    test_apply = apply_patch(workspace, test_patch, phase_root / "private-tests.patch")
    if not test_apply["ok"]:
        return {"resolved": False, "infrastructure_error": True, "error": "test_patch_apply_failed"}
    refs: list[ArtifactRef] = []
    if verification.setup_command:
        setup_log = phase_root / "setup.log"
        setup = run_process(
            ["/bin/bash", "-c", verification.setup_command], cwd=workspace, env=env,
            timeout=verification.timeout_seconds, output_limit=None, log_path=setup_log,
        )
        refs.append(trace.artifacts.import_file(setup_log, purpose=f"{phase} complete setup output"))
        if setup["returncode"] != 0:
            result = {
                "resolved": False, "infrastructure_error": True,
                "error": "verification_setup_failed", "setup_exit_code": setup["returncode"],
            }
            trace.emit(
                "verification_result", status="error",
                attributes={"phase": phase, **result}, artifact_refs=refs,
            )
            return result
    output_log = phase_root / "verification.log"
    completed = run_process(
        ["/bin/bash", "-c", verification.verification_command], cwd=workspace, env=env,
        timeout=verification.timeout_seconds, output_limit=None, log_path=output_log,
    )
    refs.append(trace.artifacts.import_file(output_log, purpose=f"{phase} complete verification output"))
    output = completed.get("stdout", "") + completed.get("stderr", "")
    infrastructure_error = bool(
        completed.get("timed_out")
        or re.search(
            r"ERROR collecting|ModuleNotFoundError|No module named|ImportError while importing|command not found|No such file or directory|fixture .+ not found|recursive dependency involving fixture",
            output,
        )
    )
    result = {
        "resolved": completed["returncode"] == 0,
        "test_failure": completed["returncode"] != 0 and not infrastructure_error,
        "infrastructure_error": completed["returncode"] != 0 and infrastructure_error,
        "exit_code": completed["returncode"],
        "elapsed_seconds": completed["elapsed_seconds"],
        "timed_out": completed.get("timed_out", False),
        "workspace_tree_sha256": _tree_hash(workspace),
        "log_sha256": refs[-1].sha256,
        # Diagnostic only; the official criterion stays pytest exit 0 on whole files.
        "target_tests": _target_test_status(test_patch, output),
    }
    trace.emit(
        "verification_result", status="error" if result["infrastructure_error"] else "ok",
        attributes={"phase": phase, **result}, artifact_refs=refs,
    )
    return result


def _environment() -> dict[str, Any]:
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[name] = distribution.version
    return {
        "created_utc": utc_now(),
        "python": sys.version,
        "platform": sys.platform,
        "executable": sys.executable,
        "packages": sorted(packages.items()),
        "fidelity": {
            "runtime": "Development emulation of the documented competition interface.",
            "trace": "Canonical local schema with a documented ATIF-compatible export.",
            "command_guard": "Policy enforcement, not an operating-system sandbox.",
        },
    }


def run_task(
    task: TaskSpec,
    verification: VerificationSpec,
    run_config: RunConfig,
    *,
    client: Any | None = None,
) -> RunResult:
    """Run, verify, trace, and seal one solver attempt."""
    if verification.task_id != task.task_id:
        raise ValueError("TaskSpec and VerificationSpec task_id values differ")
    files = _submission_files(run_config.submission_directory)
    submission = load_submission(files)
    effective = _effective_configuration(run_config, submission)
    config_hash = sha256_bytes(json_bytes({"run": run_config.as_dict(), "submission": effective}))
    experiment_id = f"{_slug(run_config.experiment_name)}-{config_hash[:8]}"
    run_id = f"{_slug(task.task_id)}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
    run_root = run_config.output_root / experiment_id / run_id
    trace = TraceRecorder(
        run_root, experiment_id=experiment_id, run_id=run_id, task_id=task.task_id
    )
    (run_root / "task").mkdir()
    (run_root / "final").mkdir()
    (run_root / "workspace" / "diffs").mkdir(parents=True)
    (run_root / "teacher").mkdir()
    public_task = task.public_dict()
    atomic_json(run_root / "task" / "public.json", public_task)
    private_hashes = verification.private_hashes()
    atomic_json(run_root / "task" / "private_manifest.json", private_hashes)
    atomic_json(run_root / "effective_config.json", {
        "run": run_config.as_dict(), "submission": effective,
        "submission_files_sha256": {
            name: hashlib.sha256(value.encode()).hexdigest() for name, value in sorted(files.items())
        },
        "configuration_sha256": config_hash,
    })
    atomic_json(run_root / "environment.json", _environment())
    server: VLLMServer | None = None
    agent: dict[str, Any] = {
        "termination_reason": "setup_error", "error": None, "patch": "", "budget": {}
    }
    baseline: dict[str, Any] = {}
    patched: dict[str, Any] = {}
    outcome = "INFRA_ERROR"
    completed = False
    work_root = run_config.work_root / run_id
    solver_workspace = work_root / "solver" / "workspace"
    solver_python = Path(sys.executable)
    try:
        with trace.span("setup"):
            _materialize_snapshot(task.snapshot_path, solver_workspace)
            configure_workspace(solver_workspace)
            if run_config.wheels_directory is not None:
                solver_python = create_task_venv(
                    work_root / "solver" / "venv", solver_workspace,
                    run_config.wheels_directory, pytest_version=run_config.pytest_version,
                )
                for setup_record in (
                    work_root / "solver" / "task_dependency_setup.json",
                    work_root / "solver" / "editable_install.json",
                ):
                    if setup_record.is_file():
                        trace.emit(
                            "solver_environment",
                            attributes={"record": setup_record.name},
                            artifact_refs=[trace.artifacts.import_file(
                                setup_record, purpose="solver dependency environment record",
                                media_type="application/json",
                            )],
                        )
            baseline_hash = _tree_hash(solver_workspace)
            trace.emit("workspace_snapshot", attributes={
                "phase": "solver_baseline", "tree_sha256": baseline_hash,
                "snapshot_sha256": (
                    hashlib.sha256(task.snapshot_path.read_bytes()).hexdigest()
                    if task.snapshot_path.is_file() else baseline_hash
                ),
            })
            if client is None:
                base_url = run_config.model.get("base_url")
                model_name = str(run_config.model.get("name") or effective["model"])
                if base_url:
                    client = OpenAIHTTPClient(str(base_url), model_name)
                else:
                    model_path = Path(str(run_config.model.get("path", ""))).expanduser()
                    if not model_path.is_dir():
                        raise ValueError("RunConfig model.path or model.base_url is required")
                    server = VLLMServer(
                        model_path=model_path,
                        model_name=model_name,
                        log_path=run_root / "server.log",
                        port=int(run_config.server.get("port", 8000)),
                        startup_timeout=int(run_config.server.get("startup_timeout", 1800)),
                        max_model_len=int(run_config.server.get("max_model_len", 32768)),
                    )
                    with trace.span("model_server"):
                        profile = server.start(
                            status_callback=lambda status: trace.emit(
                                "model_server_status", attributes=status
                            )
                        )
                        atomic_json(run_root / "server_profile.json", profile)
                    client = server.client
                    tokenizer = client.configure_tokenizer(
                        model_path, effective["tools"], agent_tool_schemas(effective)
                    )
                    trace.emit("model_preflight", attributes=tokenizer)
        budget = _budget(effective)
        quiet_logger = _QuietToolLogger(run_root)
        tools = WorkspaceTools(
            solver_workspace, budget, quiet_logger, python=solver_python,
            graph_path=task.graph_path, embeddings_path=task.embeddings_path,
            allowed_tools=effective["tools"], skills=effective.get("skills") or {},
        )
        with trace.span("agent"):
            agent = _run_agent(
                client=client, task=task, config=effective, tools=tools, trace=trace,
                max_model_len=int(run_config.server.get("max_model_len", 32768)),
            )
        patch_path = run_root / "final" / "agent.patch"
        atomic_write(patch_path, agent["patch"].encode())
        with trace.span("verification"):
            patched = _verify_phase(
                phase="patched", task=task, verification=verification,
                agent_patch=agent["patch"], work_root=work_root, trace=trace,
                run_config=run_config,
            ) if agent["patch"].strip() else {
                "resolved": False, "error": "empty_agent_patch", "infrastructure_error": False
            }
            baseline = _verify_phase(
                phase="baseline", task=task, verification=verification,
                agent_patch="", work_root=work_root, trace=trace,
                run_config=run_config,
            )
        outcome = classify_outcome(agent, patched, baseline)
        atomic_json(run_root / "final" / "baseline.json", baseline)
        atomic_json(run_root / "final" / "verification.json", patched)
        atomic_json(run_root / "final" / "outcome.json", {
            "classification": outcome,
            "resolved": outcome == "PASS",
            "run_completed": True,
            "definition": {
                "PASS": "A nonempty patch passes verification against a baseline test failure.",
                "FAIL": "The completed attempt does not resolve the task.",
                "INCONCLUSIVE": "Patched verification passes without an established baseline failure.",
                "INFRA_ERROR": "Infrastructure prevents a trustworthy result.",
            },
        })
        completed = True
    except Exception as exc:
        agent["error"] = f"{type(exc).__name__}: {exc}"
        agent["termination_reason"] = agent.get("termination_reason") or "infrastructure_error"
        error_ref = trace.artifacts.put_text(agent["error"], purpose="run infrastructure error")
        trace.emit(
            "run_error", status="error",
            attributes={"error_type": type(exc).__name__, "error": str(exc)},
            artifact_refs=[error_ref],
        )
        if solver_workspace.is_dir():
            try:
                recovered = _workspace_diff(solver_workspace)
                agent["patch"] = recovered
                atomic_write(run_root / "final" / "agent.patch", recovered.encode())
                ref = trace.artifacts.put_text(recovered, purpose="error-path recovered patch")
                trace.emit(
                    "patch_capture", status="error",
                    attributes={"patch_size": len(recovered.encode()), "patch_sha256": ref.sha256},
                    artifact_refs=[ref],
                )
            except Exception as recovery_error:
                trace.emit(
                    "patch_recovery_error", status="error",
                    attributes={"error": str(recovery_error)},
                )
        atomic_json(run_root / "final" / "baseline.json", baseline)
        atomic_json(run_root / "final" / "verification.json", patched)
        atomic_json(run_root / "final" / "outcome.json", {
            "classification": "INFRA_ERROR", "resolved": False,
            "run_completed": False, "error": agent["error"],
        })
    finally:
        if server is not None:
            server.stop()
        test_patch_value = verification.test_patch_path.read_text(encoding="utf-8", errors="replace")
        audit = audit_private_leaks(
            run_root,
            private_values={"test_patch": test_patch_value},
            private_hashes=private_hashes,
        )
        if not audit["passed"]:
            outcome = "INFRA_ERROR"
            completed = False
            atomic_json(run_root / "final" / "outcome.json", {
                "classification": outcome, "resolved": False, "run_completed": False,
                "error": "private_material_leak_detected",
            })
        summary = {
            "schema_version": "1.0",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "task_id": task.task_id,
            "completed": completed,
            "outcome": outcome,
            "termination_reason": agent.get("termination_reason"),
            "agent": {key: value for key, value in agent.items() if key != "patch"},
            "patch_size": len(agent.get("patch", "").encode()),
            "patch_sha256": hashlib.sha256(agent.get("patch", "").encode()).hexdigest(),
            "baseline": baseline,
            "verification": patched,
            "leak_audit_passed": audit["passed"],
        }
        atomic_json(run_root / "final" / "summary.json", summary)
        trace.emit("outcome", status="error" if outcome == "INFRA_ERROR" else "ok", attributes={
            "classification": outcome, "run_completed": completed,
            "termination_reason": agent.get("termination_reason"),
        })
        trace.finish(
            status="COMPLETE" if completed else "CRASHED",
            termination_reason=str(agent.get("termination_reason")), outcome=outcome,
        )
        materialize_trace(run_root)
        export_atif(run_root)
        render_reports(run_root)
        validation = validate_trace(run_root, require_complete=completed)
        atomic_json(run_root / "trace_validation.json", validation)
        seal_manifest(run_root, {
            "experiment_id": experiment_id,
            "run_id": run_id,
            "task_id": task.task_id,
            "trace_id": trace.trace_id,
            "outcome": outcome,
            "completed": completed,
            "configuration_sha256": config_hash,
            "private_input_hashes": private_hashes,
            "trace_validation": validation,
        })
        if work_root.exists():
            shutil.rmtree(work_root)
    return RunResult(
        experiment_id=experiment_id,
        run_id=run_id,
        task_id=task.task_id,
        run_directory=run_root,
        completed=completed,
        outcome=outcome,
        termination_reason=str(agent.get("termination_reason")),
        patch_path=run_root / "final" / "agent.patch",
    )
