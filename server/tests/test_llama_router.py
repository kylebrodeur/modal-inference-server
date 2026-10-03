"""Contract tests for the llama-router preset builder.

The invariants that keep the runtime honest: total context is per-slot
times parallel slots (min 1), missing/unsharded manifests fail loudly,
tuning is REQUIRED (a silent 1-slot fallback would recreate the clamp
this runtime exists to avoid), and each member is pinned to its own GPU.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from pathlib import Path as _Path

import pytest

sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from llama_router import (
    CACHE_REUSE_CHUNK,
    _member_args,
    build_preset,
    gguf_path_for,
    total_context,
)


def _write_manifest(store: Path, alias: str, digests: list[str]) -> None:
    manifest_dir = store / "manifests/registry.ollama.ai/library" / alias
    manifest_dir.mkdir(parents=True, exist_ok=True)
    layers = [{"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{d}"} for d in digests]
    (manifest_dir / "latest").write_text(json.dumps({"layers": layers}))


def test_total_context_scales_by_parallel_min_one():
    assert total_context(4096, 4) == 16384
    assert total_context(4096, 0) == 4096  # clamp, no zero-pool


def test_gguf_path_resolves_single_layer_blob(tmp_path):
    _write_manifest(tmp_path, "hue", ["abc123"])
    blob = tmp_path / "blobs/sha256-abc123"
    blob.parent.mkdir()
    blob.write_bytes(b"G")
    assert gguf_path_for(tmp_path, "hue") == blob


def test_gguf_path_fails_loudly_on_missing_manifest_and_blob(tmp_path):
    with pytest.raises(FileNotFoundError, match="no ollama manifest"):
        gguf_path_for(tmp_path, "nothing-here")
    _write_manifest(tmp_path, "hue", ["gone"])
    with pytest.raises(FileNotFoundError, match="not on the volume"):
        gguf_path_for(tmp_path, "hue")


def test_gguf_path_rejects_multi_shard(tmp_path):
    _write_manifest(tmp_path, "sharded", ["a", "b"])
    with pytest.raises(RuntimeError, match="multi-shard"):
        gguf_path_for(tmp_path, "sharded")


def test_member_args_require_resolved_tuning():
    with pytest.raises(ValueError, match="no resolved tuning"):
        _member_args({"alias": "hue", "gguf": "/x.gguf"}, 0)
    with pytest.raises(ValueError, match="must resolve contextTokens"):
        _member_args({"alias": "hue", "gguf": "/x", "tuning": {"numParallel": 1}}, 0)


def test_member_args_maps_tuning_to_flags():
    args = _member_args(
        {
            "alias": "hue",
            "gguf": "/models/hue.gguf",
            "tuning": {
                "numParallel": 4,
                "contextTokens": 8192,
                "kvCacheType": "q8_0",
                "cacheReuse": True,
                "batch": 512,
                "ubatch": 256,
                "kvUnifiedPerSlot": True,
                "slotPromptSimilarity": 0.25,
                "swaCheckpoints": 8,
                "cacheIdleSlots": True,
            },
        },
        2,
    )
    assert args["model"] == "/models/hue.gguf"
    assert args["np"] == "4"
    assert args["c"] == "32768"  # 8192 * 4
    assert args["device"] == "CUDA2"
    assert args["cache-type-k"] == args["cache-type-v"] == "q8_0"
    assert args["cache-reuse"] == str(CACHE_REUSE_CHUNK)
    assert args["batch-size"] == "512"
    assert args["ubatch-size"] == "256"
    assert args["kv-unified-per-slot"] == "True"
    assert args["slot-prompt-similarity"] == "0.25"
    assert args["ctx-checkpoints"] == "8"
    assert args["cache-idle-slots"] == "true"


def test_build_preset_pins_members_to_distinct_gpus_and_preloads(tmp_path):
    member = {
        "alias": "hue",
        "tuning": {"numParallel": 2, "contextTokens": 4096},
    }
    other = dict(member, alias="hue-2")
    _write_manifest(tmp_path, "hue", ["aaa"])
    _write_manifest(tmp_path, "hue-2", ["bbb"])
    blobs = tmp_path / "blobs"
    blobs.mkdir(exist_ok=True)
    for d in ("aaa", "bbb"):
        (blobs / f"sha256-{d}").write_bytes(b"G")
    ini, pinned = build_preset({"members": [member, other], "preload": ["hue"]}, tmp_path, 2)
    assert pinned == {"hue": 0, "hue-2": 1}
    assert "device = CUDA0" in ini
    assert "device = CUDA1" in ini
    assert "load-on-startup = true" in ini  # ONLY for the preloaded one
    assert ini.count("load-on-startup = true") == 1


def test_build_preset_refuses_more_members_than_gpus(tmp_path):
    _write_manifest(tmp_path, "hue", ["aaa"])
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "sha256-aaa").write_bytes(b"G")
    member = {"alias": "hue", "tuning": {"numParallel": 1, "contextTokens": 2048}}
    with pytest.raises(RuntimeError, match="only 1 GPU"):
        build_preset({"members": [member, dict(member)]}, tmp_path, 1)
