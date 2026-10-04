"""Generic OpenAI-compatible Modal App serving model profiles from models.json.

The app is a thin runner: operators choose an enabled alias, bootstrap its
pinned weights into a shared Volume (scoped per alias), and deploy exactly one
profile per App instance. Clients consume it as an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import warnings
from pathlib import Path
from typing import ClassVar

import modal
from fastapi import Request

import modal_inference_catalog as _cat

_runtime = _cat._runtime
_load_profile = _cat._load_profile
_profile_runtime = _cat._profile_runtime
_gpu_spec = _cat._gpu_spec
_model_dir = _cat._model_dir
resolve_profile = _cat.resolve_profile
resolve_serve_target = _cat.resolve_serve_target
_tuning_profile = _cat._tuning_profile
_runtime_tuning_override = _cat._runtime_tuning_override
_runtime_target_tuning = _cat._runtime_target_tuning

APP_NAME = os.getenv("APP_NAME", "modal-inference-server")
CATALOG_PATH = Path(os.getenv("MODEL_CATALOG_PATH", Path(__file__).with_name("models.json")))
MODEL_DIR = "/models"
ENGINE_CACHE_DIR = "/engine-cache"
USAGE_DIR = "/usage"
OLLAMA_LOG_FILE = os.getenv("OLLAMA_LOG_FILE", "/tmp/ollama-serve.log")
# llama's load line, the only place the real slot count is visible:
#   srv    load_model: initializing, n_slots = 4, n_ctx_slot = 65536, ...
_N_SLOTS_RE = re.compile(r"n_slots\s*=\s*(\d+)")
USAGE_LEDGER_DIR = f"{USAGE_DIR}/events"
USAGE_VOLUME_NAME = "modal-inference-server-usage"
VLLM_VERSION = os.getenv("VLLM_VERSION", "0.20.0")
HF_SECRET_NAME = os.getenv("HF_SECRET_NAME", "modal-inference-server-huggingface")
MAX_NUM_SEQS = int(os.getenv("MAX_NUM_SEQS", "8"))
MAX_CONTAINERS = int(os.getenv("MAX_CONTAINERS", "1"))
MIN_CONTAINERS = int(os.getenv("MIN_CONTAINERS", "0"))
SCALEDOWN_WINDOW = int(os.getenv("SCALEDOWN_WINDOW", "300"))
DEPLOYED_PROFILE = os.getenv("MODEL_PROFILE", "").strip()
PROXY_AUTH_ENFORCED = os.getenv("MODAL_INFERENCE_ENFORCE_PROXY_AUTH", "0") == "1"
# Multi-container knobs. max_inputs is the per-container input budget Modal's
# router respects before queueing or (when max_containers > 1) scaling out;
# keep headroom over the llama slot count for health/metrics probes.
# target_inputs, when set, is the autoscaler's per-container concurrency aim
# (scale-up pressure). Both default to current behavior until tuned for bursts.
MAX_INPUTS_PER_CONTAINER = int(os.getenv("MAX_INPUTS_PER_CONTAINER", str(MAX_NUM_SEQS)))
_TARGET_INPUTS = os.getenv("TARGET_INPUTS_PER_CONTAINER", "").strip()
TARGET_INPUTS_PER_CONTAINER = int(_TARGET_INPUTS) if _TARGET_INPUTS.isdigit() else None
 # Parked-request wait before a clean 429. Kept below typical client idle timeouts
 # so a client that cannot
# get a slot receives a retryable 429 while still connected, instead of a
# silent connection kill that surfaces as "Request timed out."
GATE_WAIT_SECONDS = float(os.getenv("GATE_WAIT_SECONDS", "240"))
# Ollama/llama-server withholds response HEADERS until prompt processing
# finishes (measured 2026-10-02: headers and first body byte at the same
# instant, 9.9s for a 100K-char prompt). A streaming client therefore sees
# total silence through prefill — the cause of "zombie run / Request timed
# out" on large contexts. The proxy answers streaming requests immediately
# with SSE keepalive comments: if upstream headers arrive within this grace
# window the real status/body pass through unchanged (fast path); beyond it,
# the client gets a 200 stream padded every 10s until upstream is ready.
EARLY_HEADERS_SECONDS = float(os.getenv("EARLY_HEADERS_SECONDS", "15"))
# Hard ceiling while waiting for withheld upstream headers in the slow path.
HEADERS_DEADLINE_SECONDS = float(os.getenv("HEADERS_DEADLINE_SECONDS", "600"))


class _GateTelemetry:
    """Proxy slot-gate counters, read by the heartbeat thread each cycle.

    Counters are PER ALIAS. A serve group co-hosts several models in one
    container, and each gets its own gate: a request for an idle model must
    not sit parked behind a saturated one. `*` collects requests naming an
    alias the target does not serve (they share a fallback gate).

    waiting: requests parked on the gate — silent to their clients until a
        slot frees, then 429 after gate_wait_seconds.
    active: requests holding a slot (prefill/decode inside llama).
    slots: gate capacity for that alias (the target's numParallel).
    """

    waiting: ClassVar[dict[str, int]] = {}
    active: ClassVar[dict[str, int]] = {}
    slots: ClassVar[dict[str, int]] = {}

    @classmethod
    def snapshot(cls) -> dict[str, object]:
        """Aggregate triple (all aliases) plus the per-alias breakdown."""
        aliases = sorted(set(cls.slots) | set(cls.waiting) | set(cls.active))
        return {
            "aggregate": {
                "waiting": sum(cls.waiting.values()),
                "active": sum(cls.active.values()),
                "slots": sum(cls.slots.values()),
            },
            "per_alias": {
                alias: {
                    "waiting": cls.waiting.get(alias, 0),
                    "active": cls.active.get(alias, 0),
                    "slots": cls.slots.get(alias, 0),
                }
                for alias in aliases
            },
        }


class _SlotGates:
    """One slot semaphore per served alias, sized to what llama ACTUALLY gave it.

    Ollama sizes llama's slot pool by OLLAMA_NUM_PARALLEL, but it decides per
    MODEL, not per container: architectures it cannot parallelize are silently
    forced to a single slot. Observed live on the `local-hot` group — the same
    container launched gemma-4-31b with `-np 4` and a second model (architecture
    qwen35) with `-np 1`, after logging `model architecture does not currently
    support parallel requests`. Ollama exposes no API for this, so the gate is
    sized from the `n_slots` line each load writes to the engine log.

    Sizing from the container's declared numParallel instead would admit four
    concurrent requests to a one-slot model: the extras then queue INSIDE
    llama, invisible to the client, which is exactly the silent-wait failure
    the gate exists to prevent. A group must also not share its *queue*:
    gemma's busy slots should not park a request for a different model.
    """

    def __init__(self, capacities: dict[str, int], wait_seconds: float) -> None:
        self._wait_seconds = wait_seconds
        self._gates = {alias: asyncio.Semaphore(count) for alias, count in capacities.items()}
        self._fallback = asyncio.Semaphore(MAX_NUM_SEQS)
        for alias, count in capacities.items():
            _gate.slots[alias] = count

    def _key(self, alias: str) -> str:
        return alias if alias in self._gates else "*"

    def _gate_for(self, alias: str) -> asyncio.Semaphore:
        return self._gates.get(alias, self._fallback)

    async def acquire(self, alias: str) -> None:
        """Acquire a slot for `alias`, tracking the silent waiting window."""
        gate = self._gate_for(alias)
        key = self._key(alias)
        if gate.locked():
            _gate.waiting[key] = _gate.waiting.get(key, 0) + 1
            try:
                async with asyncio.timeout(self._wait_seconds):
                    await gate.acquire()
            finally:
                _gate.waiting[key] = max(0, _gate.waiting.get(key, 0) - 1)
        else:
            async with asyncio.timeout(self._wait_seconds):
                await gate.acquire()
        _gate.active[key] = _gate.active.get(key, 0) + 1

    def release(self, alias: str) -> None:
        key = self._key(alias)
        _gate.active[key] = max(0, _gate.active.get(key, 0) - 1)
        self._gate_for(alias).release()


_gate = _GateTelemetry()
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

CUDA_BASE = "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04"
# 0.35.1 is the current stable (2026-10-02). It vendors the same llama.cpp as
# master (LLAMA_CPP_VERSION=b11232) and its arch table is IDENTICAL to 0.34.4's
# (b11081) — so this bump does NOT change which models can load (notably: still
# no glm5-next; see RUNBOOK 9b). What it adds is the /v1/systemone decision-model
# surface (Jev-compatible choice/noul/score) plus API fixes.
OLLAMA_IMAGE = os.getenv("OLLAMA_IMAGE", "ollama/ollama:0.35.1")
OLLAMA_MODELS_DIR = f"{MODEL_DIR}/ollama"  # one shared ollama store; tags are the per-model namespace
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_CACHE_ENV = {
    "HF_HUB_ENABLE_HF_TRANSFER": "1",
    "VLLM_USE_DEEP_GEMM": "0",
    "VLLM_CACHE_ROOT": "/engine-cache/vllm",
    "XDG_CACHE_HOME": "/engine-cache/xdg",
    "TORCHINDUCTOR_CACHE_DIR": "/engine-cache/torchinductor",
    "CUDA_HOME": "/usr/local/cuda",
    "TRITON_CACHE_DIR": "/engine-cache/triton",
    # FlashInfer JIT builds its kernels under ~/.cache/flashinfer by default,
    # which dies with the container. Persist on the Volume so future cold
    # boots skip the nvcc JIT (observed: ~10 min of nvcc on first boot).
    "FLASHINFER_WORKSPACE_BASE": "/engine-cache/flashinfer",
    # TileLang compiles its attention/indexer kernels at engine init (observed
    # on DeepSeek-V4.1: a long TileLang lowering + CUDA codegen phase after the
    # weights finish loading). Default is ~/.tilelang/cache, which dies with the
    # container, so every cold boot would re-pay it.
    "TILELANG_CACHE_DIR": "/engine-cache/tilelang",
}


def _usage_event_path() -> Path:
    Path(USAGE_LEDGER_DIR).mkdir(parents=True, exist_ok=True)
    return Path(USAGE_LEDGER_DIR) / f"{os.getenv('MODAL_TASK_ID', 'container')}-{os.getpid()}.jsonl"


_commit_lock = threading.Lock()
_last_commit_at = 0.0


def _volume_commit_soon() -> None:
    """Publish the ledger to the shared Volume without blocking the caller.

    Runs in a daemon thread; debounced (max 4/s per process). The event loop
    NEVER waits on a Volume commit — a blocking commit() inside the ASGI loop
    froze the proxy under concurrent agent bursts (all requests stalled for
    minutes while commits churned).
    """
    global _last_commit_at

    def _commit():
        with _commit_lock:
            if time.time() - _last_commit_at < 0.25:
                time.sleep(0.25)
            with contextlib.suppress(Exception), warnings.catch_warnings():
                # Deliberate sync commit in a worker thread: the loop-blocking
                # freeze this replaces WAS the bug (froze the proxy under
                # fleet bursts). Modal's AsyncUsageWarning fires on any sync
                # call it instruments, not just event-loop ones; silence it.
                warnings.filterwarnings("ignore", message=".*blocking call.*", module=type(usage_volume).__module__)
                usage_volume.commit()
            globals()["_last_commit_at"] = time.time()

    threading.Thread(target=_commit, daemon=True).start()


def _usage_event(event: dict[str, object]) -> None:
    """Append one redacted request record and publish it to the shared Volume."""
    event["recorded_at"] = time.time()
    event["container_id"] = os.getenv("MODAL_TASK_ID", "unknown")
    if _RESOLVED:
        event.setdefault("deployment", APP_NAME)
        event.setdefault("model", _primary_alias())
        event.setdefault("runtime", str(_RESOLVED["runtime"]))
        event.setdefault("tuning_profile", str(_RESOLVED.get("tuning_name", "baseline")))
        members = _RESOLVED.get("members")
        if isinstance(members, list) and len(members) > 1:
            # Co-resident target: record the hot set so the ledger can attribute
            # a request to the group, not just its first member.
            event.setdefault("serve_group", str(_RESOLVED.get("name", "")))
            event.setdefault("serve_members", [str(m["alias"]) for m in members])  # type: ignore[index]
    path = _usage_event_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    _volume_commit_soon()


def _log_llama_flags() -> None:
    """Boot-log the llama-server cache/eviction flag surface (Phase D Spec 3 aid).

    Enumerates what the pinned image's llama-server ACTUALLY supports before
    any KV-eviction knob gets flexed; runs once per GPU container start.
    """
    llama = "/usr/lib/ollama/llama-server"
    if not Path(llama).exists():
        llama = "/usr/local/bin/llama-server"
    if not Path(llama).exists():
        return
    try:
        out = subprocess.run([llama, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return
    keep = [
        line
        for line in (out.stdout + out.stderr).splitlines()
        if any(
            key in line
            for key in (
                "kv",
                "cache",
                "slot",
                "shift",
                "window",
                "swa",
                "evict",
                "keep",
                "defrag",
                "reuse",
                "cont-batch",
                "parallel",
                "ctx",
            )
        )
    ]
    if keep:
        print("llama-server cache/eviction flag surface:")
        for line in keep:
            print("  " + line)


def _register_gpu_container() -> None:
    """Append this container's task id to a boot-time registry on the usage Volume.

    `inference shutdown` reads it to know which live containers are GPU workers
    (the ledger only records on requests, so idle containers are invisible).
    """
    path = Path(USAGE_DIR) / "gpu-containers.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "container_id": os.getenv("MODAL_TASK_ID", "unknown"),
                    "registered_at": time.time(),
                    "alias": DEPLOYED_PROFILE,
                }
            )
            + "\n"
        )


def _read_llama_log_tail(max_lines: int = 4000) -> list[str]:
    """Tail the tee'd ollama log for slot parsing (best-effort, never raises)."""
    try:
        with Path(OLLAMA_LOG_FILE).open(encoding="utf-8", errors="replace") as handle:
            return handle.readlines()[-max_lines:]
    except OSError:
        return []


def _write_serving_state(ok: bool, model_loaded: bool, detail: str = "") -> None:
    """Publish this GPU container's serving truth to the usage Volume.

    The dashboard reads this file; `heartbeat` staleness is what makes the
    status honest — hard-killed containers (OOM, gpu_stop) never run
    @modal.exit(), so a heartbeat age cutoff is the only liveness signal.
    Slot state (per-slot phase/rate from llama's log) rides along so the
    dashboard can show what each slot is doing without container exec.
    """
    path = Path(USAGE_DIR) / f"serving-state-{os.getenv('MODAL_TASK_ID', 'unknown')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "ok": ok,
        "model_loaded": model_loaded,
        "detail": detail,
        "alias": DEPLOYED_PROFILE,
        "container_id": os.getenv("MODAL_TASK_ID", "unknown"),
        "heartbeat": time.time(),
    }
    if ok and model_loaded and _RESOLVED is not None and str(_RESOLVED["runtime"]) == "ollama":
        # The `llama` runtime needs no log scraping: llama-server exposes
        # /slots?model=<name> directly (see below), so only the Ollama path
        # (which hides slots entirely) parses the tee.
        with contextlib.suppress(Exception):
            import modal_inference_slots

            state["slots"] = modal_inference_slots.summarize(_read_llama_log_tail())
    if ok and model_loaded and _RESOLVED is not None and str(_RESOLVED["runtime"]) == "llama":
        # llama-server's own view: one entry per slot with its live phase and
        # decode rate, read straight from the engine instead of a log tee.
        with contextlib.suppress(Exception):
            import httpx

            per_model: dict[str, object] = {}
            for alias in _member_aliases():
                slots = httpx.get("http://127.0.0.1:8000/slots", params={"model": alias}, timeout=10).json()
                per_model[alias] = {
                    "slots": [
                        {
                            "id": s.get("id"),
                            "state": "busy" if (s.get("is_processing") or s.get("n_past")) else "idle",
                            "n_ctx": s.get("n_ctx"),
                            "next_token": (s.get("next_token") or {}).get("n_decoded"),
                        }
                        for s in slots
                    ]
                }
            state["router_slots"] = per_model
    if any(_gate.slots.values()):
        # Proxy slot-gate counters: waiting = requests parked silently before a
        # slot frees (the "zombie" window), active = requests inside llama now.
        # Top-level triple stays aggregate for older dashboard readers;
        # per_alias is the truth for a co-resident serve group.
        snapshot = _gate.snapshot()
        state["gate"] = snapshot["aggregate"]
        state["gate_aliases"] = snapshot["per_alias"]
    if _RESOLVED is not None:
        state["members"] = [str(member["alias"]) for member in _RESOLVED.get("members", [])]
    path.write_text(json.dumps(state))
    with contextlib.suppress(Exception):
        usage_volume.commit()


