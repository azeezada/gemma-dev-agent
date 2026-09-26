"""V3 configuration-driven runner; notebook embeds this after the shared harness."""
from .harness import *  # EMBED_SHARED_HARNESS

import posixpath

PYTEST_RUNTIME_VERSION = "8.3.4"


def _minimal_yaml_load(text: str, include: Callable[[str], Any]) -> Any:
    """Parse the tiny declarative subset used by submissions when PyYAML is absent."""
    source = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent]:
            raise ValueError(f"Tabs are not supported in YAML at line {number}")
        source.append((indent, raw.strip(), number))

    def scalar(value: str) -> Any:
        if value.startswith("!include "):
            return include(value.removeprefix("!include ").strip())
        lowered = value.casefold()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"null", "none", "~"}:
            return None
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return value[1:-1]
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value

    def block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(source) or source[index][0] < indent:
            return {}, index
        is_list = source[index][1].startswith("- ")
        result: Any = [] if is_list else {}
        while index < len(source):
            row_indent, content, number = source[index]
            if row_indent < indent:
                break
            if row_indent > indent:
                raise ValueError(f"Unexpected YAML indentation at line {number}")
            if is_list:
                if not content.startswith("- "):
                    raise ValueError(f"Mixed YAML list and mapping at line {number}")
                item = content[2:].strip()
                if re.match(r"[A-Za-z_][\w-]*:(\s|$)", item):
                    # "- key: ..." starts a mapping item whose keys sit two columns deeper.
                    source[index] = (indent + 2, item, number)
                    value, index = block(index, indent + 2)
                    result.append(value)
                    continue
                result.append(scalar(item))
                index += 1
                continue
            if content.startswith("- ") or ":" not in content:
                raise ValueError(f"Expected YAML mapping at line {number}")
            key, raw_value = content.split(":", 1)
            key, raw_value = key.strip(), raw_value.strip()
            if not key or key in result:
                raise ValueError(f"Duplicate or empty YAML key at line {number}: {key}")
            index += 1
            if raw_value:
                result[key] = scalar(raw_value)
            elif index < len(source) and source[index][0] > indent:
                result[key], index = block(index, source[index][0])
            else:
                result[key] = {}
        return result, index

    value, consumed = block(0, source[0][0] if source else 0)
    if consumed != len(source):
        raise ValueError("Could not parse complete YAML document")
    return value


def _load_skills(files: dict[str, str], declared: Any) -> dict[str, dict[str, Any]]:
    """Validate ADK skill directories: each needs SKILL.md with name/description frontmatter."""
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise ValueError("agent skills must be a list of relative directory paths")
    skills: dict[str, dict[str, Any]] = {}
    for directory in declared:
        directory = directory.strip("/")
        if not directory or Path(directory).is_absolute() or ".." in Path(directory).parts:
            raise ValueError(f"Invalid skill path: {directory}")
        manifest = files.get(f"{directory}/SKILL.md")
        if manifest is None:
            raise ValueError(f"Skill {directory} is missing SKILL.md")
        match = re.match(r"^---\n(.*?)\n---\n", manifest, re.S)
        if not match:
            raise ValueError(f"Skill {directory} SKILL.md lacks YAML frontmatter")
        meta = dict(
            (key.strip(), value.strip()) for key, _, value in
            (line.partition(":") for line in match.group(1).splitlines()) if key.strip()
        )
        name = meta.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or name in skills:
            raise ValueError(f"Invalid or duplicate skill name in {directory}: {name!r}")
        members = {key for key in files if key.startswith(directory + "/")}
        skills[name] = {
            "description": meta.get("description", ""),
            "files": {key[len(directory) + 1:]: files[key] for key in sorted(members)},
            "members": members,
        }
    return skills


