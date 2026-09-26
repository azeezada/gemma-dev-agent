"""ADK workflow-agent emulation: SequentialAgent / ParallelAgent trees of LlmAgents.

Mirrors the parts of google-adk that change what the model sees:

- Session events carry an author and a branch. A ParallelAgent gives each child the
  branch ``<parent>.<child>``; an agent sees an event when the event has no branch or
  its own branch equals or extends the event's branch (``contents._is_event_belongs_to_branch``).
- Another agent's events reach the model as user text beginning ``For context:``
  (``contents._convert_foreign_event``); reasoning of other agents is dropped.
- ``include_contents: none`` keeps only events from the latest user message or other
  agent's reply onward (``contents._get_current_turn_contents``).
- An LlmAgent inside a workflow ends at its first response without tool calls; that
  text is saved to ``session.state[output_key]`` and instructions are re-templated from
  state for every agent start.
- The official runner re-runs the *root* from its first sub-agent after each nudge
  (HARNESS_README 5.3); a SequentialAgent is not resumable.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mvp_v2.harness import build_task_prompt, inject_session_state, skill_catalog_instruction
from mvp_v2.v3 import submission_request_settings

# HARNESS_README 5.3, "Normal stop without calling submit_patch".
NUDGE_TEXT = (
    "Please continue your work using the available tools, or call submit_patch when you "
    "have completed and verified your changes."
)
MAX_CONSECUTIVE_NUDGES = 3


def _visible(event: dict[str, Any], branch: str | None) -> bool:
    if not branch or not event.get("branch"):
        return True
    return branch == event["branch"] or branch.startswith(event["branch"] + ".")


def _foreign_message(event: dict[str, Any]) -> dict[str, Any] | None:
    author, message = event["author"], event["message"]
    lines = ["For context:"]
    if message["role"] == "assistant":
        if (message.get("content") or "").strip():
            lines.append(f"[{author}] said: {message['content']}")
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            lines.append(f"[{author}] called tool `{function.get('name')}` with parameters: {arguments}")
    elif message["role"] == "tool":
        lines.append(f"[{author}] `{message.get('name')}` tool returned result: {message.get('content')}")
    if len(lines) == 1:
        return None
    return {"role": "user", "content": "\n".join(lines)}


def _contents(events: list[dict[str, Any]], agent: str, branch: str | None,
              include_contents: str) -> list[dict[str, Any]]:
    visible = [event for event in events if _visible(event, branch)]
    if include_contents == "none":
        for index in range(len(visible) - 1, -1, -1):
            if visible[index]["author"] != agent:
                visible = visible[index:]
                break
    messages: list[dict[str, Any]] = []
    for event in visible:
        if event["author"] == "user":
            messages.append(dict(event["message"]))
        elif event["author"] == agent:
            messages.append(json.loads(json.dumps(event["message"])))
        else:
            converted = _foreign_message(event)
            if converted:
                messages.append(converted)
    # Drop own tool results whose call was cut off by include_contents=none.
    while messages and messages[0]["role"] == "tool":
        messages.pop(0)
    return messages


class WorkflowRunner:
    def __init__(self, *, client: Any, task: Any, config: dict[str, Any], tools: Any, trace: Any,
                 executor: Any, max_model_len: int, stats: dict[str, int], llm_loop: Any,
                 tool_definitions: Any, advertised_tools: Any, state: dict[str, Any]):
        self.client, self.task, self.config = client, task, config
        self.tools, self.trace, self.executor = tools, trace, executor
        self.max_model_len, self.stats = max_model_len, stats
        self._llm_loop, self._tool_definitions = llm_loop, tool_definitions
        self._advertised_tools = advertised_tools
        self.state = state
        self.events: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.tool_calls = 0

    # -- session -------------------------------------------------------------------
    def _append(self, author: str, branch: str | None, message: dict[str, Any]) -> None:
        with self._lock:
            self.events.append({"author": author, "branch": branch, "message": message})
            if message.get("role") == "assistant":
                self.tool_calls += len(message.get("tool_calls") or [])

    def _stop(self) -> bool:
        return self.tools.patch_submitted or self.tools.budget.remaining_seconds <= 0

    # -- agents ----------------------------------------------------------------------
    def run_node(self, node: dict[str, Any], branch: str | None) -> None:
        if self._stop():
            return
        kind = node["kind"]
        self.trace.emit("agent_transfer", attributes={
            "agent": node["name"], "direction": "enter", "agent_class": kind, "branch": branch,
        }, budget_after=self.tools.budget.snapshot())
        if kind == "SequentialAgent":
            for child in node["sub_agents"]:
                if self._stop():
                    break
                self.run_node(child, branch)
        elif kind == "ParallelAgent":
            children = node["sub_agents"]
            with ThreadPoolExecutor(max_workers=len(children)) as pool:
                futures = [
                    pool.submit(self.run_node, child,
                                f"{branch}.{child['name']}" if branch else f"{node['name']}.{child['name']}")
                    for child in children
                ]
                for future in futures:
                    future.result()
        else:
            self._run_llm(node, branch)
        self.trace.emit("agent_transfer", attributes={
            "agent": node["name"], "direction": "exit", "agent_class": kind, "branch": branch,
        }, budget_after=self.tools.budget.snapshot())

    def _run_llm(self, node: dict[str, Any], branch: str | None) -> None:
        with self._lock:
            instruction = inject_session_state(node["instruction"], self.state)
            history = _contents(self.events, node["name"], branch, node.get("include_contents", "default"))
        messages = [{"role": "system", "content": instruction + skill_catalog_instruction(node["skills"])}]
        messages.extend(history)
        if len(messages) == 1:
            messages.append({"role": "user", "content": "Continue."})
        start = len(messages)
        self.executor.agent_tools.update({name: self._agent_tool(node) for name in node.get("agent_tools", {})})
        with self.tools.scoped(node["tools"], node["skills"]), \
                self._advertised_tools(self.client, self._tool_definitions(node)):
            outcome = self._llm_loop(
                client=self.client, messages=messages, settings=submission_request_settings(node),
                tools=self.tools, trace=self.trace, executor=self.executor,
                max_model_len=self.max_model_len, agent_name=node["name"], root=False, stats=self.stats,
            )
        # Context compaction rewrites history; only the agent's own new turns become events.
        for message in messages[start:] if len(messages) >= start else []:
            if message.get("role") in {"assistant", "tool"}:
                self._append(node["name"], branch, message)
        text = outcome.get("final_text", "")
        if node.get("output_key"):
            with self._lock:
                self.state[node["output_key"]] = text
        self.trace.emit("agent_output", attributes={
            "agent": node["name"], "branch": branch, "output_key": node.get("output_key"),
            "termination_reason": outcome.get("termination_reason"), "chars": len(text),
            "text": text[:4000],
        }, budget_after=self.tools.budget.snapshot())

    def _agent_tool(self, parent: dict[str, Any]):
        def run(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            child = parent["agent_tools"][name]
            request = arguments.get("request")
            if set(arguments) != {"request"} or not isinstance(request, str) or not request.strip():
                return {"status": "error", "error_type": "ValidationError",
                        "error_message": f"{name} takes one non-empty string argument: request"}
            with self._lock:
                instruction = inject_session_state(child["instruction"], self.state)
            child_messages = [
                {"role": "system", "content": instruction + skill_catalog_instruction(child["skills"])},
                {"role": "user", "content": request},
            ]
            with self.tools.scoped(child["tools"], child["skills"]), \
                    self._advertised_tools(self.client, self._tool_definitions(child)):
                outcome = self._llm_loop(
                    client=self.client, messages=child_messages, settings=submission_request_settings(child),
                    tools=self.tools, trace=self.trace, executor=self.executor,
                    max_model_len=self.max_model_len, agent_name=name, root=False, stats=self.stats,
                )
            return {"result": outcome["final_text"]}
        return run

    # -- official outer loop ----------------------------------------------------------
    def run(self) -> str:
        public_task = {
            "instance_id": self.task.task_id, "repo": self.task.repository,
            "problem_statement": self.task.problem_statement, "hints_text": self.task.hints,
        }
        self._append("user", None, {
            "role": "user", "content": build_task_prompt(public_task, self.tools.workspace, self.tools.budget),
        })
        nudges = 0
        while True:
            calls_before = self.tool_calls
            self.run_node(self.config["tree"], None)
            if self.tools.patch_submitted:
                return "submitted_patch"
            if self.tools.budget.remaining_seconds <= 0:
                return "time_budget_exhausted"
            if self.tools.budget.turns_used >= self.tools.budget.max_turns:
                return "turn_budget_exhausted"
            nudges = 0 if self.tool_calls > calls_before else nudges + 1
            if nudges > MAX_CONSECUTIVE_NUDGES:
                return "max_nudges"
            self._append("user", None, {"role": "user", "content": NUDGE_TEXT})
            self.trace.emit("harness_intervention", attributes={
                "kind": "root_rerun_nudge", "consecutive": nudges,
            }, budget_after=self.tools.budget.snapshot())
