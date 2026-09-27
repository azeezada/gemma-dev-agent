"""Token-level training data (plan 6.2.2).

The runtime writes `tokens/turns.jsonl` in every run bundle: per model call, the prompt token ids vLLM rendered
and the completion token ids it generated (`return_token_ids`). Training sequences are cut from those ids, so they
match serving by construction. `parity_report` re-renders every recorded prompt with the HF tokenizer (the
renderer used for teacher runs, which have no Gemma token ids) and reports where it differs from vLLM.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from gemma_agent.env.harness import template_messages, template_token_ids

TURNS = Path("tokens") / "turns.jsonl"


def load_turns(run_root: Path) -> list[dict[str, Any]]:
    path = Path(run_root) / TURNS
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _artifact(run_root: Path, relative: str) -> Any:
    return json.loads((Path(run_root) / relative).read_text(encoding="utf-8"))


def template_kwargs(settings: dict[str, Any]) -> dict[str, Any]:
    """Chat-template kwargs of one request, as the client sent them to vLLM."""
    return {"enable_thinking": True, **(settings.get("chat_template_kwargs") or {})}


def render_prompt_ids(tokenizer: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                      kwargs: dict[str, Any]) -> list[int]:
    """The training renderer: messages -> prompt token ids, the way vLLM's chat endpoint renders them."""
    encoded = tokenizer.apply_chat_template(
        template_messages(messages), tools=tools, tokenize=True, add_generation_prompt=True, **kwargs,
    )
    return template_token_ids(encoded)


def _first_difference(left: list[int], right: list[int]) -> int | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return None if len(left) == len(right) else min(len(left), len(right))


def parity_report(tokenizer: Any, run_roots: Iterable[Path],
                  extra_kwargs: Iterable[dict[str, Any]] = ({}, )) -> dict[str, Any]:
    """Compare the renderer with vLLM's prompt ids on every recorded turn.

    `extra_kwargs` are alternative template kwargs tried in order (e.g. forwarding `reasoning_effort`); the report
    names the first variant that reproduces vLLM on every turn, or None. Also measures prefix consistency: whether
    the next prompt starts with this prompt + completion, which decides how many training sequences a run yields.
    """
    variants = [dict(v) for v in extra_kwargs]
    matches = [0] * len(variants)
    turns_total = turns_with_ids = consistent = transitions = 0
    mismatches: list[dict[str, Any]] = []
    for run_root in run_roots:
        run_root = Path(run_root)
        turns = load_turns(run_root)
        previous: dict[str, list[int]] = {}
        for turn in turns:
            turns_total += 1
            prompt, completion = turn.get("prompt_token_ids"), turn.get("completion_token_ids")
            if not prompt:
                continue
            turns_with_ids += 1
            agent = turn.get("agent", "")
            if agent in previous:
                transitions += 1
                consistent += prompt[: len(previous[agent])] == previous[agent]
            previous[agent] = list(prompt) + list(completion or [])
            messages = _artifact(run_root, turn["messages"])
            tools = _artifact(run_root, turn["tools"])
            settings = _artifact(run_root, turn["settings"])
            for index, variant in enumerate(variants):
                kwargs = {**template_kwargs(settings), **{k: settings[v] for k, v in variant.items() if v in settings}}
                rendered = render_prompt_ids(tokenizer, messages, tools, kwargs)
                if rendered == list(prompt):
                    matches[index] += 1
                elif index == 0 and len(mismatches) < 20:
                    at = _first_difference(rendered, list(prompt))
                    mismatches.append({
                        "run": run_root.name, "agent": agent, "turn": turn.get("turn"), "first_difference": at,
                        "rendered_len": len(rendered), "vllm_len": len(prompt),
                        "rendered_window": rendered[max(0, (at or 0) - 8):(at or 0) + 8],
                        "vllm_window": list(prompt)[max(0, (at or 0) - 8):(at or 0) + 8],
                    })
    exact = [variants[i] for i, n in enumerate(matches) if turns_with_ids and n == turns_with_ids]
    return {
        "turns": turns_total, "turns_with_vllm_ids": turns_with_ids,
        "exact_matches_by_variant": [{"variant": v, "matches": n} for v, n in zip(variants, matches)],
        "renderer_variant": exact[0] if exact else None,
        "passed": bool(turns_with_ids) and bool(exact),
        "prefix_consistent_rate": round(consistent / transitions, 4) if transitions else None,
        "mismatches": mismatches,
    }


def build_sequences(turns: list[dict[str, Any]], max_len: int = 32_768) -> list[dict[str, Any]]:
    """Merge a conversation's turns into as few training sequences as the exact ids allow.

    A turn extends the current sequence when its prompt starts with that sequence; otherwise the current
    sequence is closed and a new one starts from the full prompt. Loss mask is 1 on generated tokens only.
    Turns without vLLM ids are skipped (they cannot be trained on exactly).
    """
    sequences: list[dict[str, Any]] = []
    current: dict[str, list[int]] = {}

    def close(agent: str) -> None:
        seq = current.pop(agent, None)
        if seq and any(seq["loss_mask"]) and len(seq["input_ids"]) <= max_len:
            sequences.append({"agent": agent, **seq, "turns": seq["turns"]})

    for turn in turns:
        prompt, completion = turn.get("prompt_token_ids"), turn.get("completion_token_ids")
        if not prompt or not completion:
            continue
        agent = turn.get("agent", "")
        seq = current.get(agent)
        if seq is not None and list(prompt[: len(seq["input_ids"])]) == seq["input_ids"]:
            added = list(prompt[len(seq["input_ids"]):])
            seq["input_ids"] += added + list(completion)
            seq["loss_mask"] += [0] * len(added) + [1] * len(completion)
            seq["turns"] += 1
        else:
            close(agent)
            current[agent] = {
                "input_ids": list(prompt) + list(completion),
                "loss_mask": [0] * len(prompt) + [1] * len(completion),
                "turns": 1,
            }
    for agent in list(current):
        close(agent)
    return sequences
