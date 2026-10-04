"""Catalog resolution: profile loading, revision pins, tuning profiles, runtime overrides.

Pure module — no Modal objects; CATALOG_PATH resolves the same way as the deploy root.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CATALOG_PATH = Path(os.getenv("MODEL_CATALOG_PATH", Path(__file__).with_name("models.json")))
DEPLOYED_PROFILE = os.getenv("MODEL_PROFILE", "").strip()
USAGE_DIR = "/usage"
MODEL_DIR = "/models"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _runtime(profile: dict[str, object]) -> str:
    runtime = str(profile.get("runtime", "vllm"))
    if runtime not in ("vllm", "ollama", "llama"):
        raise ValueError(f"unsupported runtime {runtime!r}; expected vllm, ollama, or llama")
    return runtime


def _load_profile(alias: str) -> dict[str, object]:
    if not alias:
        raise ValueError("alias is required; choose an enabled alias from models.json")
    catalog = json.loads(CATALOG_PATH.read_text())
    models = catalog.get("models")
    profile = models.get(alias) if isinstance(models, dict) else None
    if not isinstance(profile, dict) or not profile.get("enabled"):
        raise ValueError(f"model profile {alias!r} is missing or disabled in {CATALOG_PATH}")
    return profile


def _profile_runtime(profile: dict[str, object], alias: str) -> dict[str, object]:
    return {
        "runtime": _runtime(profile),
        "model": str(profile["model"]),
        "revision": str(profile.get("revision") or ""),
        "gpu": str(profile.get("gpu", os.getenv("GPU", "H100"))),
        "gpu_count": int(profile.get("gpuCount", os.getenv("GPU_COUNT", "1"))),
        "max_len": int(profile.get("maxContextTokens", os.getenv("MAX_MODEL_LEN", "32768"))),
        "alias": alias,
    }


def _gpu_spec(count: int, gpu: str) -> str:
    # Modal's multi-GPU form is "B200:4"; a LIST means fallback options, not multiple GPUs.
    return gpu if count == 1 else f"{gpu}:{count}"


def _model_dir(alias: str) -> str:
    return f"{MODEL_DIR}/{alias}"


def resolve_profile(alias: str) -> dict[str, object]:
    profile = _load_profile(alias)
    model = profile.get("model")
    revision = profile.get("revision")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"profile {alias!r} requires model")
    runtime = _runtime(profile)
    if runtime == "vllm" and (not isinstance(revision, str) or not _SHA_RE.fullmatch(revision)):
        raise ValueError(f"profile {alias!r} requires a full 40-character revision")
    return {
        "alias": alias,
        "model": model,
        "revision": str(revision or ""),
        "runtime": runtime,
        "gpu": str(profile.get("gpu", "H100")),
        "gpu_count": int(profile.get("gpuCount", 1)),
        "max_len": int(profile.get("maxContextTokens", 32768)),
        "profile": profile,
    }


def _tuning_profile(profile: dict[str, object]) -> tuple[str, dict[str, object]]:
    active = str(profile.get("activeTuning", "baseline"))
    tuning = profile.get("tuning", {})
    if not isinstance(tuning, dict):
        return active, {}
    selected = tuning.get(active)
    return active, selected if isinstance(selected, dict) else {}


def _load_group(name: str) -> dict[str, object] | None:
    """Return the raw serveGroups entry, or None when `name` is not a group."""
    catalog = json.loads(CATALOG_PATH.read_text())
    groups = catalog.get("serveGroups")
    group = groups.get(name) if isinstance(groups, dict) else None
    return group if isinstance(group, dict) else None


def _alias_target(name: str) -> dict[str, object]:
    """Single-alias serve target (the historical deployment shape)."""
    profile = _load_profile(name)
    active, tuning = _tuning_profile(profile)
    return {
        "name": name,
        "is_group": False,
        "runtime": _runtime(profile),
        "gpu": str(profile.get("gpu", "H100")),
        "gpu_count": int(profile.get("gpuCount", 1)),
        "tuning": dict(tuning),
        "tuning_name": active,
        "members": [
            {
                "alias": name,
                "model": str(profile["model"]),
                "revision": str(profile.get("revision") or ""),
                "max_len": int(profile.get("maxContextTokens", 32768)),
                "profile": profile,
            }
        ],
        "preload": [name],
    }


def _group_member(group_name: str, runtime: str, alias: object) -> dict[str, object]:
    """One validated member entry; `runtime` is the group's resolved runtime.

    The member's own profile runtime is what a standalone deploy would use. A
    group may override it (see _group_target) because the runtime describes how
    the plan serves the model, not what the model is — the same GGUF is
    servable by `ollama serve` or llama-server's router. Overriding across the
    ollama/llama pair is therefore allowed; a member whose profile needs a
    genuinely different engine (vllm) is not.
    """
    profile = _load_profile(str(alias))
    member_runtime = _runtime(profile)
    # ollama and llama both read the same GGUF store from the same image, so a
    # group may serve either; anything else must match exactly.
    interchangeable = {member_runtime, runtime} <= {"ollama", "llama"}
    if member_runtime != runtime and not interchangeable:
        raise ValueError(
            f"serve group {group_name!r} mixes runtimes ({runtime!r} and {member_runtime!r} "
            f"from {alias!r}); every member must share one runtime"
        )
    revision = str(profile.get("revision") or "")
    if member_runtime == "vllm" and not _SHA_RE.fullmatch(revision):
        raise ValueError(f"profile {alias!r} requires a full 40-character revision")
    return {
        "alias": str(alias),
        "model": str(profile.get("model", "")),
        "revision": revision,
        "max_len": int(profile.get("maxContextTokens", 32768)),
        "profile": profile,
    }


def _group_runtime(name: str, group: dict[str, object], aliases: list[object]) -> str:
    """Resolve and validate a serve group's runtime.

    The runtime is a property of the SERVING PLAN, not the model — the same
    GGUF is servable by `ollama serve` or by llama-server's router — so a group
    may override the members' own runtime. That override is what makes an
    engine A/B possible without duplicating model profiles.
    """
    member_runtimes = {_runtime(_load_profile(str(alias))) for alias in aliases}
    if len(member_runtimes) > 1:
        raise ValueError(
            f"serve group {name!r} mixes member runtimes {sorted(member_runtimes)}; every member must share one"
        )
    declared = group.get("runtime")
    runtime = str(declared) if isinstance(declared, str) and declared else member_runtimes.pop()
    if runtime not in ("vllm", "ollama", "llama"):
        raise ValueError(f"serve group {name!r} runtime {runtime!r} must be vllm, ollama, or llama")
    if runtime == "vllm" and len(aliases) > 1:
        # One vLLM process serves one model; co-hosting needs a process (and a
        # port) per member, which is a different design. Fail closed rather
        # than silently serving only the first member.
        raise ValueError(
            f"serve group {name!r} is vllm with {len(aliases)} members; co-residency is implemented for "
            "ollama groups only (one vLLM process serves one model). Deploy members as separate targets."
        )
    return runtime


def _group_preload(name: str, group: dict[str, object], members: list[dict[str, object]]) -> list[str]:
    """Aliases to warm at boot, defaulting to every member; must be members."""
    preload = group.get("preload")
    preload_aliases = (
        [str(a) for a in preload] if isinstance(preload, list) and preload else [str(m["alias"]) for m in members]
    )
    known = {str(m["alias"]) for m in members}
    unknown = [a for a in preload_aliases if a not in known]
    if unknown:
        raise ValueError(f"serve group {name!r} preloads {unknown!r}, which are not in its aliases list")
    return preload_aliases


def _group_tuning(group: dict[str, object], members: list[dict[str, object]], runtime: str) -> dict[str, object]:
    """Resolve the group tuning block, then stamp each member's served tuning."""
    tuning = group.get("tuning")
    if not isinstance(tuning, dict) or not tuning:
        # Omitted group tuning inherits the first member's active profile, which
        # keeps a group-of-one behaving exactly like that alias deployed alone.
        _active, tuning = _tuning_profile(members[0]["profile"])  # type: ignore[arg-type]
    tuning = dict(tuning)
    # Each member carries the tuning it will be SERVED with. For a container-
    # global runtime (ollama) that is the group block for everyone; for the
    # llama router each member gets its own block, so resolve it per member
    # now rather than leaving the router to invent defaults it cannot infer.
    for member in members:
        member["tuning"] = _member_tuning(member, tuning, runtime)
    return tuning