def _serve_with_heartbeat(process: subprocess.Popen[str], healthy_url: str, timeout: int, runtime: str) -> None:
    """Wait for /health while publishing a serving heartbeat until the process dies.

    Keeps publishing after health passes so a scale-to-zero idle container
    still reports `serving`; stops as soon as the subprocess exits.
    """

    def heartbeat() -> None:
        while process.poll() is None:
            _write_serving_state(True, True)
            time.sleep(30)
        _write_serving_state(False, False, "server process exited")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    _wait_healthy(process, healthy_url, timeout, runtime)


def _usage_from_json(payload: object) -> dict[str, int]:
    """Normalize OpenAI and Ollama usage shapes, including streamed SSE chunks."""
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = payload
    result: dict[str, int] = {}
    for source, target in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("prompt_eval_count", "prompt_tokens"),
        ("eval_count", "completion_tokens"),
    ):
        value = usage.get(source)
        if isinstance(value, int):
            result[target] = value
    # Cached-prefix share: the key metric for prompt-cache economics
    # (fleet sessions reuse 47-57K system/tool prefixes; the cached fraction
    # is what prefix-cache tuning actually moves).
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        result["cached_tokens"] = details["cached_tokens"]
    if "total_tokens" not in result and "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def _usage_from_stream(raw: bytes) -> dict[str, int]:
    """Extract the final usage-bearing JSON object from an SSE response."""
    usage: dict[str, int] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        with contextlib.suppress(json.JSONDecodeError):
            candidate = _usage_from_json(json.loads(body))
            if candidate:
                usage.update(candidate)
    return usage


MODULE_DIR = Path(__file__).parent
_RUNTIME_MODULES = (
    "modal_inference_catalog.py",
    "modal_inference_cost_model.py",
    "modal_inference_dashboard.py",
    "modal_inference_slots.py",
    "llama_router.py",
)


def _bake_modules(image: modal.Image, container_dir: str = "/root") -> modal.Image:
    for name in _RUNTIME_MODULES:
        image = image.add_local_file(MODULE_DIR / name, f"{container_dir}/{name}")
    return image


# llama.cpp's router mode requires the engine to be built with LLAMA_SUBPROCESS
# (common/subproc.cpp: is_supported() is a compile-time #ifdef, and
# server-models.cpp throws "subprocess is not enabled on this build" without
# it). Ollama's vendored binary is NOT built with that flag, so its router is
# absent. Upstream enables it by default on Linux, so we fetch the official
# prebuilt CUDA binary rather than compiling.
LLAMA_CPP_BUILD = os.getenv("LLAMA_CPP_BUILD", "b11349")
LLAMA_CPP_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_BUILD}"
    f"/llama-{LLAMA_CPP_BUILD}-bin-ubuntu-cuda-12.8-x64.tar.gz"
)


