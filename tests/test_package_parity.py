"""Submission packaging with adapters, the serving-parity gate, and SFT label construction."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from gemma_agent.env.v3 import load_submission
from gemma_agent.eval.package import check_adapter, stage_submission, with_adapter, zip_submission
from gemma_agent.eval.parity import parity_gate
from gemma_agent.train.lora_sft import TARGET_REGEX, labels_for, normalise_adapter_config

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "base_model.model.model.language_model.layers.0"


def write_adapter(path: Path, rank: int = 16, keys: list[str] | None = None) -> Path:
    path.mkdir(parents=True)
    keys = keys or [f"{PREFIX}.self_attn.q_proj.lora_A.weight", f"{PREFIX}.self_attn.q_proj.lora_B.weight",
                    f"{PREFIX}.mlp.down_proj.lora_A.weight", f"{PREFIX}.mlp.down_proj.lora_B.weight"]
    header, offset = {}, 0
    for key in keys:
        shape = [rank, 64] if "lora_A" in key else [64, rank]
        size = shape[0] * shape[1] * 2
        header[key] = {"dtype": "BF16", "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    blob = json.dumps(header).encode()
    (path / "adapter_model.safetensors").write_bytes(struct.pack("<Q", len(blob)) + blob + b"\0" * offset)
    (path / "adapter_config.json").write_text(json.dumps({"r": rank, "lora_alpha": 32, "target_modules": TARGET_REGEX}))
    return path


def test_adapter_checks(tmp_path):
    good = check_adapter(write_adapter(tmp_path / "good"))
    assert good["ok"] and good["modules"] == ["down_proj", "q_proj"] and good["rank"] == 16
    vision = check_adapter(write_adapter(tmp_path / "vision", keys=["base_model.model.model.vision_tower.q_proj.lora_A.weight"]))
    assert not vision["ok"] and "non-language-model" in vision["problems"][0]
    assert not check_adapter(write_adapter(tmp_path / "big", rank=256))["ok"]
    config = normalise_adapter_config(tmp_path / "good")
    assert config["target_modules"] == ["down_proj", "q_proj"]


def test_stage_and_zip_submission_with_adapter(tmp_path):
    adapter = write_adapter(tmp_path / "adapter")
    stage = stage_submission(ROOT / "harnesses" / "train_v1", tmp_path / "stage", {"mvp_lora": adapter}, "mvp_lora")
    agent = (stage / "agent.yaml").read_text()
    assert "model: gemma-4-31b-it-qat-w4a16-ct\nadapter: mvp_lora\n" in agent
    text = {p.relative_to(stage).as_posix(): p.read_text() for p in stage.rglob("*")
            if p.is_file() and p.parts[len(stage.parts)] != "adapters"}
    assert load_submission(text)["adapter"] == "mvp_lora"
    report = zip_submission(stage, tmp_path / "submission.zip")
    assert report["ok"] and report["declared_adapters"] == ["mvp_lora"]
    with zipfile.ZipFile(tmp_path / "submission.zip") as archive:
        assert "adapters/mvp_lora/adapter_model.safetensors" in archive.namelist()
        assert "agent.yaml" in archive.namelist()


def test_missing_adapter_and_bad_extension_fail(tmp_path):
    stage = stage_submission(ROOT / "harnesses" / "train_v1", tmp_path / "stage", {}, "absent")
    (stage / "notes.bin").write_bytes(b"x")
    with pytest.raises(ValueError, match="missing.*absent|disallowed"):
        zip_submission(stage, tmp_path / "s.zip")


def test_with_adapter_replaces_existing_line():
    assert with_adapter("name: a\nmodel: m\nadapter: old\n", "new") == "name: a\nmodel: m\nadapter: new\n"


def test_parity_gate_detects_ignored_adapter():
    base = [None, -1.0, -2.0, -0.5, -3.0]
    shifted = [None, -0.6, -2.5, -0.2, -2.4]
    ok = parity_gate({"p": {"hf_base": base, "hf_adapter": shifted, "vllm_base": base, "vllm_adapter": shifted}})
    assert ok["passed"], ok
    ignored = parity_gate({"p": {"hf_base": base, "hf_adapter": shifted, "vllm_base": base, "vllm_adapter": base}})
    assert not ignored["passed"] and any("correlation" in p or "adapter mean" in p for p in ignored["problems"])
    far = parity_gate({"p": {"hf_base": base, "hf_adapter": base, "vllm_base": [None, 0, 0, 0, 0],
                             "vllm_adapter": [None, 0, 0, 0, 0]}})
    assert not far["passed"]


def test_labels_shift_mask_and_prefix_truncate():
    ids, labels = labels_for([10, 11, 12, 13, 14], [0, 0, 1, 1, 0], max_len=4)
    assert ids == [10, 11, 12, 13]
    assert labels == [-100, 12, 13]


def test_target_regex_excludes_vision():
    import re
    assert re.fullmatch(TARGET_REGEX, "model.language_model.layers.3.self_attn.q_proj")
    assert re.fullmatch(TARGET_REGEX, "model.layers.3.mlp.down_proj")
    assert not re.fullmatch(TARGET_REGEX, "model.vision_tower.encoder.layers.0.self_attn.q_proj")
