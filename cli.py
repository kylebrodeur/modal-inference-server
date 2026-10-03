"""Operator CLI for the Modal inference-server deployment.

    modal-inference setup                   # one-time: writes ~/.config/modal-inference/config.json (repo, URL, token)
    modal-inference use <alias>             # switch: deploy that alias if needed, then warm it
    modal-inference warm | health | status  # act on the currently deployed alias
    modal-inference shutdown                # scale to zero now
    modal-inference models list|add|update|enable|disable

Config lives in ~/.config/modal-inference/config.json (mode 600); the token is never printed.
Env vars MODAL_BASE_URL / MODAL_PROXY_TOKEN / MODEL_PROFILE override the file when set.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path(os.getenv("MODAL_INFERENCE_CONFIG", Path.home() / ".config" / "modal-inference" / "config.json"))
APP_NAME = os.getenv("APP_NAME", "modal-inference-server")
# The provider name Pi/OMP uses to address this service; configurable so
# operators can namespace it (e.g. `modal-inference/my-model` vs `my-org/my-model`).
PROVIDER_NAME = os.getenv("MODAL_INFERENCE_PROVIDER", "modal-inference")
HEALTH_PATH = "/v1/models"  # exists on vLLM and Ollama; vLLM's /health has no Ollama equivalent
# Held by a detached process: a spawned-modal RPC is cancelled if its client exits.
_BARRIER_SNIPPET = (
    "import sys, modal\n"
    "app, timeout = sys.argv[1], int(sys.argv[2])\n"
    "call = modal.Cls.from_name(app, 'VLLMServer')().ready.spawn()\n"
    "try:\n"
    "    call.get(timeout=timeout)\n"
    "except BaseException:\n"
    "    pass\n"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def _write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


def _setting(env: str, key: str, what: str) -> str:
    value = os.getenv(env, "").strip() or str(_config().get(key, "")).strip()
    if not value:
        raise SystemExit(f"{what} is not configured; run `modal-inference setup` in the repo (or set {env})")
    return value


def _repo_root() -> Path:
    # Prefer the source checkout so `modal-inference` (uv tool) reads the live catalog, not its bundled copy.
    return Path(_setting("MODAL_INFERENCE_REPO", "repo", "repo path"))


def _catalog_path() -> Path:
    override = os.getenv("MODEL_CATALOG_PATH")
    if override:
        return Path(override)
    return Path(__file__).parent / "server" / "models.json"


def _base_url() -> str:
    return _setting("MODAL_BASE_URL", "base_url", "deployed URL").rstrip("/")


def _token() -> str:
    return _setting("MODAL_PROXY_TOKEN", "token", "proxy token")


def _dashboard_url() -> str:
    return _setting("MODAL_INFERENCE_DASHBOARD_URL", "dashboard_url", "dashboard URL").rstrip("/")


def _dashboard_token() -> str:
    return _setting("MODAL_INFERENCE_DASHBOARD_TOKEN", "dashboard_token", "dashboard token")


def _dashboard_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_dashboard_token()}"}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def cmd_stats(args: argparse.Namespace) -> int:
    """Print authenticated usage grouped by model and tuning profile (from the dashboard, not the inference proxy)."""
    response = httpx.get(f"{_dashboard_url()}/_dashboard/api/stats", headers=_dashboard_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    events = [event for event in data.get("events", []) if isinstance(event, dict)]
    usage = data.get("usage", {})
    result = {
        "deployment": data.get("deployment"),
        "event_count": data.get("event_count", len(events)),
        "model_profiles": data.get("model_profiles", sorted({event.get("model") for event in events})),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "metered_today_usd": usage.get("metered_today_usd"),
        "metered_month_to_date_usd": usage.get("metered_month_to_date_usd"),
        "workspace_billed_month_usd": usage.get("workspace_billed_month_usd"),
        "events": events if args.json else None,
    }
    if not args.json:
        result.pop("events")
    print(json.dumps(result, indent=2))
    return 0


def cmd_pricing(args: argparse.Namespace) -> int:
    """Compare Modal's real metered cost (from the dashboard/billing API) with a token-priced provider estimate."""
    response = httpx.get(f"{_dashboard_url()}/_dashboard/api/stats", headers=_dashboard_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    events = [event for event in data.get("events", []) if isinstance(event, dict)]
    usage = data.get("usage", {})
    prompt = sum(int(event.get("prompt_tokens", 0) or 0) for event in events)
    completion = sum(int(event.get("completion_tokens", 0) or 0) for event in events)
    provider_cost = prompt / 1_000_000 * args.provider_input_usd_per_million
    provider_cost += completion / 1_000_000 * args.provider_output_usd_per_million
    modal_metered = usage.get("metered_month_to_date_usd")
    print(
        json.dumps(
            {
                "model_profiles": data.get("model_profiles", sorted({event.get("model") for event in events})),
                "modal": {
                    "metered_month_to_date_usd": modal_metered,
                    "workspace_billed_month_usd": usage.get("workspace_billed_month_usd"),
                },
                "provider": {"input_tokens": prompt, "output_tokens": completion, "estimated_usd": provider_cost},
                "difference_usd": (modal_metered - provider_cost) if modal_metered is not None else None,
            },
            indent=2,
        )
    )
    return 0


def _rate_cards(rates_arg: str | None) -> dict[str, dict[str, Any]]:
    """Load the rate-card file and return model-id -> row (with provider attached)."""
    rates_path = Path(rates_arg) if rates_arg else _repo_root() / "server" / "results" / "external-rate-cards.json"
    if not rates_path.exists():
        raise SystemExit(f"no rate card file at {rates_path}")
    rates = json.loads(rates_path.read_text())
    by_id: dict[str, dict[str, Any]] = {}
    for provider_name, provider in rates.get("providers", {}).items():
        for model in provider.get("models", []):
            if isinstance(model, dict) and model.get("input_usd_per_m") is not None:
                by_id[model["id"]] = {**model, "provider": provider_name}
    return by_id


def _external_row(target: dict[str, Any], prompt: int, completion: int, actual_usd: float) -> dict[str, Any]:
    """Counterfactual: what this month's token mix would have cost on one external API."""
    name = str(target["id"])
    r_in = float(target["input_usd_per_m"])
    r_out = float(target["output_usd_per_m"])
    r_cached = float(target.get("cached_input_usd_per_m") or r_in)
    same_mix = prompt / 1e6 * r_in + completion / 1e6 * r_out
    same_mix_cached = prompt / 1e6 * r_cached + completion / 1e6 * r_out
    if actual_usd > 0:
        ratio = same_mix / actual_usd
        verdict = (
            f"{name} would cost {ratio:.1f}x what our GPUs billed"
            if ratio > 1
            else f"{name} is {1 / ratio:.1f}x cheaper"
        )
    else:
        verdict = "insufficient data"
    return {
        "model": name,
        "provider": target["provider"],
        "same_mix_cost_usd": round(same_mix, 4),
        "same_mix_cost_cached_input_usd": round(same_mix_cached, 4),
        "multiplier": round(same_mix / actual_usd, 2) if actual_usd > 0 else None,
        "selfhost_difference_usd": round(actual_usd - same_mix, 4),
        "verdict": verdict,
    }


def cmd_cost_compare(args: argparse.Namespace) -> int:
    """Compare self-host serving cost (real Modal metered ÷ ledger tokens) against external API pricing.

    Token counts come from the usage ledger via the dashboard; self-host dollar cost
    comes from Modal's real billing (metered month-to-date), attributed per model by
    each model's share of total ledger tokens.
    """
    by_id = _rate_cards(args.rates)
    response = httpx.get(f"{_dashboard_url()}/_dashboard/api/stats", headers=_dashboard_headers(), timeout=30)
    response.raise_for_status()
    data = response.json()
    events = [event for event in data.get("events", []) if isinstance(event, dict)]
    metered = data.get("usage", {}).get("metered_month_to_date_usd")
    if metered is None:
        raise SystemExit("dashboard has no metered cost (billing unavailable); cannot compare")

    events_by_alias: dict[str, list[dict[str, object]]] = {}
    for event in events:
        alias = str(event.get("model") or "")
        if alias:
            events_by_alias.setdefault(alias, []).append(event)
    tokens_total = sum(int(e.get("prompt_tokens", 0) or 0) + int(e.get("completion_tokens", 0) or 0) for e in events)

    aliases = [args.alias] if args.alias else sorted(events_by_alias)
    output: dict[str, Any] = {"as_of": data.get("as_of"), "metered_month_to_date_usd": metered, "models": {}}
    for alias in aliases:
        output["models"][alias] = _cost_compare_row(
            alias, events_by_alias.get(alias, []), tokens_total, float(metered), args, by_id
        )

    print(json.dumps(output, indent=2))
    return 0


def _cost_compare_row(
    alias: str,
    rows: list[dict[str, object]],
    tokens_total: int,
    metered: float,
    args: argparse.Namespace,
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the comparison report for one model alias."""
    prompt = sum(int(event.get("prompt_tokens", 0) or 0) for event in rows)
    completion = sum(int(event.get("completion_tokens", 0) or 0) for event in rows)
    if prompt + completion == 0:
        return {"error": "no token data recorded"}
    share = (prompt + completion) / tokens_total if tokens_total else 0.0
    actual_usd = metered * share
    row: dict[str, Any] = {
        "ledger": {
            "requests": len(rows),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "actual_gpu_cost_usd": round(actual_usd, 4),
            "actual_gpu_cost_basis": "token-count share of the App's real Modal metered cost (boot + idle included)",
        },
    }
    if args.provider_model:
        target = by_id.get(args.provider_model)
        if not target:
            raise SystemExit(f"{args.provider_model!r} not in rate cards ({sorted(by_id)[:20]}...)")
        row["external"] = _external_row(target, prompt, completion, actual_usd)
    return row


def _add_stats_pricing_commands(sub: argparse._SubParsersAction) -> None:
    stats = sub.add_parser("stats", help="show authenticated server-side request usage")
    stats.add_argument("--json", action="store_true", help="include raw redacted ledger events")
    stats.set_defaults(func=cmd_stats)
    pricing = sub.add_parser("pricing", help="compare Modal GPU cost with a token-priced provider")
    pricing.add_argument(
        "--modal-gpu-hourly-usd",
        type=float,
        default=0.0,
        help="deprecated/unused; Modal cost now comes from the real billing API via the dashboard",
    )
    pricing.add_argument("--provider-input-usd-per-million", type=float, required=True)
    pricing.add_argument("--provider-output-usd-per-million", type=float, required=True)
    pricing.set_defaults(func=cmd_pricing)
    cost = sub.add_parser("cost", help="self-host vs external-API cost comparison (uses rate cards)")
    cost_sub = cost.add_subparsers(dest="cost_command", required=True)
    compare = cost_sub.add_parser("compare", help="join usage ledger with server/results/external-rate-cards.json")
    compare.add_argument("--alias", help="limit to one model alias (default: all with token data)")
    compare.add_argument("--provider-model", help="external model id, e.g. claude-sonnet-5-5 or gemini-2.5-flash")
    compare.add_argument(
        "--rates", help="path to rate-card JSON (default: <repo>/server/results/external-rate-cards.json)"
    )
    compare.set_defaults(func=cmd_cost_compare)


def _catalog() -> dict[str, Any]:
    return json.loads(_catalog_path().read_text())


def _write_catalog(catalog: dict[str, Any]) -> None:
    _catalog_path().write_text(json.dumps(catalog, indent=2, sort_keys=False) + "\n")


def _auto_install() -> None:
    """Best-effort provider refresh after a catalog mutation.

    Keeps the global Pi / OMP provider entries in step with the catalog without
    a manual provider-install run.
    """
    from install_provider import _pi_agent_dir, install

    catalog_path = _catalog_path()
    config = _config()
    base_url = str(config.get("base_url") or "")
    token = str(config.get("token") or "")
    if not base_url or not token:
        print(json.dumps({"auto_install": "skipped (base_url/token unset in config)"}))
        return
    try:
        install(base_url, token, catalog_path, pi_agent_dir=_pi_agent_dir(""))
    except SystemExit as exc:  # install raises on invalid catalogs
        print(json.dumps({"auto_install": f"global failed: {exc}"}))
    except Exception as exc:  # noqa: BLE001 - CLI best-effort  # defensive: never fail the mutation  # noqa: BLE001
        print(json.dumps({"auto_install": f"global failed: {type(exc).__name__}: {str(exc)[:120]}"}))


def _enabled_aliases(catalog: dict[str, Any]) -> list[str]:
    return [name for name, p in catalog.get("models", {}).items() if isinstance(p, dict) and p.get("enabled")]


def _enabled_serve_targets(catalog: dict[str, Any]) -> list[str]:
    """Every name `modal-inference use` accepts: enabled aliases, then serve groups."""
    groups = catalog.get("serveGroups")
    names = list(groups) if isinstance(groups, dict) else []
    return _enabled_aliases(catalog) + names


def _group_view(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Synthesize an alias-shaped view of a serve group for CLI consumers.

    Group tuning is container-global, so the view reports the GROUP's numbers
    (GPU shape, context) rather than any one member's: that is what actually
    gets served. Returns None when `name` is not a group.
    """
    groups = catalog.get("serveGroups")
    group = groups.get(name) if isinstance(groups, dict) else None
    if not isinstance(group, dict):
        return None
    aliases = group.get("aliases") or []
    members = [catalog.get("models", {}).get(str(a), {}) for a in aliases]
    members = [m for m in members if isinstance(m, dict)]
    missing = [str(a) for a in aliases if not isinstance(catalog.get("models", {}).get(str(a)), dict)]
    if missing:
        raise SystemExit(f"serve group {name!r} references unknown aliases: {missing}")
    disabled = [str(a) for a, m in zip(aliases, members, strict=False) if not m.get("enabled")]
    if disabled:
        raise SystemExit(f"serve group {name!r} includes disabled aliases: {disabled}")
    tuning = group.get("tuning") if isinstance(group.get("tuning"), dict) else {}
    first = members[0] if members else {}
    return {
        "model": first.get("model", ""),
        "revision": first.get("revision", ""),
        "runtime": first.get("runtime", "vllm"),
        "gpu": group.get("gpu", "H100"),
        "gpuCount": group.get("gpuCount", 1),
        "maxContextTokens": tuning.get("contextTokens", first.get("maxContextTokens", 32768)),
        "enabled": True,
        "status": "group",
        "is_serve_group": True,
        "members": [str(a) for a in aliases],
        "target_tuning": dict(tuning),
        "activeTuning": group.get("activeTuning", "baseline"),
        "tuning": {group.get("activeTuning", "baseline"): dict(tuning)},
    }


def _resolve_alias(requested: str | None) -> tuple[str, dict[str, Any]]:
    """Serve-target precedence: CLI arg > MODEL_PROFILE env > last deployed name.

    Accepts an enabled alias OR a serve group name; both come back as a
    profile-shaped mapping so every existing caller keeps working unchanged.
    """
    catalog = _catalog()
    name = (requested or os.getenv("MODEL_PROFILE", "") or str(_config().get("deployed", ""))).strip()
    profile = catalog.get("models", {}).get(name) if name else None
    if isinstance(profile, dict) and profile.get("enabled"):
        return name, profile
    group_view = _group_view(catalog, name) if name else None
    if group_view is not None:
        return name, group_view
    raise SystemExit(
        f"serve target {name or '<none>'!r} is missing or disabled; "
        f"enabled: {_enabled_serve_targets(catalog) or 'none'}"
    )


# --- setup --------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Write the user config from the repo's .env (or flags) so plain `modal-inference …` works from any shell."""
    repo = Path(args.repo or Path.cwd()).resolve()
    if not (repo / "server" / "models.json").exists():
        raise SystemExit(f"{repo} has no server/models.json; run from the repo or pass --repo")
    env: dict[str, str] = {}
    dotenv = repo / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                env[key.strip()] = value.strip()
    config = _config()
    config["repo"] = str(repo)
    config["base_url"] = (args.base_url or env.get("MODAL_BASE_URL") or config.get("base_url", "")).rstrip("/")
    config["token"] = args.token or env.get("MODAL_PROXY_TOKEN") or config.get("token", "")
    config.setdefault("deployed", env.get("MODEL_PROFILE", ""))
    dashboard_url = args.dashboard_url or env.get("MODAL_INFERENCE_DASHBOARD_URL") or config.get("dashboard_url", "")
    if not dashboard_url and config["base_url"]:
        # Modal's predictable web endpoint naming: "<app>--<function>.modal.run".
        # The dashboard is a separate Function ("dashboard") in the same App as the
        # inference proxy ("vllmserver-web"); derive one from the other as a default.
        dashboard_url = config["base_url"].replace("-vllmserver-web.modal.run", "-dashboard.modal.run")
    config["dashboard_url"] = dashboard_url.rstrip("/")
    config["dashboard_token"] = (
        args.dashboard_token or env.get("MODAL_INFERENCE_DASHBOARD_TOKEN") or config.get("dashboard_token", "")
    )
    missing = [k for k in ("base_url", "token") if not config[k]]
    if missing:
        raise SystemExit(f"missing {missing}; pass --base-url/--token or put them in {dotenv}")
    _write_config(config)
    print(json.dumps({"ok": True, "config": str(CONFIG_PATH), "repo": config["repo"], "base_url": config["base_url"]}))
    return 0


# --- health -------------------------------------------------------------------


def cmd_health(_args: argparse.Namespace) -> int:
    started = time.perf_counter()
    try:
        response = httpx.get(f"{_base_url()}{HEALTH_PATH}", headers=_headers(), timeout=15)
    except httpx.HTTPError as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"http-error: {exc}", "elapsed_seconds": round(time.perf_counter() - started, 3)}
            )
        )
        return 2
    elapsed = round(time.perf_counter() - started, 3)
    out = {"ok": response.status_code == 200, "status_code": response.status_code, "elapsed_seconds": elapsed}
    if response.status_code == 503:
        out["note"] = "server scaled to zero or starting"
    elif response.status_code != 200:
        out["body"] = response.text[:300]
    print(json.dumps(out))
    return 0 if out["ok"] else 1