def _group_target(name: str, group: dict[str, object]) -> dict[str, object]:
    """Co-resident serve target: several aliases sharing one container."""
    aliases = group.get("aliases")
    if not isinstance(aliases, list) or not aliases:
        raise ValueError(f"serve group {name!r} requires a non-empty aliases list")
    runtime = _group_runtime(name, group, aliases)
    members = [_group_member(name, runtime, alias) for alias in aliases]
    preload_aliases = _group_preload(name, group, members)
    tuning = _group_tuning(group, members, runtime)
    return {
        "name": name,
        "is_group": True,
        "runtime": runtime,
        "gpu": str(group.get("gpu", "H100")),
        "gpu_count": int(group.get("gpuCount", 1)),
        "tuning": tuning,
        "tuning_name": str(group.get("activeTuning", "baseline")),
        "members": members,
        "preload": preload_aliases,
    }


def _member_tuning(member: dict[str, object], group_tuning: dict[str, object], runtime: str) -> dict[str, object]:
    """The tuning one member is actually served with.

    A router runtime gives every member its OWN llama-server process, so a
    member's own active tuning is expressible and wins — the group block only
    supplies what the member leaves unset. Every other runtime is
    container-global (one env for the whole server), so the group block is
    authoritative and member tuning is ignored, matching what is served.
    """
    if runtime != "llama":
        return dict(group_tuning)
    profile = member.get("profile")
    member_block = _tuning_profile(profile)[1] if isinstance(profile, dict) else {}
    merged = dict(group_tuning)
    merged.update(member_block)
    return merged