def load_submission(files: dict[str, str]) -> dict[str, Any]:
    """Validate the small v3 declarative subset, without executing code."""
    try:
        import yaml
    except ModuleNotFoundError:
        yaml = None
    skill_members = {name for name in files if name.startswith("skills/")}
    required = {"agent.yaml", "eval_config.yaml"}
    unexpected = sorted(
        name for name in set(files) - skill_members - required
        if not re.fullmatch(r"(prompts/[\w.-]+\.(md|txt)|configs/[\w.-]+\.yaml|sub_agents/[\w.-]+\.yaml)", name)
    )
    if unexpected or not required <= set(files):
        raise ValueError(f"Unexpected or missing submission members: {unexpected or sorted(required - set(files))}")
    bad_skill_files = sorted(
        name for name in skill_members
        if not name.endswith((".md", ".txt", ".py")) or ".." in Path(name).parts or "__pycache__" in name
    )
    if bad_skill_files:
        raise ValueError(f"Unsupported skill files: {bad_skill_files}")
    if sum(len(value.encode()) for value in files.values()) >= 3 * 1024**3:
        raise ValueError("Submission exceeds archive size limit")
    visited: set[str] = set()

    def parse(name: str, stack: tuple[str, ...] = ()) -> Any:
        if name in stack or len(stack) >= 10:
            raise ValueError("Cyclic or excessive YAML includes")
        if name not in files or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError(f"Invalid include: {name}")
        visited.add(name)
        if name.endswith((".md", ".txt")):
            return files[name]
        def resolve(relative: str) -> str:
            # Relative to the including file, as in the official sample (sub_agents/ -> ../prompts/).
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), relative))
            if Path(relative).is_absolute() or "\x00" in relative or target.split("/")[0] == "..":
                raise ValueError("Include must stay within submission root")
            return target
        if yaml is None:
            return _minimal_yaml_load(files[name], lambda relative: parse(resolve(relative), (*stack, name)))
        class Loader(yaml.SafeLoader):
            pass
        def mapping(loader, node, deep=False):
            result = {}
            for key, value in loader.construct_pairs(node, deep=deep):
                if key in result:
                    raise ValueError(f"Duplicate YAML key: {key}")
                result[key] = value
            return result
        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        def include(loader, node):
            return parse(resolve(loader.construct_scalar(node)), (*stack, name))
        Loader.add_constructor("!include", include)
        return yaml.load(files[name], Loader=Loader)

    def fields(value, allowed, required, label):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            raise ValueError(f"Unsupported or missing {label} fields")

    def number(value, low, high, label, integer=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid {label}")
        if not low <= value <= high or (integer and not isinstance(value, int)):
            raise ValueError(f"Out of range {label}")

    def check_sampling(sampling, label):
        sample_keys = ("temperature", "top_p", "top_k", "seed", "max_output_tokens", "thinking_config")
        fields(sampling, sample_keys, sample_keys, f"{label} sampling")
        thinking = sampling["thinking_config"]
        thinking_keys = ("thinking_level", "thinking_budget", "include_thoughts")
        fields(thinking, thinking_keys, thinking_keys, f"{label} thinking")
        number(sampling["temperature"], 0, 2, "temperature")
        number(sampling["top_p"], 0, 1, "top_p")
        number(sampling["top_k"], 1, 100000, "top_k", True)
        number(sampling["seed"], 0, 2**32-1, "seed", True)
        number(sampling["max_output_tokens"], 1, 32768, "max_output_tokens", True)
        number(thinking["thinking_budget"], 1, sampling["max_output_tokens"] - 1, "thinking_budget", True)
        # HARNESS_README: thinking_level MINIMAL/LOW/MEDIUM/HIGH/NONE; include_thoughts false or NONE disables thinking.
        if not isinstance(thinking["include_thoughts"], bool) or str(thinking["thinking_level"]).lower() not in {
                "none", "minimal", "low", "medium", "high"}:
            raise ValueError(f"Invalid {label} thinking_level or include_thoughts")

    known_tools = {tool["name"] for tool in TOOL_SCHEMAS}
    skill_owners: set[str] = set()

    def llm_agent(path: str, *, root: bool, spec: dict[str, Any] | None = None,
                  require_submit: bool | None = None) -> dict[str, Any]:
        """One LlmAgent; the root may reference AgentTools (one level, ADK agent_tool)."""
        spec = parse(path) if spec is None else spec
        label = f"agent {path}"
        fields(spec, ("agent_class", "name", "description", "model", "instruction", "tools", "skills",
                      "generate_content_config", "output_key", "include_contents"),
               ("name", "model", "instruction", "tools", "generate_content_config"), label)
        if spec.get("include_contents", "default") not in {"default", "none"}:
            raise ValueError(f"include_contents must be default or none in {path}")
        if spec.get("output_key") is not None and not str(spec["output_key"]).isidentifier():
            raise ValueError(f"Invalid output_key in {path}")
        if spec.get("agent_class", "LlmAgent") != "LlmAgent":
            raise ValueError("Only LlmAgent agents may declare tools")
        if spec["model"] != "gemma-4-31b-it-qat-w4a16-ct":
            raise ValueError("Unexpected model identity")
        if not isinstance(spec["name"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec["name"]):
            raise ValueError(f"Invalid agent name in {path}")
        if not isinstance(spec["instruction"], str) or not spec["instruction"].strip():
            raise ValueError(f"Missing instruction in {path}")
        entries = spec["tools"]
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{label} tools must be a non-empty list")
        declared, agent_tools = [], {}
        for entry in entries:
            if isinstance(entry, str) and entry in known_tools and entry not in declared:
                declared.append(entry)
            elif isinstance(entry, dict) and set(entry) == {"agent_tool"}:
                ref = entry["agent_tool"]
                fields(ref, ("config_path", "skip_summarization"), ("config_path",), "agent_tool")
                if not isinstance(ref.get("skip_summarization", False), bool):
                    raise ValueError("skip_summarization must be a boolean")
                child = llm_agent(ref["config_path"], root=False)
                child["skip_summarization"] = ref.get("skip_summarization", False)
                if child["name"] in agent_tools or child["name"] in known_tools | set(SKILL_TOOL_NAMES):
                    raise ValueError(f"Agent tool name collides with another tool: {child['name']}")
                agent_tools[child["name"]] = child
            else:
                raise ValueError(f"Unsupported or duplicate tool entry in {path}: {entry!r}")
        if (root if require_submit is None else require_submit) and "submit_patch" not in declared:
            raise ValueError("The root agent must declare submit_patch")
        skills = _load_skills(files, spec.get("skills") or [])
        skill_owners.update(name for skill in skills.values() for name in skill["members"])
        check_sampling(spec["generate_content_config"], label)
        return {
            "name": spec["name"], "description": str(spec.get("description") or ""),
            "model": spec["model"], "instruction": spec["instruction"],
            # Model-visible tools: declared built-ins, ADK's skill tools when skills exist, agent tools.
            "tools": declared + (list(SKILL_TOOL_NAMES) if skills else []) + list(agent_tools),
            "declared_tools": declared,
            "skills": {name: {"description": skill["description"], "files": skill["files"]}
                       for name, skill in skills.items()},
            "sampling": spec["generate_content_config"],
            "agent_tools": agent_tools,
            "kind": "LlmAgent",
            "output_key": spec.get("output_key"),
            "include_contents": spec.get("include_contents", "default"),
        }

    workflow_classes = {"SequentialAgent", "ParallelAgent"}

    def node(path: str) -> dict[str, Any]:
        """Any agent in a workflow tree: SequentialAgent/ParallelAgent or LlmAgent leaf."""
        spec = parse(path)
        if not isinstance(spec, dict):
            raise ValueError(f"Agent config must be a mapping: {path}")
        kind = spec.get("agent_class", "LlmAgent")
        if kind == "LlmAgent":
            return llm_agent(path, root=False, spec=spec, require_submit=False)
        if kind not in workflow_classes:
            raise ValueError(f"Unsupported agent_class in {path}: {kind}")
        fields(spec, ("agent_class", "name", "description", "sub_agents"), ("name", "sub_agents"), f"agent {path}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(spec["name"])):
            raise ValueError(f"Invalid agent name in {path}")
        children = spec["sub_agents"]
        if not isinstance(children, list) or not children:
            raise ValueError(f"{path} sub_agents must be a non-empty list")
        subs = []
        for ref in children:
            fields(ref, ("config_path",), ("config_path",), "sub_agent reference")
            # ADK resolves config_path against the directory of the referencing YAML.
            subs.append(node(posixpath.normpath(posixpath.join(posixpath.dirname(path), ref["config_path"]))))
        return {"kind": kind, "name": spec["name"], "description": str(spec.get("description") or ""),
                "sub_agents": subs}

    def leaves(tree: dict[str, Any]) -> list[dict[str, Any]]:
        if tree["kind"] == "LlmAgent":
            return [tree, *tree["agent_tools"].values()]
        return [leaf for child in tree["sub_agents"] for leaf in leaves(child)]

    root_spec = parse("agent.yaml")
    if isinstance(root_spec, dict) and root_spec.get("agent_class") in workflow_classes:
        tree = node("agent.yaml")
        agents = leaves(tree)
        names = [leaf["name"] for leaf in agents]
        if len(names) != len(set(names)):
            raise ValueError("Agent names in a workflow tree must be unique")
        if not any("submit_patch" in leaf["declared_tools"] for leaf in agents):
            raise ValueError("Some agent in the workflow tree must declare submit_patch")
        union = lambda key: list(dict.fromkeys(tool for leaf in agents for tool in leaf[key]))
        agent = {
            "name": tree["name"], "description": tree["description"], "model": agents[0]["model"],
            "instruction": "", "tools": union("tools"), "declared_tools": union("declared_tools"),
            "skills": {name: skill for leaf in agents for name, skill in leaf["skills"].items()},
            "sampling": agents[0]["sampling"],
            "agent_tools": {name: child for leaf in agents for name, child in leaf["agent_tools"].items()},
            "tree": tree,
        }
    else:
        agent = llm_agent("agent.yaml", root=True)
    if skill_members - skill_owners:
        raise ValueError("Every file under skills/ must belong to a declared skill")
    evaluation = parse("eval_config.yaml")
    fields(evaluation, ("evaluation",), ("evaluation",), "evaluation root")
    limits = evaluation["evaluation"]
    limit_keys = ("max_time_minutes", "max_tool_calls", "max_turns", "timeout_seconds")
    fields(limits, limit_keys, limit_keys, "budget")
    for key, value in limits.items():
        number(value, 0.01 if key == "max_time_minutes" else 1, 720 if key == "max_time_minutes" else 10000,
               key, integer=key != "max_time_minutes")
    if visited != set(files) - skill_members:
        raise ValueError(f"Packaged files not reached through includes: {sorted(set(files) - skill_members - visited)}")
    return {
        **agent, "evaluation": limits,
        "files_sha256": {name: sha256_text(value) for name, value in sorted(files.items())},
    }


def agent_tool_schemas(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [agent_tool_schema(name, child["description"]) for name, child in config.get("agent_tools", {}).items()]


def submission_request_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config["sampling"]
    request = {
        **{key: settings[key] for key in ("temperature", "top_p", "top_k", "seed")},
        "max_tokens": settings["max_output_tokens"], "tool_choice": "auto",
    }
    if not thinking_enabled(settings):
        # Emulates HARNESS_README's "include_thoughts: false or thinking_level: NONE disables thinking".
        # The official mapping to vLLM is not documented; the Gemma template switch is the closest equivalent.
        return {**request, "chat_template_kwargs": {"enable_thinking": False}}
    level = settings["thinking_config"]["thinking_level"].lower()
    return {
        **request,
        "thinking_token_budget": settings["thinking_config"]["thinking_budget"],
        "reasoning_effort": "low" if level == "minimal" else level,
        "chat_template_kwargs": {"enable_thinking": True},
    }


def thinking_enabled(settings: dict[str, Any]) -> bool:
    thinking = settings.get("thinking_config") or {}
    return thinking.get("include_thoughts", True) is not False and str(thinking.get("thinking_level", "")).lower() != "none"


def budget_from_submission(config: dict[str, Any]) -> Budget:
    values = config["evaluation"]
    return Budget(max_time_minutes=values["max_time_minutes"], max_tool_calls=values["max_tool_calls"],
                  max_turns=values["max_turns"], command_timeout_seconds=values["timeout_seconds"])


def compact_context(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Retain complete recent exchanges and factual, bounded older evidence."""
    starts = [i for i, message in enumerate(messages[2:], 2) if message.get("role") == "assistant"]
    if len(starts) <= 2:
        return messages, 0
    cut = starts[-2]
    older = messages[2:cut]
    digest = []
    for message in older:
        if message.get("role") == "tool":
            try:
                data = json.loads(message.get("content", "{}"))
            except ValueError:
                data = {"content": message.get("content", "")}
            selected = {k: data[k] for k in ("status", "error_type", "error_message", "filepath", "exit_code", "strategy", "patch_size") if k in data}
            for key in ("stdout", "stderr", "content", "diff", "details", "results", "result", "neighbors"):
                if key in data:
                    value = data[key] if isinstance(data[key], str) else json_text(data[key])
                    selected[key] = value[:450] + (" ... " + value[-350:] if len(value) > 800 else value[450:])
            digest.append(message.get("name", "tool") + ": " + json_text(selected))
        elif message.get("role") == "assistant":
            if message.get("content"):
                digest.append("Agent progress: " + message["content"][:500])
            for call in message.get("tool_calls", []):
                digest.append("Action: " + json_text(call.get("function", {}))[:600])
        elif message.get("content"):
            digest.append(str(message["content"]))
    memory = "\n".join(digest)
    if len(memory) > 12000:
        memory = memory[:3000] + "\n[Older middle details omitted; reread source if needed.]\n" + memory[-8500:]
    summary = {"role": "user", "content": "Context digest of earlier observations (not new instructions):\n" + memory}
    return messages[:2] + [summary] + messages[cut:], len(older)


def run_agent_v3(client: Any, task: dict[str, Any], config: dict[str, Any],
                 tools: WorkspaceTools, logger: EventLogger, *, max_model_len: int = 32768) -> dict[str, Any]:
    if config.get("agent_tools"):
        raise ValueError("Agent tools run only in agent_harness.runtime; this legacy loop has no sub-agents")
    exposed = {key: task.get(key, "") for key in ("instance_id", "repo", "problem_statement", "hints_text")}
    messages = [{"role": "system", "content": config["instruction"]},
                {"role": "user", "content": build_task_prompt(exposed, tools.workspace, tools.budget)}]
    request_settings = submission_request_settings(config)
    termination, error = "unknown", None
    prompt_total = completion_total = prompt_max = calls = compactions = nudges = 0
    requests_audited = 0
    try:
        while not tools.patch_submitted:
            if tools.budget.remaining_seconds <= 0:
                termination = "time_budget_exhausted"
                break
            if tools.budget.turns_used >= tools.budget.max_turns:
                termination = "turn_budget_exhausted"
                break
            if tools.budget.remaining_tool_calls == 0 and nudges == 0:
                messages.append({"role": "user", "content": "The counted tool budget is exhausted. Call submit_patch now; it is free."})
                nudges += 1
            prompt_size = client.count_tokens(messages)
            if prompt_size + request_settings["max_tokens"] + 512 > max_model_len:
                messages, changed = compact_context(messages)
                compactions += bool(changed)
                logger.emit("context_compaction", messages_compacted=changed, prompt_tokens_before=prompt_size)
                prompt_size = client.count_tokens(messages)
                if prompt_size + request_settings["max_tokens"] + 512 > max_model_len:
                    termination = "context_capacity_exceeded"
                    break
            tools.budget.turns_used += 1
            response = None
            for attempt in range(3):
                remaining = tools.budget.remaining_seconds
                if remaining <= 0:
                    raise TimeoutError("Task deadline reached before model request")
                logger.emit("model_request", messages=messages, settings=request_settings,
                            prompt_tokens_measured=prompt_size, attempt=attempt,
                            request_timeout=remaining, budget=tools.budget.snapshot())
                requests_audited += 1
                started = time.monotonic()
                try:
                    response = client.chat(messages, **request_settings, request_timeout=remaining)
                    break
                except Exception as exc:
                    transient = (isinstance(exc, ModelHTTPError) and exc.status in {429, 500, 502, 503, 504}) or isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
                    logger.emit("model_request_error", error=str(exc), transient=transient,
                                attempt=attempt, latency_seconds=time.monotonic() - started)
                    if not transient or attempt == 2 or tools.budget.remaining_seconds <= 0:
                        raise
                    time.sleep(min(0.5 * 2**attempt, tools.budget.remaining_seconds))
            calls += 1
            choice = response["choices"][0]
            message = choice["message"]
            usage = response.get("usage", {})
            prompt_total += int(usage.get("prompt_tokens") or prompt_size)
            completion_total += int(usage.get("completion_tokens") or 0)
            prompt_max = max(prompt_max, int(usage.get("prompt_tokens") or prompt_size))
            logger.emit("model_turn", turn=tools.budget.turns_used, message=message,
                        usage=usage, finish_reason=choice.get("finish_reason"),
                        latency_seconds=time.monotonic() - started, budget=tools.budget.snapshot())
            history = client.assistant_history(message) if hasattr(client, "assistant_history") else {**message, "role": "assistant"}
            messages.append(history)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                nudges += 1
                if nudges > 3:
                    termination = "max_nudges"
                    break
                messages.append({"role": "user", "content": "Continue with tools or call submit_patch when the fix is ready. A prose answer does not submit the workspace diff."})
                continue
            nudges = 0
            for call in tool_calls:
                function = call.get("function", {})
                name = str(function.get("name", ""))
                arguments = None
                try:
                    raw = function.get("arguments", "{}")
                    arguments = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                except (TypeError, ValueError) as exc:
                    result = {"status": "error", "error_type": "ToolArgumentsJSONError", "error_message": str(exc)}
                    logger.emit("tool_result", tool=name, arguments=raw, result=result)
                    # Keep the original in diagnostics, but replay valid JSON so the
                    # server's chat template can deliver the error on the next turn.
                    for history_call in history.get("tool_calls", []):
                        if history_call.get("id") == call.get("id"):
                            history_call["function"]["arguments"] = "{}"
                else:
                    result = tools.dispatch(name, arguments)
                messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                 "name": name, "content": json_text(result)})
                if tools.patch_submitted:
                    termination = "submitted_patch"
                    break
            if tools.budget.remaining_tool_calls == 0 and not tools.patch_submitted:
                if sum(row.get("event") == "tool_result" and row.get("result", {}).get("error_type") == "ToolBudgetExceeded" for row in logger.rows) >= 2:
                    termination = "tool_budget_exhausted"
                    break
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        termination = "time_budget_exhausted" if tools.budget.remaining_seconds <= 0 else "model_error"
        logger.emit("agent_error", error=error, termination_reason=termination)
    finally:
        if not tools.patch_submitted:
            try:
                logger.emit("automatic_patch_recovery", result=tools.submit_patch())
            except Exception as exc:
                error = (error or "") + f"; patch recovery failed: {exc}"
                logger.emit("patch_recovery_error", error=str(exc))
        (logger.root / "messages.json").write_text(json.dumps(messages, indent=2), encoding="utf-8")
        transcript = []
        for row in logger.rows:
            if row["event"] in {"model_turn", "tool_result", "context_compaction", "model_request_error", "agent_error", "automatic_patch_recovery"}:
                transcript.append("## " + row["event"] + "\n\n```json\n" + json.dumps(row, indent=2, default=str) + "\n```\n")
        (logger.root / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")
    return {
        "termination_reason": termination, "error": error, "patch": tools.submitted_patch,
        "patch_submitted_explicitly": termination == "submitted_patch",
        "model_calls": calls, "prompt_tokens_total": prompt_total, "prompt_tokens_max": prompt_max,
        "completion_tokens_total": completion_total, "context_compactions": compactions,
        "budget": tools.budget.snapshot(), "messages": messages, "requests_audited": requests_audited,
        "rejected_commands": tools.rejected_commands,
    }


def audit_trajectory(task: dict[str, Any], logger: EventLogger, result: dict[str, Any]) -> dict[str, Any]:
    """Audit every immutable request and every tool result; not an OS isolation proof."""
    requests = [row for row in logger.rows if row["event"] == "model_request"]
    tool_results = [row for row in logger.rows if row["event"] == "tool_result"]
    hits = []
    matches = []
    for index, row in enumerate(requests + tool_results):
        payload = row.get("messages", row.get("result"))
        text = json_text(payload)
        # A whole task-record leak is distinguishable from an independently produced diff.
        if '\\"test_patch\\"' in text or '"test_patch":' in text:
            hits.append({"record": index, "kind": "task_record_field"})
        for key in ("patch", "test_patch"):
            secret = str(task.get(key) or "")
            encoded = json.dumps(secret, ensure_ascii=False)[1:-1]
            if secret and (secret in text or encoded in text):
                matches.append({"record": index, "field": key})
                # Synthetic leak sentinels support adversarial regression tests. Real diffs
                # can legitimately match an independently generated solution; report those.
                if not secret.startswith(("diff --git", "--- ")):
                    hits.append({"record": index, "kind": "reference_sentinel"})
        if row["event"] == "tool_result" and row.get("tool") == "run_command":
            command = str(row.get("arguments", {}).get("command", "")) if isinstance(row.get("arguments"), dict) else ""
            result_data = row.get("result", {})
            if any(value in command for value in ("/kaggle/input", "tasks.jsonl")) and result_data.get("error_type") != "CommandPolicyError":
                hits.append({"record": index, "kind": "unguarded_data_access"})
    audit = {
        "instance_id": task["instance_id"], "passed": not hits and len(requests) == result.get("requests_audited", -1),
        "outbound_requests_checked": len(requests), "tool_results_checked": len(tool_results),
        "hits": hits, "reference_text_matches_for_review": matches,
        "controller_fields_exposed": ["instance_id", "repo", "problem_statement", "hints_text"],
        "reference_patch_sha256": sha256_text(str(task.get("patch", ""))),
        "test_patch_sha256": sha256_text(str(task.get("test_patch", ""))),
        "rejected_commands": result.get("rejected_commands", []),
        "verifier_started_after_agent": True,
        "limitations": "Content/provenance audit plus command guard, not an OS sandbox. Matching diffs alone cannot prove leakage.",
    }
    (logger.root / "leak_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def classify_outcome(agent: dict[str, Any], patched: dict[str, Any], baseline: dict[str, Any]) -> str:
    if agent.get("termination_reason") == "model_error" or agent.get("error") and "patch recovery failed" in agent["error"]:
        return "INFRA_ERROR"
    if any(value.get("infrastructure_error") or value.get("error") not in {None, "empty_agent_patch"}
           for value in (patched, baseline)):
        return "INFRA_ERROR"
    if not agent.get("patch", "").strip():
        return "FAIL"
    if patched.get("resolved"):
        return "PASS" if baseline.get("test_failure") else "INCONCLUSIVE"
    return "FAIL"


def synthetic_preflight(client: OpenAIHTTPClient, config: dict[str, Any], root: Path) -> dict[str, Any]:
    """Exercise native automatic tool choice and reasoning before exposing a task."""
    messages = [{"role": "system", "content": "Use the provided tools. Do not answer from memory."},
                {"role": "user", "content": "Call get_status now to inspect the workspace budget. Do not call other tools."}]
    settings = submission_request_settings(config)
    response = client.chat(messages, **settings, request_timeout=120)
    choice = response["choices"][0]
    calls = choice["message"].get("tool_calls") or []
    valid = any(call.get("function", {}).get("name") == "get_status"
                and json.loads(call["function"].get("arguments") or "{}") == {} for call in calls)
    record = {"passed": valid, "request_settings": settings, "response": response,
              "prompt_tokens_measured": client.count_tokens(messages)}
    (root / "preflight.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if not valid:
        raise RuntimeError("Synthetic native tool-call preflight failed; see preflight.json")
    return record


def benchmark_v3(*, client: Any, tasks: dict[str, dict[str, Any]], task_ids: tuple[str, ...],
                 config: dict[str, Any], data_root: Path, output_root: Path, work_root: Path,
                 max_model_len: int = 32768) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries, audits = [], []
    for task_id in task_ids:
        task = tasks[task_id]
        started = time.monotonic()
        root = output_root / "tasks" / task_id
        logger = EventLogger(root)
        temporary = work_root / task_id
        agent_root = temporary / "agent"
        agent_workspace = agent_root / "workspace"
        agent_venv = agent_root / "venv"
        patch_path = output_root / "patches" / f"{task_id}.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"instance_id": task_id, "repo": task["repo"], "status": "INFRA_ERROR",
               "resolved": False, "agent_executed": False}
        agent = None
        try:
            try:
                logger.emit("gpu_sample", phase="task_start", inventory=gpu_inventory())
            except Exception as exc:
                logger.emit("gpu_sample_unavailable", error=str(exc))
            safe_extract(data_root / "snapshots" / f"{task_id}.tgz", agent_workspace)
            python = create_task_venv(agent_venv, agent_workspace, data_root / "wheels", pytest_version=PYTEST_RUNTIME_VERSION)
            configure_workspace(agent_workspace)
            setup_seconds = time.monotonic() - started
            # Keep the solver's input an explicit allowlist; verifier-only fields stay here.
            public_task = {key: task.get(key, "") for key in ("instance_id", "repo", "problem_statement", "hints_text")}
            tools = WorkspaceTools(agent_workspace, budget_from_submission(config), logger, python=python,
                                   graph_path=data_root / "graphs" / f"{task_id}.json",
                                   embeddings_path=data_root / "embeddings" / f"{task_id}.npz")
            agent = run_agent_v3(client, public_task, config, tools, logger, max_model_len=max_model_len)
            row["agent_executed"] = True
            patch_path.write_text(agent["patch"], encoding="utf-8")
            patch_bytes = patch_path.read_bytes()
            audit = audit_trajectory(task, logger, agent)
            audits.append(audit)
            row.update({key: agent[key] for key in (
                "termination_reason", "error", "patch_submitted_explicitly", "model_calls",
                "prompt_tokens_total", "prompt_tokens_max", "completion_tokens_total", "context_compactions", "budget")})
            row.update({"setup_seconds": round(setup_seconds, 3), "patch_size": len(patch_bytes),
                        "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
                        "leak_audit_passed": audit["passed"], "valid_tool_calls": tools.budget.tool_calls_used})
            verification = {}
            # Each phase gets a fresh repository AND venv; none can mutate the agent's env.
            for phase, patch in (("patched", agent["patch"]), ("baseline", "")):
                if phase == "patched" and not patch.strip():
                    verification[phase] = {"resolved": False, "error": "empty_agent_patch"}
                    continue
                phase_root = temporary / phase
                phase_root.mkdir(parents=True, exist_ok=True)
                log_path = output_root / "test_outputs" / f"{task_id}.{phase}.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    verification[phase] = verify_patch(
                        snapshot=data_root / "snapshots" / f"{task_id}.tgz",
                        workspace=phase_root / "workspace", agent_patch=patch, test_patch=task["test_patch"],
                        python=python, venv=phase_root / "venv", wheels_dir=data_root / "wheels",
                        pytest_version=PYTEST_RUNTIME_VERSION,
                        output_log=log_path,
                    )
                except Exception as exc:
                    verification[phase] = {"resolved": False, "infrastructure_error": True,
                                           "error": f"{type(exc).__name__}: {exc}"}
                finally:
                    for filename in ("task_dependency_setup.json", "editable_install.json"):
                        if (phase_root / filename).exists():
                            shutil.copy2(phase_root / filename, root / f"{phase}_{filename}")
                    if phase_root.exists():
                        shutil.rmtree(phase_root)
            if patch_path.read_bytes() != patch_bytes:
                raise RuntimeError("Frozen agent patch changed during verification")
            row.update({"verification": verification["patched"], "baseline": verification["baseline"]})
            row["status"] = classify_outcome(agent, verification["patched"], verification["baseline"])
            if not audit["passed"]:
                row["status"] = "INFRA_ERROR"
            row["resolved"] = row["status"] == "PASS"
        except Exception as exc:
            row.update({"error": f"{type(exc).__name__}: {exc}", "status": "INFRA_ERROR", "resolved": False})
            logger.emit("task_exception", error=row["error"])
        finally:
            try:
                logger.emit("gpu_sample", phase="task_end", inventory=gpu_inventory())
            except Exception as exc:
                logger.emit("gpu_sample_unavailable", error=str(exc))
            for filename in ("task_dependency_setup.json", "editable_install.json"):
                if (agent_root / filename).exists():
                    shutil.copy2(agent_root / filename, root / f"agent_{filename}")
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            summaries.append(row)
            (root / "summary.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
            with (output_root / "task_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json_text(row) + "\n")
            if temporary.exists():
                shutil.rmtree(temporary)
        print(f"{task_id}: {row['status']} calls={row.get('valid_tool_calls', 0)} patch={row.get('patch_size', 0)} bytes", flush=True)
    return summaries, audits
