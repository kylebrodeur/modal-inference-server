"""llama-server router-mode config, derived from the same catalog Ollama uses.

Why this exists: `ollama serve` decides `numParallel` per MODEL, not per
container, and silently clamps architectures on its own blocklist to a single
slot (server/sched.go). One container therefore cannot serve gemma-4-31b at
4 slots and a qwen35-architecture model at 4 slots with different contexts, and the
clamp is invisible in `/api/ps`.

`llama-server --models-preset` has no such limit: each preset section carries
its own command-line arguments, so per-model slots, context, KV type, and GPU
placement are all expressible. Ollama already vendors a llama-server build
(b11081 in ollama 0.34.4) that is NEWER than the upstream fix for the qwen35
parallel crash, so the engine can do this today — only the Go scheduler stops
it.

Blob reuse: Ollama's store is plain GGUF on disk. The manifest's
`application/vnd.ollama.image.model` layer names the blob, so the router serves
the exact bytes already verified and pinned in the volume — no second copy, no
re-download. Verified 2026-10-02: blob magic bytes are `GGUF`, and the digest
the router resolves matches the blob ollama itself loads.
"""

from __future__ import annotations

import json
from pathlib import Path

MODEL_LAYER = "application/vnd.ollama.image.model"
MANIFEST_ROOT = Path("manifests/registry.ollama.ai/library")

# llama.cpp's --cache-reuse takes a MINIMUM CHUNK SIZE in tokens, not a bool
# (Ollama's LLAMA_ARG_CACHE_REUSE translates its own bool onto this). Enabled
# reuse emits a sane chunk length; disabled emits llama's own default of 0.
CACHE_REUSE_CHUNK = 256


# -c is the TOTAL KV pool in llama.cpp; each slot gets -c/np. Ollama computes
# the total as per-slot-context * numParallel, so the router does the same to
# keep "contextTokens" meaning the same thing in both runtimes.
def total_context(context_tokens: int, num_parallel: int) -> int:
    return context_tokens * max(num_parallel, 1)


def gguf_path_for(store_root: Path, alias: str) -> Path:
    """Resolve an alias to the GGUF blob Ollama already downloaded.

    Fails loudly on a missing manifest, a missing blob, or a multi-shard /
    multimodal model: the router would otherwise load the wrong thing or a
    partial model and only surface it as a bad answer.
    """
    manifest = store_root / MANIFEST_ROOT / alias / "latest"
    if not manifest.exists():
        raise FileNotFoundError(f"no ollama manifest for {alias!r} at {manifest}; run bootstrap for this alias first")
    data = json.loads(manifest.read_text())
    layers = [layer for layer in data.get("layers", []) if layer.get("mediaType") == MODEL_LAYER]
    if not layers:
        raise RuntimeError(f"manifest for {alias!r} has no {MODEL_LAYER} layer")
    if len(layers) > 1:
        raise RuntimeError(
            f"{alias!r} is a multi-shard model ({len(layers)} model layers); "
            "the router preset expects a single GGUF file"
        )
    digest = str(layers[0]["digest"]).removeprefix("sha256:")
    blob = store_root / "blobs" / f"sha256-{digest}"
    if not blob.exists():
        raise FileNotFoundError(f"{alias!r} names blob {blob.name} but it is not on the volume")
    return blob