def resolve_serve_target(name: str) -> dict[str, object]:
    """Resolve a serve target: a plain alias, or a serve group (hot set).

    One container serves a *target*. An alias target is the historical
    single-model deployment; a group target co-hosts several aliases in one
    container so they never evict each other. Both return the same shape, so
    callers (deploy, boot, gate, install) need no branch.

    Group tuning is CONTAINER-GLOBAL: OLLAMA_CONTEXT_LENGTH and friends are
    one env for the whole server, so a member's own activeTuning is ignored
    while it is in a group. The group's block is authoritative.
    """
    group = _load_group(name)
    return _alias_target(name) if group is None else _group_target(name, group)


def _runtime_tuning_override(profile: dict[str, object]) -> dict[str, object]:
    """Merge boot-time overrides from the usage Volume over the baked tuning.

    The dashboard writes /usage/runtime-overrides.json ({alias: {numParallel, ...}});
    this makes knobs like numParallel flex WITHOUT a redeploy — the next GPU
    container boot picks them up. Bad/missing file falls back to the baked profile.
    """
    try:
        path = Path(USAGE_DIR) / "runtime-overrides.json"
        if not path.exists():
            return dict(profile)
        value = json.loads(path.read_text())
        overrides = value.get(DEPLOYED_PROFILE) if isinstance(value, dict) else None
        if not isinstance(overrides, dict):
            return dict(profile)
    except (OSError, json.JSONDecodeError):
        return dict(profile)
    merged = dict(profile)
    tuning = dict(merged.get("tuning") or {}) if isinstance(merged.get("tuning"), dict) else {}
    active = str(merged.get("activeTuning", "baseline"))
    base = dict(tuning.get(active) or {})
    base.update(overrides)
    tuning[active] = base
    merged["tuning"] = tuning
    return merged


def _runtime_target_tuning(target: dict[str, object]) -> dict[str, object]:
    """Boot-time tuning overrides for a serve TARGET (alias or group).

    A group has no member profile to flex — its tuning IS the container env —
    so overrides written by `mci tuning flex --alias <group>` (or the
    dashboard) apply straight onto the target's block. Single-alias targets
    flow through `_runtime_tuning_override` so their existing behavior and
    file format are unchanged.
    """
    if not target.get("is_group"):
        members = target.get("members") or []
        if isinstance(members, list) and members:
            member = members[0]  # type: ignore[index]
            profile = member.get("profile") if isinstance(member, dict) else None
            if isinstance(profile, dict):
                updated = _runtime_tuning_override(profile)
                _active, tuning = _tuning_profile(updated)
                return dict(tuning)
        return dict(target.get("tuning") or {})
    try:
        path = Path(USAGE_DIR) / "runtime-overrides.json"
        if not path.exists():
            return dict(target.get("tuning") or {})
        value = json.loads(path.read_text())
        overrides = value.get(str(target.get("name", ""))) if isinstance(value, dict) else None
        if not isinstance(overrides, dict):
            return dict(target.get("tuning") or {})
    except (OSError, json.JSONDecodeError):
        return dict(target.get("tuning") or {})
    merged = dict(target.get("tuning") or {})
    merged.update(overrides)
    return merged