def _serve_image(alias: str) -> modal.image._Image:
    """Build the serve image for one serve target (alias or group name).

    Runtime comes from `resolve_serve_target`, not from the member profile:
    a group may legitimately override an `ollama` member to run on the `llama`
    runtime (that is how the two engines can be A/B'd on the same weights), and
    reading the profile would pick the wrong image and boot without fastapi.
    """
    runtime = "ollama"
    vllm_image = ""
    if alias:
        runtime = str(resolve_serve_target(alias).get("runtime") or "ollama")
        pinned_by = _cat._load_group(alias) or _load_profile(alias)
        members = pinned_by.get("aliases") if isinstance(pinned_by, dict) else None
        source = _load_profile(str(members[0])) if isinstance(members, list) and members else pinned_by
        pinned = source.get("vllmImage") if isinstance(source, dict) else None
        vllm_image = pinned if isinstance(pinned, str) else ""
    common = {"MODEL_PROFILE": alias, **_CACHE_ENV}
    # The `llama` runtime runs llama-server in router mode, using the binary
    # ollama already vendors in this image. Same image, same model store, same
    # bootstrap — only the boot command differs.
    if runtime in ("ollama", "llama"):
        image = (
            modal.Image.from_registry(OLLAMA_IMAGE, add_python="3.11")
            .entrypoint([])
            .pip_install("httpx==0.28.1", "fastapi==0.133.0", "huggingface_hub[hf_transfer]")
        )
        if runtime == "llama":
            # Router mode needs the LLAMA_SUBPROCESS build (see above). Unpack
            # the official CUDA binary next to ollama's so both engines are
            # available and the router's argv is stable. The archive is FLAT
            # (binaries and .so files in one dir, resolved via $ORIGIN rpath),
            # so it is unpacked whole rather than split into bin/ and lib/.
            # The base has no curl or wget, and upstream links libgomp at
            # runtime; both are installed before unpacking.
            image = image.apt_install("curl", "ca-certificates", "libgomp1").run_commands(
                f"set -e; cd /tmp && curl -fsSL -o llama.tar.gz {LLAMA_CPP_URL} "
                "&& mkdir -p /opt/llama.cpp "
                "&& tar -xzf llama.tar.gz -C /opt/llama.cpp --strip-components=1 "
                "&& rm -f llama.tar.gz "
                "&& test -x /opt/llama.cpp/llama-server "
                "&& LD_LIBRARY_PATH=/opt/llama.cpp /opt/llama.cpp/llama-server --version"
            )
        return _bake_modules(image.env(common).add_local_file(CATALOG_PATH, "/root/models.json"))
    if vllm_image.strip():
        # fastapi is required: modal_service.py imports Request at module level and
        # this branch boots the app's own web wrapper. The ollama/llama branch
        # installs it; omitting it here fails the whole runner with
        # ModuleNotFoundError before the engine ever starts.
        return _bake_modules(
            modal.Image.from_registry(vllm_image, add_python="3.11")
            .entrypoint([])
            .pip_install("httpx==0.28.1", "fastapi==0.133.0")
            .env(common)
            .add_local_file(CATALOG_PATH, "/root/models.json")
        )
    return _bake_modules(
        modal.Image.from_registry(CUDA_BASE, add_python="3.11")
        .pip_install(f"vllm=={VLLM_VERSION}", "httpx==0.28.1", "mistral_common>=1.11.0", "huggingface_hub[hf_transfer]")
        .env(common)
        .add_local_file(CATALOG_PATH, "/root/models.json")
    )


# The deployed alias pins everything about the serve side at deploy time.
_SERVE_IMAGE = _serve_image(DEPLOYED_PROFILE) if DEPLOYED_PROFILE else None
app = modal.App(APP_NAME, image=_SERVE_IMAGE, tags={"project": APP_NAME})
model_volume = modal.Volume.from_name(
    os.getenv("MODEL_VOLUME_NAME", "modal-inference-server-models"), create_if_missing=True
)
engine_volume = modal.Volume.from_name(
    os.getenv("ENGINE_CACHE_VOLUME_NAME", "modal-inference-server-engine-cache"), create_if_missing=True
)
hf_secret = modal.Secret.from_name(HF_SECRET_NAME)
dashboard_secret = modal.Secret.from_name("modal-inference-server-dashboard")
usage_volume = modal.Volume.from_name(USAGE_VOLUME_NAME, create_if_missing=True)


def _ollama_env(max_len: int, models_dir: str = OLLAMA_MODELS_DIR) -> dict[str, str]:
    """Ollama's own knobs plus LLAMA_ARG_* passthrough to llama-server.

    Tuning comes from the SERVE TARGET, not the member profile: ollama's
    context/parallel/batch env is container-global, so every co-resident member
    is served with the target's numbers. For a single-alias target those are
    that alias's own active tuning, so nothing changes.
    """
    gpu_count = int(_RESOLVED["gpu_count"]) if _RESOLVED else 1
    target_tuning = _RESOLVED.get("tuning") if _RESOLVED else None
    tuning: dict[str, object] = dict(target_tuning) if isinstance(target_tuning, dict) else {}
    if not tuning and _RESOLVED:
        # Defensive: a target without a tuning block (should not happen) falls
        # back to the first member's active profile.
        members = _RESOLVED.get("members")
        if isinstance(members, list) and members:
            _active, tuning = _tuning_profile(members[0]["profile"])  # type: ignore[index]
    env = {
        **os.environ,
        "OLLAMA_HOST": "0.0.0.0:8000",
        "OLLAMA_MODELS": models_dir,
        "OLLAMA_KEEP_ALIVE": "-1",
        "OLLAMA_CONTEXT_LENGTH": str(tuning.get("contextTokens", max_len)),
        "OLLAMA_NUM_PARALLEL": str(tuning.get("numParallel", MAX_NUM_SEQS)),
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_LOAD_TIMEOUT": "45m",
        "LLAMA_ARG_BATCH": str(tuning.get("batch", os.getenv("LLAMA_BATCH", "2048"))),
        "LLAMA_ARG_UBATCH": str(tuning.get("ubatch", os.getenv("LLAMA_UBATCH", "2048"))),
        "LLAMA_ARG_CACHE_TYPE_K": str(tuning.get("kvCacheType", os.getenv("LLAMA_KV_TYPE", "q8_0"))),
        "LLAMA_ARG_CACHE_TYPE_V": str(tuning.get("kvCacheType", os.getenv("LLAMA_KV_TYPE", "q8_0"))),
        "LLAMA_ARG_CACHE_REUSE": "1" if tuning.get("cacheReuse", True) else "0",
    }
    # KV-eviction experiment knobs (Phase D Spec 3): optional tuning keys,
    # absent = llama-server defaults (behavior unchanged).
    if tuning.get("kvUnifiedPerSlot"):
        env["LLAMA_ARG_KV_UNIFIED_PER_SLOT"] = str(tuning["kvUnifiedPerSlot"])
    if tuning.get("slotPromptSimilarity") is not None:
        env["LLAMA_ARG_SLOT_PROMPT_SIMILARITY"] = str(tuning["slotPromptSimilarity"])
    if tuning.get("swaCheckpoints"):
        env["LLAMA_ARG_SWA_CHECKPOINTS"] = str(tuning["swaCheckpoints"])
    if tuning.get("cacheIdleSlots") is not None:
        env["LLAMA_ARG_CACHE_IDLE_SLOTS"] = "1" if tuning["cacheIdleSlots"] else "0"
    if gpu_count > 1 and os.getenv("LLAMA_SPLIT_MODE"):
        env["LLAMA_ARG_SPLIT_MODE"] = os.environ["LLAMA_SPLIT_MODE"]
    return env


def _ollama_serve_store() -> str:
    """Local symlinked view over the Volume's Ollama store."""
    local = Path("/root/ollama-store")
    shutil.rmtree(local, ignore_errors=True)
    (local / "blobs").mkdir(parents=True)
    for blob in (Path(OLLAMA_MODELS_DIR) / "blobs").iterdir():
        if blob.is_file():
            (local / "blobs" / blob.name).symlink_to(blob)
    shutil.copytree(Path(OLLAMA_MODELS_DIR) / "manifests", local / "manifests")
    return str(local)


def _wait_healthy(process: subprocess.Popen[str], url: str, timeout: int, name: str) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited during startup with status {process.returncode}")
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise TimeoutError(f"{name} did not become healthy within {timeout}s")


def _ollama_digest(tag: str) -> str:
    import httpx

    for entry in httpx.get("http://127.0.0.1:8000/api/tags", timeout=30).json().get("models", []):
        name = str(entry.get("name", ""))
        if name == tag or name == f"{tag}:latest":
            return str(entry["digest"])
    raise RuntimeError(f"ollama store has no manifest for {tag!r}")


_HF_TAG_RE = re.compile(r"^hf\.co/(?P<repo>[^:]+/[^:]+):(?P<quant>.+)$")


def _import_hf_gguf(alias: str, repo: str, quant: str, model_dir: str, env: dict[str, str]) -> None:
    """Ollama refuses sharded GGUF via `pull`; download the quant folder and `ollama create` from shard 1.

    `create` copies the shards into the blob store, so the download is removed afterwards.

    No `revision=` here on purpose. For an ollama-runtime profile the catalog's
    `revision` is the OLLAMA MANIFEST DIGEST (verified: each alias's value
    equals the sha256 of its own manifest), because
    that is what `_bootstrap_ollama` compares against and what makes an ollama
    import self-verifying. A manifest digest is NOT an HF revision and does not
    resolve on the Hub, so passing it to snapshot_download breaks bootstrap for
    every existing ollama model. HF commit shas belong to vllm-runtime profiles,
    which pin via the snapshot_download branch in bootstrap_model instead.
    """
    from huggingface_hub import snapshot_download

    os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
    staging = Path(model_dir) / "hf-staging"
    snapshot_download(
        repo_id=repo,
        allow_patterns=[f"{quant}/*", f"*{quant}*.gguf"],
        local_dir=staging,
    )
    shards = sorted(staging.rglob("*.gguf"))
    if not shards:
        raise RuntimeError(f"no GGUF files found for {repo}:{quant}")
    model_volume.commit()  # keep the 186 GB staging if create fails; the rerun then skips the download
    modelfile = staging / "Modelfile"
    # FROM <dir> makes the CLI glob every *.gguf shard (FROM <shard1> sends one file and create rejects it).
    # The Volume mount is a symlink; the CLI resolves files but not the dir, so pass the real path.
    modelfile.write_text(f"FROM {shards[0].parent.resolve()}\n")
    subprocess.run(["ollama", "create", alias, "-f", str(modelfile)], env=env, check=True, text=True)
    shutil.rmtree(staging)


def _bootstrap_ollama(alias: str, tag: str, pinned: str, marker: Path, model_dir: str) -> None:
    """Populate the shared Volume store under the alias and record the manifest digest.

    Registry tags are pulled then `ollama cp`'d to the alias (manifest-only,
    blobs shared). hf.co/<repo>:<quant> tags are imported via `ollama create`
    because the registry path rejects sharded GGUF.
    """
    env = _ollama_env(4096)
    server = subprocess.Popen(["ollama", "serve"], env=env, text=True, start_new_session=True)
    try:
        _wait_healthy(server, "http://127.0.0.1:8000/", 120, "ollama")
        hf_tag = _HF_TAG_RE.match(tag)
        if hf_tag:
            _import_hf_gguf(alias, hf_tag["repo"], hf_tag["quant"], model_dir, env)
        else:
            subprocess.run(["ollama", "pull", tag], env=env, check=True, text=True)
            if tag != alias:
                subprocess.run(["ollama", "cp", tag, alias], env=env, check=True, text=True)
        digest = _ollama_digest(alias)
        if pinned and digest != pinned:
            raise RuntimeError(f"imported {tag}@{digest} but profile {alias!r} pins {pinned}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{tag}@{digest}\n")
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=30)