# --- status -------------------------------------------------------------------

_METRIC_PREFIXES = (
    "vllm:num_requests_",
    "vllm:prompt_tokens_",
    "vllm:generation_tokens_",
    "vllm:request_success_",
    "vllm:request_prompt_tokens",
    "vllm:request_generation_tokens",
    "vllm:gpu_cache_usage_perc",
    "vllm:cpu_cache_usage_perc",
    "vllm:cache_queries",
    "vllm:cache_hits",
    "vllm:time_to_first_token",
    "vllm:time_per_output_token",
    "vllm:e2e_request_latency",
)


def _fetch_metrics() -> dict[str, float]:
    try:
        response = httpx.get(f"{_base_url()}/metrics", headers=_headers(), timeout=15)
    except httpx.HTTPError:
        return {}
    if response.status_code != 200:
        return {}
    metrics: dict[str, float] = {}
    for line in response.text.splitlines():
        if not line.startswith("#") and " " in line:
            name, _, value = line.partition(" ")
            try:
                metrics[name] = float(value)
            except ValueError:
                continue
    return metrics


def cmd_status(args: argparse.Namespace) -> int:
    profile_name, profile = _resolve_alias(args.alias)
    members = profile.get("members") if isinstance(profile.get("members"), list) else []
    out: dict[str, Any] = {
        "model": profile_name,
        "hf_repo": profile.get("model"),
        "revision": str(profile.get("revision", ""))[:12],
        "max_context": profile.get("maxContextTokens"),
    }
    if profile.get("is_serve_group"):
        # Co-resident target: report every member: a group that silently
        # serves only one of two models is the failure this surfaces.
        out["serve_group"] = {"members": [str(m) for m in members]}
    health_started = time.perf_counter()
    try:
        health = httpx.get(f"{_base_url()}{HEALTH_PATH}", headers=_headers(), timeout=15)
        out["health"] = {
            "status_code": health.status_code,
            "ok": health.status_code == 200,
            "elapsed_seconds": round(time.perf_counter() - health_started, 3),
        }
    except httpx.HTTPError as exc:
        out["health"] = {"ok": False, "error": str(exc)[:150]}
    try:
        models = httpx.get(f"{_base_url()}/v1/models", headers=_headers(), timeout=15)
        out["served_models"] = (
            [m.get("id") for m in models.json().get("data", [])] if models.status_code == 200 else None
        )
    except (httpx.HTTPError, ValueError):
        out["served_models"] = None
    # Residency: which models are actually loaded in VRAM right now. This is
    # the co-residency assertion: a group is healthy only when every member
    # it preloads appears here together.
    try:
        ps = httpx.get(f"{_base_url()}/api/ps", headers=_headers(), timeout=15)
        if ps.status_code == 200:
            resident = [
                {
                    "name": str(m.get("name", "")).split(":")[0],
                    "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1),
                }
                for m in ps.json().get("models", [])
            ]
            out["resident"] = resident
            if members:
                resident_names = {r["name"] for r in resident}
                out["members_resident"] = {str(m): (str(m) in resident_names) for m in members}
        else:
            out["resident"] = None
    except (httpx.HTTPError, ValueError):
        out["resident"] = None
    metrics = _fetch_metrics()
    if metrics:
        wanted = {name: value for name, value in metrics.items() if any(name.startswith(p) for p in _METRIC_PREFIXES)}
        out["engine_metrics"] = dict(sorted(wanted.items()))
    else:
        out["engine_metrics"] = None
        out["engine_metrics_note"] = "scaled to zero or metrics unavailable"
    print(json.dumps(out, indent=2))
    return 0 if out["health"].get("ok") else 1