def _ini_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _member_args(member: dict[str, object], gpu_index: int) -> dict[str, str]:
    """Per-model llama-server args for one hot-set member.

    `member["tuning"]` is already the group's per-member effective block, merged
    by the catalog with the group defaults. It is required here rather than
    defaulted: llama-server cannot infer slots or context, and a silent
    fallback to 1 slot would reproduce exactly the clamp this runtime exists to
    avoid — so a member without resolved tuning is a hard error.
    """
    tuning = member.get("tuning")
    if not isinstance(tuning, dict) or not tuning:
        raise ValueError(f"member {member.get('alias')!r} has no resolved tuning; cannot build router args")
    num_parallel = tuning.get("numParallel")
    context_tokens = tuning.get("contextTokens")
    if num_parallel is None or context_tokens is None:
        raise ValueError(
            f"member {member.get('alias')!r} tuning must resolve contextTokens and numParallel (got {tuning!r})"
        )
    num_parallel = int(num_parallel)
    context_tokens = int(context_tokens)

    args: dict[str, str] = {
        "model": str(member["gguf"]),
        # All layers on GPU; the preset pins which one.
        "n-gpu-layers": "999",
        "device": f"CUDA{gpu_index}",
        "main-gpu": str(gpu_index),
        "np": str(num_parallel),
        "c": str(total_context(context_tokens, num_parallel)),
        "flash-attn": "on",
        # Chat templates come from the GGUF metadata, same as Ollama's default.
        "jinja": "true",
    }
    kv_type = tuning.get("kvCacheType")
    if isinstance(kv_type, str) and kv_type:
        args["cache-type-k"] = kv_type
        args["cache-type-v"] = kv_type
    if tuning.get("cacheReuse"):
        args["cache-reuse"] = str(CACHE_REUSE_CHUNK)
    # Batch size is a real prefill lever on this runtime (the ollama lane
    # hardcodes -b/-ub on its own cmdline, so the keys are inert there).
    # Omitted = llama.cpp's own default.
    if tuning.get("batch"):
        args["batch-size"] = str(int(tuning["batch"]))
    if tuning.get("ubatch"):
        args["ubatch-size"] = str(int(tuning["ubatch"]))
    if tuning.get("kvUnifiedPerSlot"):
        args["kv-unified-per-slot"] = str(tuning["kvUnifiedPerSlot"])
    if tuning.get("slotPromptSimilarity") is not None:
        args["slot-prompt-similarity"] = str(tuning["slotPromptSimilarity"])
    if tuning.get("swaCheckpoints"):
        args["ctx-checkpoints"] = str(tuning["swaCheckpoints"])
    if tuning.get("cacheIdleSlots"):
        args["cache-idle-slots"] = "true"
    return args


def build_preset(
    target: dict[str, object],
    store_root: Path,
    gpu_count: int,
) -> tuple[str, dict[str, int]]:
    """Render the router preset INI for a serve target.

    Returns the INI text and a {alias: gpu index} map recording where each
    member was pinned. Members are pinned to distinct GPUs round-robin:
    llama-server spawns one child process per model, and unpinned children
    would all contend for CUDA0 and evict each other — the exact failure
    co-residency exists to prevent.
    """
    members = target.get("members") or []
    if not isinstance(members, list) or not members:
        raise ValueError("serve target has no members to build a router preset from")

    preload = {str(alias) for alias in (target.get("preload") or [])}
    lines = [
        "; Generated from models.json by llama_router.py — do not edit by hand.",
        "version = 1",
        "",
        "; Applied to every model unless the model's own section overrides it.",
        "[*]",
        "host = 0.0.0.0",
        "port = 8000",
        "no-webui = true",
        f"models-max = {len(members)}",
        "",
    ]
    resolved: dict[str, int] = {}
    for index, raw_member in enumerate(members):
        member = dict(raw_member)
        alias = str(member["alias"])
        if index >= gpu_count:
            raise RuntimeError(
                f"serve target has {len(members)} members but only {gpu_count} GPU(s); "
                "each member is pinned to its own GPU for the router prototype"
            )
        member["gguf"] = str(gguf_path_for(store_root, alias))
        lines.append(f"[{alias}]")
        # load-on-startup is preset-only (not a llama-server flag): it makes
        # `ready` mean the whole hot set is resident, matching the Ollama
        # runtime's preload contract.
        if alias in preload:
            lines.append("load-on-startup = true")
        for key, value in _member_args(member, index).items():
            lines.append(f"{key} = {_ini_value(value)}")
        lines.append("")
        resolved[alias] = index
    return "\n".join(lines), resolved