@app.function(
    volumes={MODEL_DIR: model_volume},
    secrets=[hf_secret],
    timeout=3 * 3600,  # 400+ GB models: download + sha256 + blob copy
    ephemeral_disk=1_000_000,  # MiB; xet stages chunks on local disk and the default ran out at ~430 GB
)
def bootstrap_model(alias: str) -> None:
    """Download exactly one pinned model into the Volume and commit it.

    Each profile writes a marker under /models/<alias>/ so many models coexist
    in one Volume with per-model locks. vLLM profiles snapshot a pinned HF
    revision into that directory; ollama profiles pull into the shared ollama
    store and pin the manifest digest. Fails if that alias's Volume state
    holds a different revision.
    """
    resolved = resolve_profile(alias)
    model_id = str(resolved["model"])
    revision = str(resolved["revision"])
    model_dir = _model_dir(alias)
    marker = Path(model_dir) / ".model-revision"
    existing = marker.read_text().strip() if marker.exists() else ""
    if existing and revision and existing != f"{model_id}@{revision}":
        raise RuntimeError(
            f"model Volume contains {existing} for {model_dir}, refusing to overwrite with {model_id}@{revision}"
        )

    lock = Path(MODEL_DIR) / f".{alias}.bootstrap.lock"
    if lock.exists():
        if lock.is_dir() and not any(lock.iterdir()):
            # Empty lock dir means the previous run died before its finally block.
            shutil.rmtree(lock)
        else:
            raise RuntimeError(f"another bootstrap is already running for {alias}")
    try:
        lock.mkdir()
        if resolved["runtime"] in ("ollama", "llama"):
            _bootstrap_ollama(alias, model_id, revision, marker, model_dir)
        else:
            from huggingface_hub import snapshot_download

            os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
            snapshot_download(repo_id=model_id, revision=revision, local_dir=model_dir)
            marker.write_text(f"{model_id}@{revision}\n")
        model_volume.commit()
    finally:
        lock.rmdir()


# Deploy-time resolution: one App instance serves one target — a single alias,
# or a serve group (a hot set of aliases co-hosted in one container so they
# never evict each other).
_RESOLVED: dict[str, object] | None = resolve_serve_target(DEPLOYED_PROFILE) if DEPLOYED_PROFILE else None

# Slots llama actually granted each member at boot, measured from the load log.
# Empty until preload runs, or when measurement was impossible (log rotated).
# Sizing a gate from the declared numParallel instead would over-admit for
# architectures Ollama silently clamps to one slot. See _measure_member_slots.
_MEMBER_SLOTS: dict[str, int] = {}


def _member_capacities() -> dict[str, int]:
    """Per-alias gate capacity: measured slots, else the target's declared value."""
    declared = _served_slot_total()
    capacities = {}
    for alias in _member_aliases():
        capacities[alias] = _MEMBER_SLOTS.get(alias, declared)
    return capacities or {"*": declared}


def _primary_alias() -> str:
    """First served alias of the deploy target (usage/ledger default)."""
    if _RESOLVED is None:
        return DEPLOYED_PROFILE
    members = _RESOLVED.get("members")
    if isinstance(members, list) and members:
        return str(members[0]["alias"])  # type: ignore[index]
    return DEPLOYED_PROFILE


def _member_aliases() -> list[str]:
    if _RESOLVED is None:
        return []
    members = _RESOLVED.get("members")
    if not isinstance(members, list):
        return []
    return [str(member["alias"]) for member in members]  # type: ignore[index]


def _served_slot_total() -> int:
    """Total llama slots this target serves (group members share one env)."""
    if _RESOLVED is None:
        return MAX_NUM_SEQS
    tuning = _RESOLVED.get("tuning")
    per_member = int(tuning.get("numParallel") or MAX_NUM_SEQS) if isinstance(tuning, dict) else MAX_NUM_SEQS
    return per_member


def _assert_members_bootable(members: list[dict[str, object]]) -> None:
    """Every member must already hold its pinned revision on the Volume.

    A co-resident boot that silently dropped a member would look like a
    healthy deployment while routing requests to a model that never loads.
    """
    missing: list[str] = []
    for member in members:
        member_alias = str(member["alias"])
        marker = Path(_model_dir(member_alias)) / ".model-revision"
        recorded = marker.read_text().strip() if marker.exists() else ""
        expected = f"{member['model']}@{member.get('revision') or ''}"
        if not recorded or (member.get("revision") and recorded != expected):
            missing.append(f"{member_alias} (expected {expected!r}, found {recorded!r})")
    if missing:
        raise RuntimeError(
            "model Volume does not hold the pinned revision for: "
            + "; ".join(missing)
            + ". Run bootstrap for each member before deploying this target."
        )


def _measure_member_slots() -> int | None:
    """Slots llama granted the model that loaded most recently.

    Ollama decides `numParallel` per MODEL at load time, not per container, and
    silently forces 1 for architectures it cannot parallelize (sched.go's
    deny-list: mllama, qwen3vl(moe), qwen35(moe), qwen3next, lfm2(moe),
    nemotron_h(_moe/_omni)). `n_slots` on the load line is the only place that
    truth is visible — `/api/ps` carries no slot count, and llama's `/slots`
    endpoint is not proxied by `ollama serve`.

    Preload runs once per container against a fresh log, so the last `n_slots`
    line at read time belongs to the member just loaded. Last-wins also keeps
    this correct if a line-count offset were ever invalidated by the tail cap.
    """
    found: int | None = None
    for line in _read_llama_log_tail():
        match = _N_SLOTS_RE.search(line)
        if match:
            found = int(match.group(1))
    return found


def _measure_router_slots(members: list[str], timeout: int = 900) -> None:
    """Read each router child's real slot count from llama's own `/slots`.

    The router exposes what Ollama does not: `/props?model=<name>` carries the
    child's `n_slots` (and total context), and `/slots?model=<name>` lists the
    live slots. Measured here rather than assumed from tuning, for the same
    reason the Ollama path measures: a declared numParallel is a request, not a
    grant, and the gate must be sized to the grant.
    """
    import httpx

    deadline = time.monotonic() + timeout
    pending = list(members)
    while pending and time.monotonic() < deadline:
        for alias in list(pending):
            try:
                props = httpx.get(
                    "http://127.0.0.1:8000/props",
                    params={"model": alias},
                    timeout=30,
                ).json()
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
            slots = props.get("total_slots") or props.get("n_slots")
            if isinstance(slots, int) and slots > 0:
                _MEMBER_SLOTS[alias] = slots
                print(f"router: {alias} reports n_slots={slots} ctx={props.get('n_ctx')}")
                pending.remove(alias)
        if pending:
            time.sleep(5)
    if pending:
        print(f"router: slot count unmeasured for {pending} (gate falls back to declared)")


def _preload_members(preload: list[str], timeout: int) -> dict[str, int]:
    """Force each hot-set model resident, returning the slots each actually got.

    Ollama loads lazily; without this a group would report ready and then pay a
    full model load (plus CUDA-graph capture) inside a live request.
    """
    import httpx

    measured: dict[str, int] = {}
    for member_alias in preload:
        httpx.post(
            "http://127.0.0.1:8000/api/generate",
            json={"model": member_alias, "keep_alive": -1},
            timeout=timeout,
        ).raise_for_status()
        slots = _measure_member_slots()
        if slots is not None:
            measured[member_alias] = slots
        print(f"preloaded {member_alias} into the hot set (slots={slots if slots is not None else 'unmeasured'})")
    return measured


