"""Serving-parity gate (plan 9, 17 step 5): per-token logprobs of fixed probe sequences must agree between HF
training (int4 QAT weights dequantised, + LoRA) and vLLM serving (int4 kernels, + the same LoRA).

HF logprobs come from `gemma_agent.train.lora_sft --probes`; vLLM logprobs from `/v1/completions` with
`prompt_logprobs`. The gate checks the base model and the adapter separately, and that the adapter moves
logprobs the same way in both stacks (catches adapters vLLM silently ignores, e.g. wrong module names).
"""
from __future__ import annotations

import json
import math
import urllib.request
from typing import Any

BASE_TOLERANCE = 0.1  # mean |delta logprob| in nats per token
EFFECT_MIN = 0.005    # below this the adapter barely changed anything; direction check is skipped
EFFECT_CORRELATION = 0.8


def vllm_prompt_logprobs(base_url: str, model: str, token_ids: list[int], timeout: int = 600) -> list[float | None]:
    payload = {"model": model, "prompt": token_ids, "max_tokens": 1, "temperature": 0.0, "prompt_logprobs": 1}
    request = urllib.request.Request(base_url.rstrip("/") + "/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode())
    entries = body["choices"][0]["prompt_logprobs"]
    values: list[float | None] = []
    for token, entry in zip(token_ids, entries):
        values.append(None if entry is None else float(entry[str(token)]["logprob"]))
    return values


def _pairs(a: list[float | None], b: list[float | None]) -> list[tuple[float, float]]:
    return [(x, y) for x, y in zip(a, b) if x is not None and y is not None]


def compare(hf: list[float | None], served: list[float | None]) -> dict[str, float]:
    diffs = sorted(abs(x - y) for x, y in _pairs(hf, served))
    if not diffs:
        return {"n": 0, "mean_abs": math.inf, "p95_abs": math.inf, "max_abs": math.inf}
    return {"n": len(diffs), "mean_abs": sum(diffs) / len(diffs),
            "p95_abs": diffs[min(len(diffs) - 1, int(0.95 * len(diffs)))], "max_abs": diffs[-1]}


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else float("nan")


def parity_gate(probes: dict[str, dict[str, list[float | None]]], *, tolerance: float = BASE_TOLERANCE,
                effect_min: float = EFFECT_MIN, effect_correlation: float = EFFECT_CORRELATION) -> dict[str, Any]:
    """`probes[id]` holds `hf_base`, `hf_adapter`, `vllm_base`, `vllm_adapter` per-token logprob lists."""
    rows, problems = {}, []
    for probe_id, p in probes.items():
        base, adapter = compare(p["hf_base"], p["vllm_base"]), compare(p["hf_adapter"], p["vllm_adapter"])
        quad = [(a, b, c, d) for a, b, c, d in zip(p["hf_base"], p["hf_adapter"], p["vllm_base"], p["vllm_adapter"])
                if None not in (a, b, c, d)]
        hf_effect = [b - a for a, b, _, _ in quad]
        vllm_effect = [d - c for _, _, c, d in quad]
        magnitude = sum(map(abs, hf_effect)) / len(hf_effect) if hf_effect else 0.0
        row = {"base": base, "adapter": adapter, "hf_effect_mean_abs": magnitude,
               "vllm_effect_mean_abs": sum(map(abs, vllm_effect)) / len(vllm_effect) if vllm_effect else 0.0,
               "effect_correlation": _correlation(hf_effect, vllm_effect)}
        if base["mean_abs"] > tolerance:
            problems.append(f"{probe_id}: base mean |d| {base['mean_abs']:.4f} > {tolerance}")
        if adapter["mean_abs"] > tolerance:
            problems.append(f"{probe_id}: adapter mean |d| {adapter['mean_abs']:.4f} > {tolerance}")
        if magnitude >= effect_min and not row["effect_correlation"] >= effect_correlation:
            problems.append(f"{probe_id}: adapter effect correlation {row['effect_correlation']:.3f} "
                            f"< {effect_correlation} (vLLM may not apply the adapter as trained)")
        row["effect_checked"] = magnitude >= effect_min
        rows[probe_id] = row
    return {"passed": bool(rows) and not problems, "problems": problems, "probes": rows,
            "thresholds": {"tolerance": tolerance, "effect_min": effect_min, "effect_correlation": effect_correlation}}
