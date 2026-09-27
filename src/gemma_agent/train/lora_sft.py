"""LoRA SFT on exact vLLM token sequences (plan 9; MVP notebook stage 4) and HF probe logprobs (stage 5).

Base = the competition's int4 QAT checkpoint dequantised to bf16 by transformers (compressed-tensors
`dequantize=True`; `run_compressed=False` on older transformers), spread over the GPUs with `device_map`: the same
weights vLLM serves in int4.
Loss: next-token CE on generated tokens only (`loss_mask`), computed in chunks under activation checkpointing so
the 262k-vocab logits never exist for the whole sequence. Runs as a subprocess so GPU memory is fully released
before vLLM restarts:

    python -m gemma_agent.train.lora_sft --model M --data sft.jsonl --out adapter/ --run-dir run/ \
        [--probes probes.jsonl --probe-out hf_probe_logprobs.json]
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import random
import time
from pathlib import Path
from typing import Any

from gemma_agent.observe.registry import log_metric

LORA_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")
# Language-model projections only: the checkpoint may carry vision/audio towers (plan 9).
TARGET_REGEX = r"^(?!.*(?:vision|audio|multi_modal|embed_vision|embed_audio)).*\.(?:" + "|".join(LORA_MODULES) + r")$"
CHUNK = 2048


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def labels_for(input_ids: list[int], loss_mask: list[int], max_len: int) -> tuple[list[int], list[int]]:
    """Prefix-truncate to `max_len` (exact: a prefix of a valid conversation is valid) and shift labels;
    label t is input t+1 when that token was generated, else -100."""
    ids, mask = input_ids[:max_len], loss_mask[:max_len]
    return ids, [ids[t + 1] if mask[t + 1] else -100 for t in range(len(ids) - 1)]


def normalise_adapter_config(adapter_dir: Path) -> dict[str, Any]:
    """PEFT stores the regex; list the adapted leaf modules instead (what vLLM's loader expects)."""
    from gemma_agent.eval.package import LORA_KEY, safetensors_header
    path = Path(adapter_dir) / "adapter_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    keys = safetensors_header(Path(adapter_dir) / "adapter_model.safetensors")
    config["target_modules"] = sorted({k.split(".lora_")[0].rsplit(".", 1)[-1] for k in keys if LORA_KEY.search(k)})
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs GPUs
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--max-len", type=int, default=16384)
    parser.add_argument("--max-memory-gib", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--probes", type=Path)
    parser.add_argument("--probe-out", type=Path)
    args = parser.parse_args(argv)

    import torch
    import torch.nn.functional as F
    import transformers
    from peft import LoraConfig, get_peft_model
    from torch.utils.checkpoint import checkpoint
    from transformers import AutoConfig, CompressedTensorsConfig

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def metric(name: str, value: float, **tags: Any) -> None:
        log_metric(run_dir, name, float(value), stage="train", **tags)
        print(f"[train] {name}={value} {tags or ''}", flush=True)

    config = AutoConfig.from_pretrained(args.model)
    architecture = getattr(transformers, config.architectures[0])
    gpus = torch.cuda.device_count()
    dtype_key = "dtype" if tuple(map(int, transformers.__version__.split(".")[:2])) >= (4, 56) else "torch_dtype"
    dequantize = ({"dequantize": True} if "dequantize" in inspect.signature(CompressedTensorsConfig).parameters
                  else {"run_compressed": False})
    print(f"[train] loading {config.architectures[0]} with CompressedTensorsConfig({dequantize})", flush=True)
    model = architecture.from_pretrained(
        args.model, device_map="auto", attn_implementation="sdpa",
        max_memory={i: f"{args.max_memory_gib}GiB" for i in range(gpus)},
        quantization_config=CompressedTensorsConfig(**dequantize), **{dtype_key: torch.bfloat16},
    )
    metric("load_seconds", time.monotonic() - started)
    for i in range(gpus):
        metric("gpu_memory_after_load_gib", torch.cuda.memory_allocated(i) / 2**30, gpu=i)
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.alpha, lora_dropout=0.0, bias="none",
        target_modules=TARGET_REGEX, task_type="CAUSAL_LM",
    ))
    trainable = [p for p in model.parameters() if p.requires_grad]
    metric("trainable_parameters", sum(p.numel() for p in trainable))

    base = model.get_base_model()
    inner = base.model
    language_model = getattr(inner, "language_model", inner)
    lm_head = base.get_output_embeddings()
    cap = getattr(config.get_text_config(), "final_logit_softcapping", None)
    input_device = language_model.get_input_embeddings().weight.device
    head_device = lm_head.weight.device

    def hidden(ids: "torch.Tensor") -> "torch.Tensor":
        return language_model(input_ids=ids.to(input_device), use_cache=False).last_hidden_state[0]

    def head_logits(h: "torch.Tensor") -> "torch.Tensor":
        logits = lm_head(h.to(head_device)).float()
        return torch.tanh(logits / cap) * cap if cap else logits

    # Self-check: the language-model path must reproduce the full model's logits on text-only input.
    with torch.no_grad():
        probe_ids = torch.tensor([load_rows(args.data)[0]["input_ids"][:64]])
        full = base(input_ids=probe_ids.to(input_device), use_cache=False).logits[0].float()
        mine = head_logits(hidden(probe_ids))
        drift = (full.to(mine.device) - mine).abs().max().item()
    metric("lm_path_max_logit_drift", drift)
    if drift > 0.05:
        raise RuntimeError(f"language-model forward path differs from the full model (max |d logit| {drift})")

    def chunk_loss(h: "torch.Tensor", labels: "torch.Tensor") -> "torch.Tensor":
        return F.cross_entropy(head_logits(h), labels.to(head_device), ignore_index=-100, reduction="sum")

    def sequence_loss(ids: list[int], labels: list[int]) -> tuple["torch.Tensor", int]:
        h = hidden(torch.tensor([ids]))[:-1]
        target = torch.tensor(labels)
        total = torch.zeros((), device=head_device)
        for start in range(0, len(labels), CHUNK):
            part = target[start:start + CHUNK]
            if (part != -100).any():
                total = total + checkpoint(chunk_loss, h[start:start + CHUNK], part, use_reentrant=False)
        return total, int((target != -100).sum())

    rows = load_rows(args.data)
    examples = [labels_for(r["input_ids"], r["loss_mask"], args.max_len) for r in rows]
    examples = [(ids, labels) for ids, labels in examples if any(l != -100 for l in labels)]
    metric("sequences", len(examples))
    metric("train_tokens", sum(len(ids) for ids, _ in examples))
    optimizer = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=0.0)
    step = 0
    model.train()
    for epoch in range(args.epochs):
        order = list(range(len(examples)))
        random.shuffle(order)
        for batch_start in range(0, len(order), args.grad_accum):
            batch = [examples[i] for i in order[batch_start:batch_start + args.grad_accum]]
            tick = time.monotonic()
            targets = sum(sum(l != -100 for l in labels) for _, labels in batch)
            loss_value = 0.0
            for ids, labels in batch:
                loss_sum, _ = sequence_loss(ids, labels)
                (loss_sum / targets).backward()
                loss_value += loss_sum.item() / targets
            grad_norm = torch.nn.utils.clip_grad_norm_(trainable, 1.0).item()
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            seconds = time.monotonic() - tick
            tokens = sum(len(ids) for ids, _ in batch)
            metric("loss", loss_value, step=step, epoch=epoch)
            metric("grad_norm", grad_norm, step=step)
            metric("tokens_per_second", tokens / seconds, step=step)
            metric("peak_memory_gib", max(torch.cuda.max_memory_allocated(i) for i in range(gpus)) / 2**30, step=step)
            if not math.isfinite(loss_value):
                raise RuntimeError("non-finite loss")
    metric("train_seconds", time.monotonic() - started)
    args.out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.out, safe_serialization=True)
    normalise_adapter_config(args.out)

    if args.probes and args.probe_out:
        model.eval()

        def logprobs(ids: list[int]) -> list[float | None]:
            with torch.no_grad():
                h = hidden(torch.tensor([ids]))[:-1]
                values: list[float | None] = [None]
                for start in range(0, len(ids) - 1, CHUNK):
                    lp = torch.log_softmax(head_logits(h[start:start + CHUNK]), dim=-1)
                    target = torch.tensor(ids[start + 1:start + 1 + CHUNK], device=lp.device)
                    values += lp.gather(1, target[:, None])[:, 0].tolist()
                return values

        out: dict[str, Any] = {}
        for probe in load_rows(args.probes):
            ids = probe["input_ids"]
            with model.disable_adapter():
                hf_base = logprobs(ids)
            out[probe["id"]] = {"input_ids": ids, "hf_base": hf_base, "hf_adapter": logprobs(ids)}
        args.probe_out.write_text(json.dumps(out), encoding="utf-8")
        metric("probe_sequences", len(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