def _warm_members(preload: list[str], timeout: int) -> None:
    """Warm the CUDA-graph buckets for each member BEFORE user traffic.

    llama-server captures compute graphs lazily on the first request of each
    batch size, costing ~60-100s inside that request (measured: a 16K-token
    request took 82s cold vs 6.8s warm). Typical client timeouts then kill
    the "first" request of every fresh boot, which looked like an outage.

    Routed by runtime: Ollama takes /api/generate, llama-server takes the
    OpenAI /v1/completions shape. Both are hit on the same local port, but a
    warmup against the wrong endpoint would silently no-op (caught below).
    """
    import httpx

    ollama = _RESOLVED is not None and str(_RESOLVED.get("runtime")) == "ollama"
    for member_alias in preload:
        for warm_tokens in (2048, 8192, 16384):
            warm_text = ("warmup " * (warm_tokens // 2)).strip()
            payload: dict[str, object] = (
                {"model": member_alias, "prompt": warm_text, "stream": False, "options": {"num_predict": 1}}
                if ollama
                else {"model": member_alias, "prompt": warm_text, "max_tokens": 1, "stream": False}
            )
            url = "http://127.0.0.1:8000/api/generate" if ollama else "http://127.0.0.1:8000/v1/completions"
            try:
                httpx.post(url, json=payload, timeout=timeout).raise_for_status()
            except Exception as exc:  # warmup is best-effort; never block serving
                print(f"warmup pass {warm_tokens} for {member_alias} skipped: {type(exc).__name__}")


def _llama_single_args(member: dict[str, object], gpu_count: int) -> list[str]:
    """argv for llama-server serving ONE model directly (no router).

    Single-model mode spawns no child process, so it avoids the router's
    subprocess path entirely while still giving per-model `-np`/`-c` — which is
    the whole point of this runtime, since `ollama serve` decides numParallel
    per model and silently clamps some architectures to one slot.
    """
    import llama_router

    tuning = member.get("tuning")
    if not isinstance(tuning, dict) or not tuning:
        raise ValueError(f"member {member.get('alias')!r} has no resolved tuning")
    num_parallel = int(tuning["numParallel"])
    context_tokens = int(tuning["contextTokens"])
    args = [
        "-m",
        str(member["gguf"]),
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--no-webui",
        "--alias",
        str(member["alias"]),
        "-ngl",
        "999",
        "-np",
        str(num_parallel),
        "-c",
        str(llama_router.total_context(context_tokens, num_parallel)),
        "--flash-attn",
        "on",
        "--jinja",
    ]
    kv_type = tuning.get("kvCacheType")
    if isinstance(kv_type, str) and kv_type:
        args += ["-ctk", kv_type, "-ctv", kv_type]
    if tuning.get("cacheReuse"):
        args += ["--cache-reuse", str(llama_router.CACHE_REUSE_CHUNK)]
    # Batch size is a real prefill lever here, unlike on the ollama lane:
    # `ollama serve` hardcodes -b/-ub on its own llama-server cmdline and the
    # cmdline wins, so `batch`/`ubatch` in a catalog tuning block do nothing
    # there. This runtime builds the argv itself, so the keys take effect.
    # Omitted = llama.cpp's own default.
    if tuning.get("batch"):
        args += ["-b", str(int(tuning["batch"]))]
    if tuning.get("ubatch"):
        args += ["-ub", str(int(tuning["ubatch"]))]
    # Multi-GPU split is left to llama.cpp's own default (layer split), which is
    # what the ollama path effectively gets too; pinning to one device would
    # waste the second GPU on a single-model target.
    _ = gpu_count
    return args


def _llama_binary() -> Path:
    """Upstream llama-server, NOT ollama's vendored copy.

    Router mode requires LLAMA_SUBPROCESS, which ollama does not compile in
    (see LLAMA_CPP_BUILD); single-model mode works in either, but this runtime
    always uses the upstream build so both modes behave identically.
    """
    binary = Path("/opt/llama.cpp/llama-server")
    if not binary.exists():
        raise RuntimeError(f"llama-server not found at {binary}; the llama.cpp image step did not run")
    return binary


def _ollama_cuda_lib_dirs() -> list[Path]:
    """Directories holding the CUDA runtime the upstream build links against.

    Upstream llama.cpp links `libcudart.so.12` / `libcublas.so.12`; the ollama
    base image ships those only inside `/usr/lib/ollama/cuda_v12` (its own
    binary loads them by absolute path, so they are never on the default
    loader path). Pinned to the v12 dir because that is the SONAME the
    upstream binary requests.
    """
    return [
        d
        for d in (Path("/usr/lib/ollama/cuda_v12"), Path("/usr/lib/ollama/cuda_v13"))
        if (d / "libcudart.so.12").exists()
    ]


def _require_cuda_device(binary: Path) -> None:
    """Fail closed unless the upstream build can see at least one CUDA device.

    A silent CUDA-load failure is the worst outcome: llama-server starts,
    answers /health, and serves from CPU at ~2.7 tok/s, which looks like a slow
    model rather than a broken deployment. Better to refuse the boot loudly.
    """
    probe = subprocess.run(
        [str(binary), "--list-devices"],
        capture_output=True,
        text=True,
        env=_llama_env(),
        check=False,
    )
    print("llama.cpp devices:")
    for line in (probe.stdout or "").splitlines():
        print("  " + line)
    if "CUDA" not in (probe.stdout or ""):
        raise RuntimeError(
            "llama-server sees no CUDA device (would serve from CPU); "
            f"LD_LIBRARY_PATH={_llama_env().get('LD_LIBRARY_PATH')} stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )


def _llama_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the upstream llama.cpp binary.

    Both the binary's own directory (flat archive, `$ORIGIN` rpath) and
    ollama's CUDA directory MUST be on `LD_LIBRARY_PATH`. Without the latter,
    `ggml_backend_load_all()` silently skips the CUDA backend: `--list-devices`
    prints `(none)`, the server serves from CPU (measured 2.70 tok/s decode /
    5.91 tok/s prefill vs ~48 tok/s on GPU), and router children fail with
    `invalid device: CUDA0`.
    """
    dirs = [str(_llama_binary().parent), *[str(d) for d in _ollama_cuda_lib_dirs()]]
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(dirs)}
    if extra:
        env.update(extra)
    return env


def _boot_llama_single(resolved: dict[str, object], preload: list[str], timeout: int) -> subprocess.Popen[str]:
    """Serve one hot-set member directly from llama-server (no router)."""
    import llama_router

    members = list(resolved.get("members") or [])  # type: ignore[arg-type]
    if len(members) != 1:
        raise RuntimeError(
            f"single-model llama mode serves exactly one member, got {len(members)}; co-residency needs router mode"
        )
    member = dict(members[0])
    member["gguf"] = str(llama_router.gguf_path_for(Path(_ollama_serve_store()), str(member["alias"])))
    binary = _llama_binary()
    _require_cuda_device(binary)
    args = _llama_single_args(member, int(resolved.get("gpu_count") or 1))
    print("llama-server single-model argv: " + " ".join(args))
    log_path = Path(OLLAMA_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["bash", "-c", f"exec {binary} {' '.join(args)} 2>&1 | tee {log_path}"],
        env=_llama_env(),
        text=True,
        start_new_session=True,
    )
    _register_gpu_container()
    _serve_with_heartbeat(process, "http://127.0.0.1:8000/health", timeout, "llama")
    _measure_router_slots(preload)
    return process


def _boot_llama_router(resolved: dict[str, object], preload: list[str], timeout: int) -> subprocess.Popen[str]:
    """Start llama-server in router mode and wait until it serves.

    Router mode is signalled by the ABSENCE of a model argument: llama-server
    then supervises one child process per preset section, each with its own
    command-line args — which is how per-model slots and context become
    expressible at all (`ollama serve` decides numParallel per model and clamps
    some architectures to one slot).
    """
    import llama_router

    members = list(resolved.get("members") or [])  # type: ignore[arg-type]
    preset_ini, _gpu_of = llama_router.build_preset(
        {**resolved, "members": members, "preload": preload},
        Path(_ollama_serve_store()),
        int(resolved.get("gpu_count") or 1),
    )
    preset_path = Path("/tmp/llama-models.ini")
    preset_path.write_text(preset_ini)
    print("llama-server router preset:")
    for line in preset_ini.splitlines():
        if line.strip() and not line.startswith(";"):
            print("  " + line)

    # Upstream build, NOT ollama's vendored copy: router mode requires
    # LLAMA_SUBPROCESS, which ollama does not compile in (see LLAMA_CPP_BUILD).
    llama_bin = _llama_binary()
    # The preset pins members by device name, and ggml's naming is its own; a
    # CUDA-load failure would surface as "invalid device" only after the
    # container is already up, so verify visibility up front.
    _require_cuda_device(llama_bin)
    log_path = Path(OLLAMA_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            f"exec {llama_bin} --models-preset {preset_path} --host 0.0.0.0 "
            f"--port 8000 --no-webui 2>&1 | tee {log_path}",
        ],
        # The archive is flat and its libs resolve via rpath relative to the
        # binary; ollama's CUDA dir supplies libcudart/libcublas.
        env=_llama_env({"LLAMA_CACHE": str(ENGINE_CACHE_DIR)}),
        text=True,
        start_new_session=True,
    )
    _register_gpu_container()
    _serve_with_heartbeat(process, "http://127.0.0.1:8000/health", timeout, "llama")
    _measure_router_slots(preload)
    return process


def _boot_ollama(
    resolved: dict[str, object],
    preload: list[str],
    timeout: int,
    max_len: int,
    recorded: str,
    model_id: str,
    revision: str,
) -> subprocess.Popen[str]:
    """Start `ollama serve`, verify the pinned revision, and preload members."""
    if not recorded.startswith(f"{model_id}@") or (revision and recorded != f"{model_id}@{revision}"):
        raise RuntimeError(
            f"ollama store on the Volume does not hold {model_id} (got {recorded!r}); run bootstrap first"
        )
    ollama_env = _ollama_env(max_len, _ollama_serve_store())
    # Tee ollama/llama-server output to a container-local file so the heartbeat
    # thread can parse per-slot state (prefill progress, decode rate, releases)
    # and publish it for the dashboard. Without a file there is no slot
    # visibility: ollama exposes no /slots passthrough. `tee` keeps stdout
    # flowing to Modal's log stream so `modal app logs` still works.
    log_path = Path(OLLAMA_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["bash", "-c", f"exec ollama serve 2>&1 | tee {log_path}"],
        env=ollama_env,
        text=True,
        start_new_session=True,
    )
    llama_args = {k: v for k, v in ollama_env.items() if k.startswith("LLAMA_ARG_")}
    print("llama-server env args:", json.dumps(llama_args, sort_keys=True))
    _register_gpu_container()
    _log_llama_flags()
    _serve_with_heartbeat(process, "http://127.0.0.1:8000/", timeout, "ollama")
    _MEMBER_SLOTS.update(_preload_members(preload, timeout))
    return process


def _boot_vllm(
    alias: str,
    primary: dict[str, object],
    max_len: int,
    model_dir: str,
    timeout: int,
) -> subprocess.Popen[str]:
    """Start vLLM for a single-member target."""
    member_profile = primary.get("profile") if isinstance(primary, dict) else None
    profile_vllm_args = member_profile.get("vllmArgs", []) if isinstance(member_profile, dict) else []
    args = [
        "vllm",
        "serve",
        model_dir,
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--served-model-name",
        alias,
        "--max-model-len",
        str(max_len),
        "--download-dir",
        ENGINE_CACHE_DIR,
    ]
    extra = [str(value) for value in profile_vllm_args]
    # Base default only when the profile doesn't set its own (avoids duplicate
    # --max-num-seqs, which argparse would reject as ambiguous).
    if not any(item == "--max-num-seqs" for item in extra):
        args += ["--max-num-seqs", str(MAX_NUM_SEQS)]
    process = subprocess.Popen(args + extra, text=True, start_new_session=True)
    _register_gpu_container()
    _log_llama_flags()
    _serve_with_heartbeat(process, "http://127.0.0.1:8000/health", timeout, "vLLM")
    return process


@app.cls(
    gpu=_gpu_spec(int(_RESOLVED["gpu_count"]) if _RESOLVED else 1, str(_RESOLVED["gpu"]) if _RESOLVED else "H100"),
    volumes={MODEL_DIR: model_volume, ENGINE_CACHE_DIR: engine_volume, USAGE_DIR: usage_volume},
    secrets=[hf_secret, dashboard_secret],
    scaledown_window=SCALEDOWN_WINDOW,
    min_containers=MIN_CONTAINERS,
    max_containers=MAX_CONTAINERS,
    timeout=3600,
)
@modal.concurrent(max_inputs=MAX_INPUTS_PER_CONTAINER, target_inputs=TARGET_INPUTS_PER_CONTAINER)
class VLLMServer:
    process: subprocess.Popen[str] | None = None
    lock = threading.Lock()

    @modal.enter()
    def start(self) -> None:
        global _RESOLVED
        if _RESOLVED is None:
            raise RuntimeError(
                "MODEL_PROFILE must be set at deploy time; serve one enabled alias or one serve group per App instance"
            )
        # Boot-time flex: dashboard-written /usage/runtime-overrides.json wins
        # over the baked catalog for THIS container's tuning (no redeploy needed).
        _RESOLVED = {**_RESOLVED, "tuning": _runtime_target_tuning(_RESOLVED)}
        members = list(_RESOLVED.get("members") or [])  # type: ignore[arg-type]
        preload = [str(a) for a in (_RESOLVED.get("preload") or [])]
        primary = members[0] if members else {}
        alias = str(primary.get("alias", DEPLOYED_PROFILE))
        revision = str(primary.get("revision", ""))
        model_id = str(primary.get("model", ""))
        model_dir = _model_dir(alias)
        max_len = int(primary.get("max_len") or 32768)
        # Read at call time: deploy-time env changes don't require an image rebuild.
        timeout = int(os.getenv("HEALTH_TIMEOUT_SECONDS", "2400"))

        _assert_members_bootable(members)
        marker = Path(model_dir) / ".model-revision"
        recorded = marker.read_text().strip() if marker.exists() else ""

        # Each runtime owns its boot sequence; this method only resolves the
        # target and dispatches, so adding a runtime doesn't deepen it.
        runtime = str(_RESOLVED["runtime"])
        if runtime == "llama":
            # Router mode needs a child process per model and the
            # LLAMA_SUBPROCESS build; a one-member target is served directly,
            # which keeps the simple case (and per-model A/B) on the plain path.
            boot = _boot_llama_single if len(members) == 1 else _boot_llama_router
            self.process = boot(_RESOLVED, preload, timeout)
        elif runtime == "ollama":
            self.process = _boot_ollama(_RESOLVED, preload, timeout, max_len, recorded, model_id, revision)
        else:
            if recorded != f"{model_id}@{revision}":
                raise RuntimeError(f"model Volume does not contain the configured pinned revision at {model_dir}")
            self.process = _boot_vllm(alias, primary, max_len, model_dir, timeout)
        _warm_members(preload, timeout)

    @modal.method()
    def ready(self) -> str:
        """Lifecycle barrier: Modal queues this call until @modal.enter() finished.

        Returns once VLLMServer.start() confirmed /health, so `inference warm` can
        block through container startup without blind HTTP retries.
        """
        _write_serving_state(True, True)
        return "ready"

    @modal.exit()
    def stop(self) -> None:
        _write_serving_state(False, False, "container exiting")
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)

    @modal.asgi_app()
    def web(self):
        import hmac
        from contextlib import asynccontextmanager

        import httpx
        from fastapi import FastAPI
        from fastapi.responses import Response, StreamingResponse

        client = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=None)
        proxy_token = os.getenv("MODAL_PROXY_TOKEN", "").strip()
        hop_by_hop = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}
        # Backpressure gate: llama-server has numParallel slots; forwarding more
        # concurrent inference requests than slots overflows the slot pool and
        # kills in-flight decodes ("Server has lost track of input" 500s,
        # observed 2026-10-01 at 6 concurrent / 4 slots / 160K-token prompts).
        # Gate POST /chat/completions at the seam: excess waits briefly, then
        # gets a clean 429 (most clients retry 429s; they do not retry 500s cleanly).
        gate_wait_seconds = GATE_WAIT_SECONDS
        # Per-alias gates: a serve group co-hosts several models, and a request
        # for an idle one must not queue behind a saturated sibling. Capacity is
        # what llama ACTUALLY gave each member (Ollama clamps unparallelizable
        # architectures to one slot regardless of the declared numParallel), so
        # a one-slot model is not handed four concurrent requests to serialize
        # invisibly inside llama. Publish live occupancy so the dashboard can
        # show requests parked before a slot frees (silent waiters) vs requests
        # inside llama.
        gates = _SlotGates(_member_capacities(), gate_wait_seconds)

        def _request_alias(payload_body: bytes, default: str) -> str:
            """Resolve which member a request targets, for gate selection."""
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(payload_body)
                if isinstance(parsed, dict):
                    named = parsed.get("model")
                    if isinstance(named, str) and named.strip():
                        # Ollama tags carry ':latest'; members are catalog aliases.
                        return named.split(":")[0]
            return default

        async def acquire_gate(alias: str) -> None:
            await gates.acquire(alias)

        def release_gate(alias: str) -> None:
            gates.release(alias)

        @asynccontextmanager
        async def lifespan(_app):
            yield
            await client.aclose()

        api = FastAPI(title=APP_NAME, lifespan=lifespan)

        def authorized(request: Request) -> bool:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {proxy_token}" if proxy_token else ""
            return not PROXY_AUTH_ENFORCED or (bool(proxy_token) and hmac.compare_digest(supplied, expected))

        _ollama_runtime = _RESOLVED is not None and str(_RESOLVED["runtime"]) == "ollama"

        def _flatten_content(content: object) -> str:
            """OpenAI content parts (some clients send a list) -> a plain string.

            Ollama's native /api/chat rejects array content with
            `json: cannot unmarshal array into Go struct field
            .ChatRequest.messages.content of type string` (reproduced
            2026-10-02 against real payloads), so the translation must
            flatten before forwarding. Images are dropped (gemma text-only).
            """
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif isinstance(part, str):
                        parts.append(part)
                return "\n".join(parts)
            return str(content) if content is not None else ""

        def _to_native_chat(payload: dict[str, object]) -> dict[str, object]:
            """OpenAI chat payload -> Ollama /api/chat payload (think gating).

            Carries the full conversational shape sent by clients: flattened
            content, assistant tool_calls (arguments string -> object) and tool
            results (tool_name + flattened content), plus the request's tools
            array. Without these the native path silently degrades multi-step
            agent loops.
            """
            messages = []
            tool_names: dict[str, str] = {}
            for m in payload.get("messages", []) or []:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                entry: dict[str, object] = {"role": role, "content": _flatten_content(m.get("content"))}
                tool_calls = m.get("tool_calls")
                if role == "assistant" and isinstance(tool_calls, list):
                    native_calls = []
                    for call in tool_calls:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") or {}
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            with contextlib.suppress(json.JSONDecodeError):
                                args = json.loads(args)
                        call_id = str(call.get("id") or "")
                        name = str(fn.get("name") or "")
                        if call_id and name:
                            tool_names[call_id] = name
                        native_calls.append(
                            {"function": {"name": name, "arguments": args if isinstance(args, dict) else {}}}
                        )
                    if native_calls:
                        entry["tool_calls"] = native_calls
                if role == "tool":
                    call_id = str(m.get("tool_call_id") or "")
                    name = str(m.get("name") or "") or tool_names.get(call_id, "")
                    if name:
                        entry["tool_name"] = name
                messages.append(entry)
            native: dict[str, object] = {
                "model": payload.get("model"),
                "messages": messages,
                "stream": bool(payload.get("stream")),
                "think": False,
            }
            tools = payload.get("tools")
            if isinstance(tools, list) and tools:
                native["tools"] = tools
            if payload.get("tool_choice") is not None:
                native["tool_choice"] = payload["tool_choice"]
            opts: dict[str, object] = {}
            # Some clients send max_completion_tokens; other clients send max_tokens.
            limit = payload.get("max_tokens")
            if not isinstance(limit, int):
                limit = payload.get("max_completion_tokens")
            if isinstance(limit, int):
                opts["num_predict"] = limit
            if payload.get("temperature") is not None:
                opts["temperature"] = payload["temperature"]
            if payload.get("top_p") is not None:
                opts["top_p"] = payload["top_p"]
            if payload.get("stop"):
                opts["stop"] = payload["stop"]
            if opts:
                native["options"] = opts
            return native

        def _native_to_openai_response(native: dict[str, object], model: str) -> dict[str, object]:
            """Ollama /api/chat (stream:false) response -> OpenAI chat completion.

            Tool calls translate back to OpenAI shape (arguments object ->
            JSON string) so agent tool loops keep working through the native
            path; finish_reason becomes tool_calls when any are present.
            """
            message = native.get("message", {}) if isinstance(native, dict) else {}
            native_calls = message.get("tool_calls") if isinstance(message, dict) else None
            openai_calls = []
            if isinstance(native_calls, list):
                for call in native_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    args = fn.get("arguments")
                    openai_calls.append(
                        {
                            "id": f"call_{int(time.time() * 1000)}_{len(openai_calls)}",
                            "type": "function",
                            "function": {
                                "name": str(fn.get("name") or ""),
                                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                            },
                        }
                    )
            usage_in = int(native.get("prompt_eval_count", 0) or 0)
            usage_out = int(native.get("eval_count", 0) or 0)
            out_message: dict[str, object] = {"role": "assistant", "content": message.get("content", "") or ""}
            if openai_calls:
                out_message["tool_calls"] = openai_calls
            return {
                "id": f"chatcmpl-native-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": out_message,
                        "finish_reason": "tool_calls" if openai_calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": usage_in,
                    "completion_tokens": usage_out,
                    "total_tokens": usage_in + usage_out,
                },
            }

        def _openai_sse_chunk(
            model: str,
            delta_content: str,
            finish: str | None = None,
            usage: dict[str, object] | None = None,
            tool_calls: list[dict[str, object]] | None = None,
        ) -> str:
            if tool_calls:
                delta: dict[str, object] = {"tool_calls": tool_calls}
            else:
                delta = {"role": "assistant"} if delta_content == "" and finish is None else {"content": delta_content}
            chunk: dict[str, object] = {
                "id": f"chatcmpl-native-{int(time.time() * 1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                chunk["usage"] = usage
            return f"data: {json.dumps(chunk)}\n\n"

        @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        async def proxy(path: str, request: Request):
            started = time.perf_counter()
            if not authorized(request):
                _usage_event(
                    {
                        # Body unread: the target model is genuinely unknown.
                        "model": _primary_alias(),
                        "path": "/" + path,
                        "method": request.method,
                        "status": 401,
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                        "error": "unauthorized",
                    }
                )
                return Response(status_code=401, content="unauthorized")
            # Narrow the model list to what this deployment can actually serve.
            # `ollama serve` advertises every manifest in its store, but only a
            # PRELOADED member has VRAM behind it: kimi-k2.7-code is a 464GB
            # model on a 2xH200 (287GB) container, so selecting it does not fail
            # fast — ollama accepts the request and hangs until the client times
            # out. The harness cannot tell a loadable model from an unloadable
            # one from this endpoint, so the proxy states it.
            if request.method == "GET" and path.rstrip("/") in ("v1/models", "models"):
                served = _member_aliases()
                if served:
                    try:
                        listed = await client.get(
                            "/" + path,
                            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                        )
                        payload = listed.json()
                    except (httpx.HTTPError, json.JSONDecodeError) as exc:
                        return Response(status_code=502, content=f"model list unavailable: {type(exc).__name__}")
                    entries = payload.get("data")
                    if isinstance(entries, list):
                        allowed = set(served)
                        payload["data"] = [
                            entry
                            for entry in entries
                            if isinstance(entry, dict) and str(entry.get("id", "")).split(":")[0] in allowed
                        ]
                        return Response(
                            status_code=listed.status_code,
                            content=json.dumps(payload),
                            media_type="application/json",
                        )
            body = await request.body()
            forwarded_body = body
            # Which member this request targets — drives per-alias gate choice
            # in a co-resident serve group (falls back to the first member).
            request_alias = _request_alias(body, _primary_alias())
            # Ask OpenAI-compatible backends to include usage in SSE streams.
            # Without this, streamed requests never get token counts in the ledger
            # (observed: streamed requests show dashes in the dashboard).
            if (
                path.endswith("/chat/completions")
                and request.method == "POST"
                and body
                and "text/event-stream" in request.headers.get("accept", "text/event-stream")
            ):
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(body)
                    if isinstance(payload, dict) and isinstance(payload.get("stream_options"), (dict, type(None))):
                        opts = payload.get("stream_options") or {}
                        if not opts.get("include_usage"):
                            opts = {**opts, "include_usage": True}
                            payload["stream_options"] = opts
                            forwarded_body = json.dumps(payload).encode()
            # Thinking control: Ollama's OpenAI endpoint ignores reasoning_effort
            # (measured 2026-09-29: low == default on gemma-4-31b; only the native
            # /api/chat "think": bool actually gates the trace). The per-request
            # reasoning_effort="none" is the client-side off switch; translate it
            # in the seam so agents can suppress wasteful traces without knowing
            # the backend is Ollama.
            if _ollama_runtime and path.endswith("/chat/completions") and request.method == "POST" and body:
                try:
                    payload = json.loads(forwarded_body)
                    if isinstance(payload, dict) and payload.get("reasoning_effort") == "none":
                        # Ollama's OpenAI shim drops the native "think" field, so
                        # the only working off-switch is the native /api/chat
                        # endpoint. Translate request + response here.
                        native_payload = _to_native_chat(payload)
                        native_request = client.build_request(
                            "POST",
                            "/api/chat",
                            content=json.dumps(native_payload).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        try:
                            await acquire_gate(request_alias)
                        except TimeoutError:
                            _usage_event(
                                {
                                    "model": request_alias,
                                    "path": "/" + path,
                                    "method": request.method,
                                    "status": 429,
                                    "elapsed_seconds": round(time.perf_counter() - started, 6),
                                    "error": "slot_gate_timeout",
                                }
                            )
                            return Response(status_code=429, content="server slots busy, retry shortly")
                        native_streaming = bool(native_payload.get("stream"))
                        if native_streaming:
                            # Ollama withholds headers through prefill: answer
                            # immediately and pad until the upstream responds
                            # (same design as the OpenAI path above).
                            native_send = asyncio.create_task(client.send(native_request, stream=True))
                            try:
                                upstream = await asyncio.wait_for(
                                    asyncio.shield(native_send), timeout=EARLY_HEADERS_SECONDS
                                )
                            except TimeoutError:
                                upstream = None
                            except httpx.HTTPError as exc:
                                release_gate(request_alias)
                                _usage_event(
                                    {
                                        "model": request_alias,
                                        "path": "/" + path,
                                        "method": request.method,
                                        "status": 502,
                                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                                        "error": type(exc).__name__,
                                    }
                                )
                                return Response(status_code=502, content=f"inference upstream unreachable: {exc}")
                        else:
                            upstream = await client.send(native_request, stream=False)
                        if not native_payload.get("stream"):
                            raw = (await upstream.aread()).decode("utf-8", "replace")
                            await upstream.aclose()
                            with contextlib.suppress(json.JSONDecodeError):
                                converted = _native_to_openai_response(json.loads(raw), str(payload.get("model")))
                                raw = json.dumps(converted)
                            _usage_event(
                                {
                                    "model": request_alias,
                                    "path": "/" + path,
                                    "method": request.method,
                                    "status": upstream.status_code,
                                    "elapsed_seconds": round(time.perf_counter() - started, 6),
                                    "stream": False,
                                    "think": False,
                                }
                            )
                            release_gate(request_alias)
                            return Response(
                                status_code=upstream.status_code,
                                content=raw,
                                media_type="application/json",
                            )

                        # streaming: convert ollama SSE deltas to OpenAI chunks
                        async def native_stream_relay():
                            nonlocal upstream
                            out_usage = {"prompt_tokens": 0, "completion_tokens": 0}
                            buf = b""
                            cancelled = False
                            # Same early-headers design as the OpenAI path: pad
                            # with SSE comments while ollama withholds headers
                            # through prefill, then adopt the response. Comments
                            # are skipped by most parsers (only "data:" parsed).
                            if upstream is None:
                                deadline = time.monotonic() + HEADERS_DEADLINE_SECONDS
                                while True:
                                    try:
                                        upstream = await asyncio.wait_for(asyncio.shield(native_send), timeout=10)
                                        break
                                    except TimeoutError:
                                        if time.monotonic() > deadline:
                                            raise TimeoutError("upstream headers deadline exceeded") from None
                                        yield ": inference-keepalive\n\n"
                            upstream.timeout = httpx.Timeout(600.0, read=600.0, write=None, pool=None)
                            queue: asyncio.Queue[bytes | None] = asyncio.Queue()

                            async def native_pump() -> None:
                                try:
                                    async for chunk in upstream.aiter_raw():
                                        queue.put_nowait(chunk)
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    pass
                                finally:
                                    queue.put_nowait(None)

                            pump_task = asyncio.create_task(native_pump())
                            try:
                                while True:
                                    try:
                                        chunk = await asyncio.wait_for(queue.get(), timeout=10)
                                    except TimeoutError:
                                        yield ": inference-keepalive\n\n"
                                        continue
                                    if chunk is None:
                                        break
                                    buf += chunk
                                    while b"\n" in buf:
                                        line, buf = buf.split(b"\n", 1)
                                        line_s = line.decode("utf-8", "replace").strip()
                                        if not line_s:
                                            continue
                                        with contextlib.suppress(json.JSONDecodeError):
                                            event = json.loads(line_s)
                                            msg = event.get("message", {})
                                            content = msg.get("content") or ""
                                            if content:
                                                out_usage["completion_tokens"] = out_usage["completion_tokens"] + 1
                                                yield _openai_sse_chunk(str(payload.get("model")), content)
                                            if event.get("done"):
                                                # Native tool calls arrive on the final message; emit
                                                # them as OpenAI deltas before finishing so agent loops
                                                # survive the native path.
                                                deltas: list[dict[str, object]] = []
                                                native_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
                                                if isinstance(native_calls, list) and native_calls:
                                                    for idx, call in enumerate(native_calls):
                                                        if not isinstance(call, dict):
                                                            continue
                                                        fn = call.get("function") or {}
                                                        args = fn.get("arguments")
                                                        deltas.append(
                                                            {
                                                                "index": idx,
                                                                "id": f"call_{int(time.time() * 1000)}_{idx}",
                                                                "type": "function",
                                                                "function": {
                                                                    "name": str(fn.get("name") or ""),
                                                                    "arguments": args
                                                                    if isinstance(args, str)
                                                                    else json.dumps(args or {}),
                                                                },
                                                            }
                                                        )
                                                    if deltas:
                                                        yield _openai_sse_chunk(
                                                            str(payload.get("model")), "", None, None, deltas
                                                        )
                                                out_usage["prompt_tokens"] = int(event.get("prompt_eval_count", 0) or 0)
                                                out_usage["completion_tokens"] = int(event.get("eval_count", 0) or 0)
                                                usage_block = {
                                                    "prompt_tokens": out_usage["prompt_tokens"],
                                                    "completion_tokens": out_usage["completion_tokens"],
                                                    "total_tokens": out_usage["prompt_tokens"]
                                                    + out_usage["completion_tokens"],
                                                }
                                                yield _openai_sse_chunk(
                                                    str(payload.get("model")),
                                                    "",
                                                    "tool_calls" if deltas else "stop",
                                                    usage_block,
                                                )
                                                yield "data: [DONE]\n\n"
                            except (asyncio.CancelledError, GeneratorExit) as exc:
                                # Client disconnected (client request timeout).
                                # Acknowledge the cancellation immediately: the
                                # bounded aclose() in the finally below releases
                                # the upstream socket. NOTE: Response.close()
                                # raises on an async stream, and an exception
                                # escaping a generator mid-cancellation makes
                                # Modal kill the task ("failed to respond to
                                # cancellation"), which is the crash loop this
                                # used to cause.
                                cancelled = True
                                if isinstance(exc, asyncio.CancelledError):
                                    raise
                            finally:
                                pump_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await pump_task
                                with contextlib.suppress(Exception):
                                    await asyncio.wait_for(upstream.aclose(), timeout=2)
                                _usage_event(
                                    {
                                        "model": request_alias,
                                        "path": "/" + path,
                                        "method": request.method,
                                        "status": upstream.status_code if not cancelled else 499,
                                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                                        "stream": True,
                                        "think": False,
                                        **out_usage,
                                    }
                                )
                                release_gate(request_alias)

                        return StreamingResponse(
                            native_stream_relay(),
                            status_code=upstream.status_code,
                            headers={"Content-Type": "text/event-stream"},
                            media_type="text/event-stream",
                        )
                    if (
                        isinstance(payload, dict)
                        and payload.get("reasoning_effort") is not None
                        and payload.get("reasoning_effort") != "none"
                    ):
                        # OpenAI reasoning_effort has no native mapping (measured:
                        # ollama ignores it); strip it so backends don't 400 on
                        # unknown params.
                        del payload["reasoning_effort"]
                        forwarded_body = json.dumps(payload).encode()
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass
            upstream_request = client.build_request(
                request.method,
                "/" + path,
                content=forwarded_body,
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() != "host" and not (forwarded_body != body and k.lower() == "content-length")
                },
            )
            # Inference POSTs pass the slot gate; health/metrics/other verbs go direct.
            gated = request.method == "POST" and (path.endswith("/chat/completions") or path == "/api/chat")
            if gated:
                try:
                    await acquire_gate(request_alias)
                except TimeoutError:
                    _usage_event(
                        {
                            "model": request_alias,
                            "path": "/" + path,
                            "method": request.method,
                            "status": 429,
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                            "error": "slot_gate_timeout",
                        }
                    )
                    return Response(status_code=429, content="server slots busy, retry shortly")
            # Streaming inference requests get an immediate response: ollama
            # withholds upstream headers through the whole prefill, so waiting
            # for them means total client silence for minutes. Non-streaming
            # requests keep the buffered path (they need the full body anyway).
            stream_requested = False
            with contextlib.suppress(json.JSONDecodeError):
                parsed_body = json.loads(forwarded_body)
                if isinstance(parsed_body, dict):
                    stream_requested = bool(parsed_body.get("stream"))
            if request.method == "POST" and stream_requested and gated:
                send_task: asyncio.Task = asyncio.create_task(client.send(upstream_request, stream=True))
                try:
                    upstream = await asyncio.wait_for(asyncio.shield(send_task), timeout=EARLY_HEADERS_SECONDS)
                except TimeoutError:
                    upstream = None  # slow start: pad the client until headers land
                except httpx.HTTPError as exc:
                    release_gate(request_alias)
                    _usage_event(
                        {
                            "model": request_alias,
                            "path": "/" + path,
                            "method": request.method,
                            "status": 502,
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                            "error": type(exc).__name__,
                        }
                    )
                    return Response(status_code=502, content=f"inference upstream unreachable: {exc}")
                if upstream is not None and upstream.status_code >= 400:
                    # Real error (401/404/500...): the client expects an SSE
                    # format but the body here is a small JSON error. Relay the
                    # true status + body verbatim rather than a fake 200 stream.
                    raw = (await upstream.aread()).decode("utf-8", "replace")
                    await upstream.aclose()
                    if gated:
                        release_gate(request_alias)
                    _usage_event(
                        {
                            "model": request_alias,
                            "path": "/" + path,
                            "method": request.method,
                            "status": upstream.status_code,
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                            "stream": True,
                        }
                    )
                    return Response(status_code=upstream.status_code, content=raw, media_type="application/json")

                async def early_relay():
                    chunks: list[bytes] = []
                    cancelled: asyncio.CancelledError | None = None
                    nonlocal upstream
                    try:
                        if upstream is None:
                            # Slow path: upstream headers withheld through
                            # prefill. Pad with SSE comments so the client's
                            # idle window never fires, then adopt the response
                            # the moment it arrives. Most client parsers skip non-"data:" lines.
                            deadline = time.monotonic() + HEADERS_DEADLINE_SECONDS
                            while True:
                                try:
                                    upstream = await asyncio.wait_for(asyncio.shield(send_task), timeout=10)
                                    break
                                except TimeoutError:
                                    if time.monotonic() > deadline:
                                        raise TimeoutError("upstream headers deadline exceeded") from None
                                    yield b": inference-keepalive\n\n"
                        upstream.timeout = httpx.Timeout(600.0, read=600.0, write=None, pool=None)
                        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

                        async def pump() -> None:
                            try:
                                async for chunk in upstream.aiter_raw():
                                    queue.put_nowait(chunk)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
                            finally:
                                queue.put_nowait(None)

                        pump_task = asyncio.create_task(pump())
                        try:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(queue.get(), timeout=10)
                                except TimeoutError:
                                    # Mid-stream stall (e.g. long decode gap) —
                                    # keep the connection visibly alive.
                                    yield b": inference-keepalive\n\n"
                                    continue
                                if chunk is None:
                                    break
                                chunks.append(chunk)
                                yield chunk
                        finally:
                            pump_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await pump_task
                    except asyncio.CancelledError as exc:
                        # Client disconnected (a client-side timeout). Record it
                        # and let the finally's bounded aclose() release the
                        # upstream socket. Response.close() raises on an async
                        # stream and must never be called here: an exception
                        # escaping during cancellation gets the task killed.
                        cancelled = exc
                        send_task.cancel()
                    except GeneratorExit:
                        send_task.cancel()
                    finally:
                        if upstream is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(upstream.aclose(), timeout=2)
                        raw = b"".join(chunks)
                        usage: dict[str, int] = {}
                        if upstream is not None:
                            if "application/json" in upstream.headers.get("content-type", ""):
                                with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                                    usage = _usage_from_json(json.loads(raw))
                            elif "text/event-stream" in upstream.headers.get("content-type", ""):
                                usage = _usage_from_stream(raw)
                        _usage_event(
                            {
                                "model": request_alias,
                                "path": "/" + path,
                                "method": request.method,
                                "status": (upstream.status_code if upstream is not None else 200)
                                if not cancelled
                                else 499,
                                "elapsed_seconds": round(time.perf_counter() - started, 6),
                                "stream": True,
                                **usage,
                            }
                        )
                        release_gate(request_alias)
                    if cancelled is not None:
                        raise cancelled

                return StreamingResponse(
                    early_relay(),
                    status_code=200,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            try:
                upstream = await client.send(upstream_request, stream=True)
            except httpx.HTTPError as exc:
                if gated:
                    release_gate(request_alias)
                _usage_event(
                    {
                        "model": request_alias,
                        "path": "/" + path,
                        "method": request.method,
                        "status": 502,
                        "elapsed_seconds": round(time.perf_counter() - started, 6),
                        "error": type(exc).__name__,
                    }
                )
                return Response(status_code=502, content=f"inference upstream unreachable: {exc}")
            response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in hop_by_hop}

            async def relay():
                chunks: list[bytes] = []
                cancelled: asyncio.CancelledError | None = None
                # Liveness guard while streaming: a dead-but-held upstream
                # socket must not hold a gated slot forever (observed: llama
                # stalled mid-prefill, relays hung on timeout=None, all four
                # slots leaked, everything 429'd at 300s). 600s with zero
                # bytes from upstream = treat as hung, abort, release slot.
                upstream.timeout = httpx.Timeout(600.0, read=600.0, write=None, pool=None)
                queue: asyncio.Queue[bytes | None] = asyncio.Queue()

                async def pump() -> None:
                    try:
                        async for chunk in upstream.aiter_raw():
                            queue.put_nowait(chunk)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    finally:
                        queue.put_nowait(None)

                pump_task = asyncio.create_task(pump())
                try:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=10)
                        except TimeoutError:
                            yield b": inference-keepalive\n\n"
                            continue
                        if chunk is None:
                            break
                        chunks.append(chunk)
                        yield chunk
                except asyncio.CancelledError as exc:
                    # Client disconnected (a client-side timeout, typically
                    # mid-prefill). Record it and let the finally's bounded
                    # aclose() release the upstream socket; llama keeps the
                    # partial prefill in the slot so a retry resumes warm.
                    # Response.close() raises on an async stream and must never
                    # be called here: an exception escaping during cancellation
                    # makes Modal kill the task, which is the crash loop this
                    # used to cause.
                    cancelled = exc
                except GeneratorExit:
                    # Generator close: no re-raise (PEP 525 forbids converting
                    # GeneratorExit); aclose() in the finally does the release.
                    pass
                finally:
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump_task
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(upstream.aclose(), timeout=2)
                    raw = b"".join(chunks)
                    usage: dict[str, int] = {}
                    if "application/json" in upstream.headers.get("content-type", ""):
                        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                            usage = _usage_from_json(json.loads(raw))
                    elif "text/event-stream" in upstream.headers.get("content-type", ""):
                        usage = _usage_from_stream(raw)
                    _usage_event(
                        {
                            "model": request_alias,
                            "path": "/" + path,
                            "method": request.method,
                            "status": upstream.status_code if not cancelled else 499,
                            "elapsed_seconds": round(time.perf_counter() - started, 6),
                            "stream": "text/event-stream" in upstream.headers.get("content-type", ""),
                            **usage,
                        }
                    )
                    if gated:
                        release_gate(request_alias)
                if cancelled is not None:
                    # propagate the cancellation so Modal/Starlette complete
                    # the disconnect promptly (avoids the 30s kill).
                    raise cancelled

            return StreamingResponse(
                relay(),
                status_code=upstream.status_code,
                headers=response_headers,
                media_type=upstream.headers.get("content-type"),
            )

        return api




import modal_inference_cost_model as _cm  # noqa: E402

_cost_compare_payload = _cm._cost_compare_payload
_archive_billing = _cm._archive_billing
_cost_curve = _cm._cost_curve
_serving_cost_usd = _cm._serving_cost_usd
# inject the debounced volume publisher used by _archive_billing
_cm.thread_publish = _volume_commit_soon
dashboard_image = _bake_modules(
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi==0.133.0")
    .env({"MODEL_PROFILE": DEPLOYED_PROFILE})
    .add_local_dir(
        str(Path(__file__).parent / "dashboard" / "dist"),
        "/root/dashboard",
    )
    .add_local_file(CATALOG_PATH, "/root/models.json")
    .add_local_file(Path(__file__).parent / "results" / "external-rate-cards.json", "/root/external-rate-cards.json")
)


@app.function(
    image=dashboard_image,
    volumes={USAGE_DIR: usage_volume},
    secrets=[dashboard_secret],
    min_containers=0,
    max_containers=1,
    timeout=120,
)
@modal.asgi_app()
def dashboard():
    """Deploy root wraps the module-built dashboard; usage volume injected."""
    from modal_inference_dashboard import build_dashboard_api

    return build_dashboard_api(usage_volume=usage_volume)


@app.local_entrypoint()
def main() -> None:
    print("Bootstrap a profile:  uv run modal run modal_service.py::bootstrap_model --alias <alias>")
    print("Then deploy one alias: MODEL_PROFILE=<alias> uv run modal deploy modal_service.py")


@app.local_entrypoint()
def gpu_stop_eager() -> None:
    """Zero the GPU worker: VLLMServer autoscaler -> 0/0, live GPU containers stopped.

    The dashboard Function is CPU-only and is left untouched. Runs as a
    local_entrypoint so the whole thing executes in one clean event loop
    (`uv run modal run modal_service.py::gpu_stop_eager`); the inference CLI shells
    out to this instead of doing sync/async gymnastics.

    Container enumeration uses the SUPPORTED `modal container` CLI, not the
    private TaskList/TaskGetInfo/ContainerStop stub. Those private RPCs were
    the shutdown hang: `TaskList` never returned ("RPC request ... made outside
    of task context") and ate the caller's whole timeout while the GPU was
    already gone. `modal container list --json` reports the same containers and
    `stop` does the stopping, both on a maintained surface.
    """
    import asyncio

    async def run() -> dict[str, object]:
        # Idempotent when nothing is up. `Cls.from_name` is LAZY — it resolves
        # the app only when the class is called, so the guard wraps the call.
        try:
            vllm_cls = modal.Cls.from_name(APP_NAME, "VLLMServer")
            await vllm_cls().update_autoscaler.aio(min_containers=0, max_containers=0, scaledown_window=0)
        except modal.exception.NotFoundError:
            return {"ok": True, "containers_stopped": [], "detail": "app not deployed; nothing to stop"}

        # The autoscaler change alone retires idle containers, but a container
        # mid-request (or mid-boot) can outlive it; stop those explicitly.
        listed = subprocess.run(
            ["uv", "run", "modal", "container", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        containers: list[dict[str, object]] = []
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(listed.stdout or "[]")
            if isinstance(parsed, list):
                containers = [c for c in parsed if isinstance(c, dict)]
        stopped: list[str] = []
        for container in containers:
            container_id = str(container.get("container_id") or "")
            # Only our own App's containers; never another app's.
            if not container_id or str(container.get("app_name") or "") != APP_NAME:
                continue
            stopped_now = subprocess.run(
                ["uv", "run", "modal", "container", "stop", "--yes", container_id],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if stopped_now.returncode == 0:
                stopped.append(container_id)
        return {"ok": True, "containers_stopped": stopped}

    print(json.dumps(asyncio.run(run())))