# --- warm ---------------------------------------------------------------------


def _deploy(alias: str) -> None:
    command = ["uv", "run", "modal", "deploy", "server/modal_service.py"]
    started = time.perf_counter()
    result = subprocess.run(  # noqa: PLW1510 - returncode inspected manually
        command,
        cwd=_repo_root(),
        env={**os.environ, "MODEL_PROFILE": alias},
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise SystemExit("deploy failed:\n" + "\n".join(detail[-15:]))
    config = _config()
    config["deployed"] = alias
    _write_config(config)
    print(json.dumps({"event": "deployed", "model": alias, "elapsed_seconds": round(time.perf_counter() - started, 1)}))


def cmd_deploy(args: argparse.Namespace) -> int:
    alias, _profile = _resolve_alias(args.alias)
    _deploy(alias)
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    """Switch the service to an alias: stop the old server, deploy if needed, warm."""
    alias, _profile = _resolve_alias(args.alias)
    deployed = str(_config().get("deployed", ""))
    if deployed != alias or args.redeploy:
        if deployed:
            subprocess.run(["uv", "run", "modal", "app", "stop", "-y", APP_NAME], cwd=_repo_root(), capture_output=True, check=False)
        _deploy(alias)
    return cmd_warm(argparse.Namespace(alias=alias, timeout=args.timeout, wait=args.wait))


def cmd_warm(args: argparse.Namespace) -> int:
    alias, profile = _resolve_alias(args.alias)
    deployed = str(_config().get("deployed", ""))
    if deployed and deployed != alias:
        raise SystemExit(
            f"{deployed!r} is deployed, not {alias!r}; run `modal-inference use {alias}` to switch"
        )
    url = _base_url()
    gpu = f"{profile.get('gpu', 'H100')}x{profile.get('gpuCount', 1)}"
    print(json.dumps({"event": "warm-start", "model": alias, "gpu": gpu, "timeout_seconds": args.timeout}))

    # `modal app stop` (modal-inference shutdown) unregisters the deployment; put it back before warming.
    import modal

    try:
        modal.App.lookup(APP_NAME)
    except modal.exception.NotFoundError:
        _deploy(alias)

    # 1. One request: already warm (200), or triggers the cold container.
    try:
        response = httpx.get(f"{url}{HEALTH_PATH}", headers=_headers(), timeout=15)
    except httpx.HTTPError:
        response = None
    if response is not None and response.status_code == 200:
        print(json.dumps({"event": "warm-ok", "attempts": 1, "already_warm": True}))
        return 0

    # 2. Cold path: the barrier call must be held by a *live client* or Modal
    #    cancels the container ("client connection closed before llama-server
    #    finished loading"). So run it in a detached background process that
    #    keeps its Modal RPC open, and poll /v1/models for readiness.
    try:
        modal.Cls.from_name(APP_NAME, "VLLMServer")
    except Exception as exc:  # noqa: BLE001 - CLI best-effort
        print(json.dumps({"event": "warm-failed", "error": str(exc)[:400], "note": "check modal app logs"}))
        return 1
    subprocess.Popen(
        [sys.executable, "-c", _BARRIER_SNIPPET, APP_NAME, str(args.timeout)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=Path(__file__).resolve().parent,
    )
    if not args.wait:
        print(
            json.dumps(
                {
                    "event": "warm-spawned",
                    "note": "booting in the background (~5-6 min); `modal-inference status` shows health",
                }
            )
        )
        return 0

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}{HEALTH_PATH}", headers=_headers(), timeout=10).status_code == 200:
                print(json.dumps({"event": "warm-ok", "lifecycle": "serving"}))
                return 0
        except httpx.HTTPError:
            pass
        time.sleep(5)
    print(json.dumps({"event": "warm-timeout", "timeout_seconds": args.timeout, "note": "check modal app logs"}))
    return 1


# --- shutdown -----------------------------------------------------------------


def cmd_shutdown(args: argparse.Namespace) -> int:
    """Scale the GPU worker to zero immediately WITHOUT stopping the dashboard.

    `modal app stop` tears down the whole App (dashboard included). Instead,
    update the VLLMServer class's autoscaler to min_containers=0 +
    scaledown_window=0: idle containers retire at once while the dashboard
    Function (separate autoscaler) keeps serving. `--full-app` still opts
    into the old whole-App stop.
    """
    if args.full_app:
        command = ["uv", "run", "modal", "app", "stop", "-y", args.app or APP_NAME]
        started = time.perf_counter()
        result = subprocess.run(command, cwd=_repo_root(), capture_output=True, text=True, timeout=120, check=False)
        elapsed = round(time.perf_counter() - started, 2)
        ok = result.returncode == 0
        print(json.dumps({"ok": ok, "mode": "full-app", "command": " ".join(command), "elapsed_seconds": elapsed}))
        if not ok:
            print("\n".join((result.stderr or result.stdout).strip().splitlines()[-10:]), file=sys.stderr)
        return 0 if ok else 1

    started = time.perf_counter()
    if args.full_app:
        command = ["uv", "run", "modal", "app", "stop", "-y", args.app or APP_NAME]
        result = subprocess.run(command, cwd=_repo_root(), capture_output=True, text=True, timeout=120, check=False)
        elapsed = round(time.perf_counter() - started, 2)
        ok = result.returncode == 0
        print(json.dumps({"ok": ok, "mode": "full-app", "command": " ".join(command), "elapsed_seconds": elapsed}))
        if not ok:
            print("\n".join((result.stderr or result.stdout).strip().splitlines()[-10:]), file=sys.stderr)
        return 0 if ok else 1

    # GPU-only: delegate to the clean-interpreter entrypoint (own event loop; the
    # dashboard Function is CPU-only and untouched).
    command = ["uv", "run", "modal", "run", "server/modal_service.py::gpu_stop_eager"]
    try:
        result = subprocess.run(command, cwd=_repo_root(), capture_output=True, text=True, timeout=300, check=False)
        elapsed = round(time.perf_counter() - started, 2)
        # The entrypoint's JSON is one line among modal run's banner output
        # ("✓ App completed. View run at ..."), so scan for it instead of
        # trusting the last line: that broke with Modal SDK 1.5.5 and made
        # every shutdown report a JSONDecodeError.
        payload: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(stripped)
                    break
        ok = result.returncode == 0 and payload.get("ok") is True
        print(
            json.dumps(
                {
                    "ok": ok,
                    "mode": "gpu-only",
                    "target": f"{APP_NAME}.VLLMServer",
                    "containers_stopped": payload.get("containers_stopped", []),
                    "detail": payload.get("detail")
                    or "GPU containers retired; dashboard Function unaffected. Next warm auto-recovers (MAX_CONTAINERS=1 at deploy).",
                    "elapsed_seconds": elapsed,
                }
            )
        )
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 - CLI best-effort
        print(json.dumps({"ok": False, "mode": "gpu-only", "error": str(exc)[:300]}))
        return 1


# --- bootstrap ----------------------------------------------------------------
# Invoked through the deployed app, so the weight download runs server-side
# and a local client disconnect can never cancel a multi-minute bootstrap.


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Kick off bootstrap_model(alias) inside the deployed app and detach."""
    try:
        import modal
    except ImportError as exc:
        raise SystemExit("modal SDK not installed; run `uv add modal`") from exc
    alias = args.alias
    catalog = _catalog()
    profile = catalog.get("models", {}).get(alias)
    if not isinstance(profile, dict):
        raise SystemExit(f"alias {alias!r} not found in {_catalog_path()}")
    if not profile.get("enabled"):
        raise SystemExit(
            f"alias {alias!r} is disabled; enable it with `modal-inference models enable {alias}` first"
        )
    runtime = profile.get("runtime", "vllm")
    if runtime == "vllm" and not (isinstance(profile.get("revision"), str) and _SHA_RE.fullmatch(profile["revision"])):
        raise SystemExit(f"alias {alias!r} must have a full 40-char revision before bootstrap")
    try:
        func = modal.Function.from_name(APP_NAME, "bootstrap_model")
        handle = func.spawn(alias)
        print(
            json.dumps(
                {
                    "ok": True,
                    "alias": alias,
                    "app": APP_NAME,
                    "function_call_id": getattr(handle, "function_call_id", None),
                    "note": "bootstrap runs server-side; watch with `uv run modal app logs <app>`",
                }
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI best-effort
        print(json.dumps({"ok": False, "error": str(exc)[:400]}))
        return 1


# --- models -------------------------------------------------------------------


def _validate_revision(alias: str, runtime: str, revision: object) -> None:
    if revision is None:
        return
    pattern, expected = (
        (_DIGEST_RE, "a 64-hex ollama manifest digest")
        if runtime in ("ollama", "llama")
        else (_SHA_RE, "a full 40-char commit SHA")
    )
    if not isinstance(revision, str) or not pattern.fullmatch(revision):
        raise SystemExit(f"profile {alias!r} --revision must be {expected}")


def _validate_profile(alias: str, profile: dict[str, Any], *, partial: bool) -> None:
    if not alias or alias != alias.strip() or any(ch.isspace() for ch in alias):
        raise SystemExit(f"invalid alias {alias!r}: no whitespace allowed")
    if not partial and not profile.get("enabled", True):
        raise SystemExit("new profiles must be enabled; use `models disable` to store a disabled one")
    repository = profile.get("model")
    if not isinstance(repository, str) or not repository.strip():
        raise SystemExit(f"profile {alias!r} requires --model REPO/NAME")
    runtime = profile.get("runtime", "vllm")
    if runtime not in ("vllm", "ollama", "llama"):
        raise SystemExit(f"profile {alias!r} --runtime must be vllm, ollama, or llama")
    _validate_revision(alias, runtime, profile.get("revision"))
    gpu_count = profile.get("gpuCount", 1)
    if not isinstance(gpu_count, int) or gpu_count < 1:
        raise SystemExit(f"profile {alias!r} gpuCount must be a positive integer")
    if not isinstance(profile.get("maxContextTokens", 32768), int):
        raise SystemExit(f"profile {alias!r} maxContextTokens must be an integer")
    vllm_args = profile.get("vllmArgs", [])
    if not isinstance(vllm_args, list) or not all(isinstance(item, str) for item in vllm_args):
        raise SystemExit(f"profile {alias!r} vllmArgs must be a list of strings")


def _get_profile(alias: str) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = _catalog()
    profile = catalog.get("models", {}).get(alias)
    if not isinstance(profile, dict):
        raise SystemExit(f"alias {alias!r} not found in {_catalog_path()}")
    return catalog, profile


def cmd_tuning_list(args: argparse.Namespace) -> int:
    _, profile = _get_profile(args.alias)
    tuning = profile.get("tuning", {})
    if not isinstance(tuning, dict):
        tuning = {}
    active = profile.get("activeTuning", "baseline")
    print(
        json.dumps(
            {
                "alias": args.alias,
                "active": active,
                "profiles": {name: cfg for name, cfg in tuning.items() if isinstance(cfg, dict)},
            },
            indent=2,
        )
    )
    return 0


def cmd_tuning_show(args: argparse.Namespace) -> int:
    _, profile = _get_profile(args.alias)
    tuning = profile.get("tuning", {})
    cfg = tuning.get(args.profile) if isinstance(tuning, dict) else None
    if not isinstance(cfg, dict):
        raise SystemExit(f"tuning profile {args.profile!r} not found for alias {args.alias!r}")
    print(json.dumps({"alias": args.alias, "profile": args.profile, "config": cfg}, indent=2))
    return 0


def cmd_tuning_add(args: argparse.Namespace) -> int:
    """Add or update a named tuning profile. Additive only: never changes activeTuning."""
    catalog, profile = _get_profile(args.alias)
    tuning = profile.setdefault("tuning", {})
    if not isinstance(tuning, dict):
        raise SystemExit(f"alias {args.alias!r} has a malformed tuning block in {_catalog_path()}")
    base_name = args.copy_from or profile.get("activeTuning", "baseline")
    base = dict(tuning.get(base_name, {})) if isinstance(tuning.get(base_name), dict) else {}
    cfg = dict(tuning.get(args.profile, base)) if isinstance(tuning.get(args.profile), dict) else dict(base)
    if args.context_tokens is not None:
        cfg["contextTokens"] = args.context_tokens
    if args.parallel is not None:
        cfg["numParallel"] = args.parallel
    if args.batch is not None:
        cfg["batch"] = args.batch
    if args.ubatch is not None:
        cfg["ubatch"] = args.ubatch
    if args.kv_cache_type is not None:
        cfg["kvCacheType"] = args.kv_cache_type
    if args.cache_reuse is not None:
        cfg["cacheReuse"] = args.cache_reuse
    if args.kv_unified_per_slot is not None:
        cfg["kvUnifiedPerSlot"] = args.kv_unified_per_slot
    if args.slot_prompt_similarity is not None:
        cfg["slotPromptSimilarity"] = args.slot_prompt_similarity
    if args.swa_checkpoints is not None:
        cfg["swaCheckpoints"] = args.swa_checkpoints
    if args.cache_idle_slots is not None:
        cfg["cacheIdleSlots"] = args.cache_idle_slots
    required = ("contextTokens", "numParallel", "batch", "ubatch", "kvCacheType", "cacheReuse")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise SystemExit(f"tuning profile {args.profile!r} is missing {missing}; pass the corresponding flags")
    tuning[args.profile] = cfg
    _write_catalog(catalog)
    _auto_install()
    print(
        json.dumps(
            {
                "ok": True,
                "alias": args.alias,
                "profile": args.profile,
                "config": cfg,
                "active": profile.get("activeTuning", "baseline"),
                "note": (
                    "not activated; run `modal-inference tuning activate` to make this the live profile "
                    "on next redeploy"
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_tuning_activate(args: argparse.Namespace) -> int:
    catalog, profile = _get_profile(args.alias)
    tuning = profile.get("tuning", {})
    if not isinstance(tuning, dict) or args.profile not in tuning:
        raise SystemExit(f"tuning profile {args.profile!r} not found for alias {args.alias!r}; add it first")
    previous = profile.get("activeTuning", "baseline")
    profile["activeTuning"] = args.profile
    _write_catalog(catalog)
    _auto_install()
    deployed = str(_config().get("deployed", ""))
    print(
        json.dumps(
            {
                "ok": True,
                "alias": args.alias,
                "active_tuning": args.profile,
                "previous_tuning": previous,
                "redeploy_required": deployed == args.alias,
                "note": (
                    f"redeploy {args.alias!r} for this to take effect on the live worker: "
                    f"MODEL_PROFILE={args.alias} uv run modal deploy server/modal_service.py"
                    if deployed == args.alias
                    else "this alias is not currently deployed; the new profile takes effect on its next deploy"
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_models_list(args: argparse.Namespace) -> int:
    catalog = _catalog()
    rows = []
    for alias, profile in catalog.get("models", {}).items():
        rows.append(
            {
                "alias": alias,
                "model": profile.get("model"),
                "runtime": profile.get("runtime", "vllm"),
                "revision": (profile.get("revision") or "")[:12],
                "gpu": f"{profile.get('gpu', 'H100')}x{profile.get('gpuCount', 1)}",
                "maxContext": profile.get("maxContextTokens"),
                "enabled": bool(profile.get("enabled")),
                "status": profile.get("status", ""),
            }
        )
    out: dict[str, Any] = {"catalog_path": str(_catalog_path()), "models": rows}
    if args.remote:
        try:
            response = httpx.get(f"{_base_url()}/v1/models", headers=_headers(), timeout=15)
            if response.status_code == 200:
                remote = response.json().get("data", [])
                out["remote_models"] = [item.get("id") for item in remote]
            else:
                out["remote_error"] = f"status {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            out["remote_error"] = str(exc)[:200]
    print(json.dumps(out, indent=2))
    return 0


def cmd_models_enable_disable(alias: str, enabled: bool) -> int:
    catalog = _catalog()
    profile = catalog.get("models", {}).get(alias)
    if not isinstance(profile, dict):
        raise SystemExit(f"alias {alias!r} not found in {_catalog_path()}")
    profile["enabled"] = enabled
    _write_catalog(catalog)
    print(json.dumps({"ok": True, "alias": alias, "enabled": enabled}))
    _auto_install()
    return 0


def cmd_models_add_update(args: argparse.Namespace) -> int:
    catalog = _catalog()
    models = catalog.setdefault("models", {})
    existing = models.get(args.alias)
    profile: dict[str, Any] = (
        dict(existing)
        if isinstance(existing, dict)
        else {
            "enabled": True,
            "status": "registered",
            "gpu": "H100",
            "gpuCount": 1,
            "maxContextTokens": 32768,
            "vllmArgs": [],
        }
    )
    if args.model is not None:
        profile["model"] = args.model
    if args.runtime is not None:
        profile["runtime"] = args.runtime
    if args.revision is not None:
        profile["revision"] = args.revision
    if args.gpu is not None:
        profile["gpu"] = args.gpu
    if args.gpu_count is not None:
        profile["gpuCount"] = args.gpu_count
    if args.max_context is not None:
        profile["maxContextTokens"] = args.max_context
    if args.status is not None:
        profile["status"] = args.status
    if args.enable:
        profile["enabled"] = True
    if args.disable:
        profile["enabled"] = False
    if args.vllm_arg:
        extra = list(profile.get("vllmArgs", []))
        for item in args.vllm_arg:
            extra.extend(item.split())
        profile["vllmArgs"] = extra
    if args.clear_vllm_args:
        profile["vllmArgs"] = []
    if args.vllm_image is not None:
        profile["vllmImage"] = args.vllm_image
    _validate_profile(args.alias, profile, partial=existing is not None)
    models[args.alias] = profile
    _write_catalog(catalog)
    _auto_install()
    print(
        json.dumps(
            {"ok": True, "alias": args.alias, "action": "update" if existing else "add", "profile": profile}, indent=2
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lifecycle and catalog CLI for the modal-inference-server deployment.")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="write ~/.config/modal-inference/config.json from the repo .env (run once)")
    setup.add_argument("--repo", help="repo path (default: cwd)")
    setup.add_argument("--base-url")
    setup.add_argument("--token")
    setup.add_argument("--dashboard-url", help="dashboard Function URL (default: derived from --base-url)")
    setup.add_argument("--dashboard-token", help="dashboard bearer token")
    setup.set_defaults(func=cmd_setup)

    use = sub.add_parser("use", help="switch to an alias: deploy it if needed, then warm")
    use.add_argument("alias")
    use.add_argument("--redeploy", action="store_true", help="redeploy even if this alias is already deployed")
    use.add_argument("--wait", action="store_true", help="block until the model is serving (default: return at once)")
    use.add_argument("--timeout", type=int, default=2400, help="max seconds for --wait")
    use.set_defaults(func=cmd_use)

    deploy = sub.add_parser("deploy", help="deploy an alias without warming it")
    deploy.add_argument("alias", nargs="?")
    deploy.set_defaults(func=cmd_deploy)

    doctor = sub.add_parser(
        "doctor", help="health + drift report for the whole chain (config, deploy, heartbeat, installs, modelRoles)"
    )
    doctor.add_argument("--quick", action="store_true", help="skip remote checks (config + catalog only)")
    doctor.set_defaults(func=cmd_doctor)

    warm = sub.add_parser("warm", help="cold-start the deployed alias and wait until it is serving")
    warm.add_argument("alias", nargs="?")
    warm.add_argument("--wait", action="store_true", help="block until the model is serving (default: return at once)")
    warm.add_argument("--timeout", type=int, default=2400, help="max seconds for --wait")
    warm.set_defaults(func=cmd_warm)

    health = sub.add_parser("health", help="one-shot authenticated health probe")
    health.set_defaults(func=cmd_health)

    status = sub.add_parser("status", help="model profile, health, served models, and engine metrics snapshot")
    status.add_argument("alias", nargs="?")
    status.set_defaults(func=cmd_status)

    shutdown = sub.add_parser(
        "shutdown", help="scale the GPU worker to zero now (dashboard stays up; --full-app to stop everything)"
    )
    shutdown.add_argument("--app", default="", help=f"app name (default: {APP_NAME})")
    shutdown.add_argument(
        "--full-app", action="store_true", help="old behavior: modal app stop (takes the dashboard down too)"
    )

    _add_stats_pricing_commands(sub)
    shutdown.set_defaults(func=cmd_shutdown)

    bootstrap = sub.add_parser("bootstrap", help="kick off server-side model weight download for an enabled alias")
    bootstrap.add_argument("alias")
    bootstrap.set_defaults(func=cmd_bootstrap)

    models = sub.add_parser("models", help="inspect or edit the model catalog")
    models_sub = models.add_subparsers(dest="models_command", required=True)

    models_list = models_sub.add_parser("list", help="list catalog entries (and optionally the remote model list)")
    models_list.add_argument("--remote", action="store_true", help="also query /v1/models on the deployed service")
    models_list.set_defaults(func=cmd_models_list)

    add = models_sub.add_parser("add", help="add a new model profile")
    update = models_sub.add_parser("update", help="update an existing model profile")
    for target in (add, update):
        target.add_argument("alias")
        target.add_argument("--runtime", choices=("vllm", "ollama", "llama"), help="serving runtime (default vllm)")
        target.add_argument(
            "--model",
            help="Hugging Face repo (vllm), e.g. zai-org/GLM-5.3-Flash, or ollama tag (ollama; must equal the alias)",
        )
        target.add_argument(
            "--revision",
            help="full 40-char commit SHA (vllm) or 64-hex manifest digest (ollama, optional; recorded by bootstrap)",
        )
        target.add_argument("--gpu", help='GPU type, e.g. "H200"')
        target.add_argument("--gpu-count", type=int)
        target.add_argument("--max-context", type=int, help="max context tokens")
        target.add_argument("--status", help='free-form status, e.g. "benchmarking" or "approved"')
        target.add_argument("--vllm-arg", action="append", help="extra vLLM arg (repeatable; space-separated accepted)")
        target.add_argument("--clear-vllm-args", action="store_true")
        target.add_argument(
            "--vllm-image",
            help="Docker image reference with the matching vLLM build, e.g. vllm/vllm-openai:deepseekv41-flash-0909-amd64",
        )
        target.add_argument("--enable", action="store_true")
        target.add_argument("--disable", action="store_true")
        target.set_defaults(func=cmd_models_add_update)

    enable = models_sub.add_parser("enable", help="enable an alias")
    enable.add_argument("alias")
    enable.set_defaults(func=lambda a: cmd_models_enable_disable(a.alias, True))

    disable = models_sub.add_parser("disable", help="disable an alias")
    disable.add_argument("alias")
    disable.set_defaults(func=lambda a: cmd_models_enable_disable(a.alias, False))

    tuning = sub.add_parser("tuning", help="inspect or edit per-model tuning profiles (context/parallel/cache knobs)")
    tuning_sub = tuning.add_subparsers(dest="tuning_command", required=True)

    tuning_list = tuning_sub.add_parser("list", help="list tuning profiles for an alias and which is active")
    tuning_list.add_argument("alias")
    tuning_list.set_defaults(func=cmd_tuning_list)

    tuning_show = tuning_sub.add_parser("show", help="show one tuning profile's config")
    tuning_show.add_argument("alias")
    tuning_show.add_argument("profile")
    tuning_show.set_defaults(func=cmd_tuning_show)

    tuning_add = tuning_sub.add_parser(
        "add", help="add or update a named tuning profile (additive; never changes what's active)"
    )
    tuning_add.add_argument("alias")
    tuning_add.add_argument("profile")
    tuning_add.add_argument("--context-tokens", type=int, help="context length for this profile")
    tuning_add.add_argument("--parallel", type=int, help="concurrent request slots (numParallel)")
    tuning_add.add_argument("--batch", type=int, help="prompt batch size")
    tuning_add.add_argument("--ubatch", type=int, help="micro-batch size")
    tuning_add.add_argument("--kv-cache-type", help='KV-cache quantization, e.g. "q8_0" or "f16"')
    cache_group = tuning_add.add_mutually_exclusive_group()
    cache_group.add_argument("--cache-reuse", dest="cache_reuse", action="store_true", default=None)
    cache_group.add_argument("--no-cache-reuse", dest="cache_reuse", action="store_false")
    tuning_add.add_argument(
        "--kv-unified-per-slot", type=int, help="context limit per parallel slot (llama-server kv-unified-per-slot)"
    )
    tuning_add.add_argument(
        "--slot-prompt-similarity", type=float, help="slot routing similarity threshold (0 = any slot)"
    )
    tuning_add.add_argument("--swa-checkpoints", type=int, help="max context checkpoints per slot")
    idle_group = tuning_add.add_mutually_exclusive_group()
    idle_group.add_argument("--cache-idle-slots", dest="cache_idle_slots", action="store_true", default=None)
    idle_group.add_argument("--no-cache-idle-slots", dest="cache_idle_slots", action="store_false")
    tuning_add.add_argument(
        "--copy-from", help="seed unset fields from this existing profile (default: the current active profile)"
    )
    tuning_add.set_defaults(func=cmd_tuning_add)

    tuning_activate = tuning_sub.add_parser(
        "activate", help="set the active tuning profile (takes effect on that alias's next redeploy)"
    )
    tuning_activate.add_argument("alias")
    tuning_activate.add_argument("profile")
    tuning_activate.set_defaults(func=cmd_tuning_activate)

    compare = tuning_sub.add_parser("compare", help="compare real ledger metrics between two recorded tuning profiles")
    compare.add_argument("alias")
    compare.add_argument("profiles", nargs="+")
    compare.set_defaults(func=cmd_tuning_compare)

    flex = tuning_sub.add_parser("flex", help="live-flex tuning knobs without a redeploy (next GPU boot applies)")
    flex.add_argument("alias")
    flex.add_argument("--parallel", type=int, help="concurrent slots (numParallel)")
    flex.add_argument("--context-tokens", type=int, help="context tokens per slot")
    flex.add_argument("--batch", type=int, help="prompt batch size")
    flex.add_argument("--ubatch", type=int, help="micro-batch size")
    flex.add_argument(
        "--clear",
        action="store_true",
        help="remove this alias's overrides entirely (overrides silently beat the catalog at boot)",
    )
    flex.set_defaults(func=cmd_tuning_flex)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


def cmd_tuning_flex(args: argparse.Namespace) -> int:
    """Write runtime-overrides.json via the dashboard; next GPU boot applies them."""
    overrides: dict[str, object] = {}
    if args.parallel is not None:
        overrides["numParallel"] = args.parallel
    if args.context_tokens is not None:
        overrides["contextTokens"] = args.context_tokens
    if args.batch is not None:
        overrides["batch"] = args.batch
    if args.ubatch is not None:
        overrides["ubatch"] = args.ubatch
    if args.clear:
        overrides["clear"] = True
    if not overrides:
        raise SystemExit("nothing to flex: pass --parallel/--context-tokens/--batch/--ubatch, or --clear")
    response = httpx.post(
        f"{_dashboard_url()}/_dashboard/api/runtime-tuning",
        headers=_dashboard_headers(),
        json={"alias": args.alias, **overrides},
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"flex failed: {response.status_code} {response.text[:200]}")
    result = response.json()
    print(json.dumps(result, indent=2))
    print("cycle the GPU for it to apply:  modal-inference shutdown && modal-inference warm --wait")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def cmd_tuning_compare(args: argparse.Namespace) -> int:
    """Compare real request metrics between tuning profiles already recorded in the usage ledger."""
    response = httpx.get(
        f"{_dashboard_url()}/_dashboard/api/stats",
        headers=_dashboard_headers(),
        timeout=30,
    )
    response.raise_for_status()
    events = [e for e in response.json().get("events", []) if isinstance(e, dict) and e.get("model") == args.alias]

    def bucket(profile: str) -> dict[str, object]:
        rows = [e for e in events if e.get("tuning_profile") == profile and e.get("status") == 200]
        timed = [
            e
            for e in rows
            if isinstance(e.get("prompt_tokens"), int) and isinstance(e.get("elapsed_seconds"), (int, float))
        ]
        completion_rate = [
            e["completion_tokens"] / e["elapsed_seconds"]
            for e in timed
            if e.get("elapsed_seconds") and isinstance(e.get("completion_tokens"), int)
        ]
        prompt_sizes = sorted(e["prompt_tokens"] for e in timed)
        return {
            "requests": len(rows),
            "requests_with_token_data": len(timed),
            "avg_elapsed_seconds": round(sum(e["elapsed_seconds"] for e in timed) / len(timed), 2) if timed else None,
            "avg_completion_tokens_per_sec": round(sum(completion_rate) / len(completion_rate), 2)
            if completion_rate
            else None,
            "median_prompt_tokens": prompt_sizes[len(prompt_sizes) // 2] if prompt_sizes else None,
            "max_prompt_tokens": max(prompt_sizes) if prompt_sizes else None,
        }

    report = {profile: bucket(profile) for profile in args.profiles}
    print(json.dumps({"alias": args.alias, "profiles": report}, indent=2))
    return 0


def _doctor_check_config(add) -> None:
    """Config + deployed-target checks (both failure-critical)."""
    catalog = _catalog()
    config = _config()
    base_url = str(config.get("base_url") or "")
    add("ok" if base_url else "fail", "config.base_url", base_url or "(unset)")
    add("ok" if config.get("token") else "fail", "config.token", "set" if config.get("token") else "(unset)")
    deployed = str(config.get("deployed") or "")
    targets = _enabled_serve_targets(catalog)
    if not deployed:
        add("warn", "deployed-target", "(unset: nothing warmed yet)")
    elif deployed not in targets:
        add("fail", "deployed-target", f"deployed {deployed!r} is not an enabled alias or serve group")
    else:
        add("ok", "deployed-target", deployed)


def _doctor_check_heartbeat(add) -> None:
    """Serving-heartbeat freshness via the dashboard (stale/missing degrade to WARN)."""
    try:
        response = httpx.get(f"{_dashboard_url()}/_dashboard/api/stats", headers=_dashboard_headers(), timeout=15)
        response.raise_for_status()
        deployment = response.json().get("deployment", {})
        age = deployment.get("heartbeat_age_seconds")
        if age is None:
            add("warn", "serving-heartbeat", "no heartbeat file (never served or pre-heartbeat deploy)")
        elif float(age) > 90:
            add("ok", "serving-heartbeat", f"stale ({age}s old): GPUs stopped")
        else:
            add("ok", "serving-heartbeat", f"fresh ({age}s old): actively serving")
    except Exception as exc:  # noqa: BLE001 - CLI best-effort
        add("fail", "serving-heartbeat", f"dashboard unreachable: {type(exc).__name__}: {str(exc)[:80]}")


def _doctor_check_installs(add) -> None:
    """Provider-install drift across all surfaces (drift degrades to WARN)."""
    from install_provider import _check_omp, _check_pi, _enabled_profiles, _pi_agent_dir

    base_url = str(_config().get("base_url") or "")
    profiles = _enabled_profiles(_catalog_path())
    for label, agent_dir in [("pi-global", _pi_agent_dir(""))]:
        stale = not _check_pi(base_url, profiles, agent_dir)
        add(
            "warn" if stale else "ok",
            f"install.{label}",
            "stale → re-run the provider-install script" if stale else "ok",
        )
    omp_ok = (Path.home() / ".omp" / "agent" / "models.yml").exists() and _check_omp(base_url, profiles)
    add(
        "warn" if not omp_ok else "ok",
        "install.omp",
        "stale → re-run the provider-install script" if not omp_ok else "ok",
    )


def _doctor_check_model_roles(add) -> None:
    """OMP modelRoles entries pointing at disabled/missing modal aliases are FAIL-critical."""
    roles_path = Path.home() / ".omp" / "agent" / "config.yml"
    if not roles_path.exists():
        add("warn", "modelRoles", "(config.yml absent)")
        return
    try:
        import yaml

        roles = (yaml.safe_load(roles_path.read_text()) or {}).get("modelRoles", {})
        enabled = _enabled_aliases(_catalog())
        for role, value in roles.items():
            if not isinstance(value, str):
                continue
            parts = value.split(":")
            model_part = parts[-2] if len(parts) > 2 else parts[0]
            provider, _, alias = model_part.rpartition("/")
            if provider == "modal-inference" and alias not in enabled:
                add(
                    "fail", f"modelRoles.{role}", f"points at disabled/missing modal alias {alias!r} (value: {value!r})"
                )
            elif provider == PROVIDER_NAME and alias in enabled:
                add("ok", f"modelRoles.{role}", value)
    except Exception as exc:  # noqa: BLE001 - CLI best-effort
        add("warn", "modelRoles", f"unreadable: {type(exc).__name__}: {str(exc)[:80]}")


def _group_problems(group: object, aliases: object, models: dict[str, Any]) -> list[str]:
    """Why a serve group could not boot, as human-readable problems.

    Pure so the complexity stays low and each rule reads on its own line: a
    group that references a disabled alias, mixes runtimes, or preloads a
    non-member fails at container boot, which costs a GPU allocation to
    discover.
    """
    if not isinstance(group, dict):
        return ["not a mapping"]
    if not isinstance(aliases, list) or not aliases:
        return ["requires a non-empty aliases list"]
    problems: list[str] = []
    runtimes: set[str] = set()
    for alias in aliases:
        profile = models.get(str(alias))
        if not isinstance(profile, dict):
            problems.append(f"{alias}: unknown alias")
            continue
        if not profile.get("enabled"):
            problems.append(f"{alias}: disabled")
        runtimes.add(str(profile.get("runtime", "vllm")))
    if len(runtimes) > 1:
        problems.append(f"mixed runtimes {sorted(runtimes)}")
    if "vllm" in runtimes and len(aliases) > 1:
        problems.append("vllm groups are single-member only")
    preload = group.get("preload")
    if isinstance(preload, list):
        unknown = [str(a) for a in preload if str(a) not in {str(x) for x in aliases}]
        if unknown:
            problems.append(f"preload not in aliases: {unknown}")
    return problems


def _doctor_check_serve_groups(add) -> None:
    """Serve groups must be deployable; failures would surface at GPU boot."""
    groups = _catalog().get("serveGroups")
    if not isinstance(groups, dict) or not groups:
        return
    models = _catalog().get("models", {})
    for name, group in groups.items():
        aliases = group.get("aliases") if isinstance(group, dict) else None
        problems = _group_problems(group, aliases, models)
        if problems:
            add("fail", f"serveGroups.{name}", "; ".join(problems))
        else:
            add("ok", f"serveGroups.{name}", f"{len(aliases)} members: {', '.join(str(a) for a in aliases)}")


def cmd_doctor(args: argparse.Namespace) -> int:
    """One-command health + drift report for the whole chain.

    Checks: config completeness, deployed target vs catalog, serve-group
    integrity, serving heartbeat freshness, provider-install staleness on ALL
    surfaces (Pi global, OMP models.yml), and OMP modelRoles entries pointing
    at missing/disabled aliases. Exit 1 on any FAIL (script-friendly),
    0 otherwise; degraded items show as WARN without failing.
    """
    checks: list[dict[str, object]] = []

    def add(status: str, name: str, detail: str) -> None:
        checks.append({"status": status, "check": name, "detail": detail})

    quick = getattr(args, "quick", False)
    _doctor_check_config(add)
    _doctor_check_serve_groups(add)
    if not quick:
        _doctor_check_heartbeat(add)
        _doctor_check_installs(add)
        _doctor_check_model_roles(add)

    fails = [c for c in checks if c["status"] == "fail"]
    print(json.dumps({"result": "FAIL" if fails else "PASS", "checks": checks}, indent=2))
    return 1 if fails else 0
