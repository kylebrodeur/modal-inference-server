"""Dashboard ASGI app: auth/session, stats, billing snapshot, cost compare, runtime tuning.

Pure fastapi factory; Modal decoration stays in the deploy root.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

from modal_inference_catalog import _tuning_profile, resolve_serve_target
from modal_inference_cost_model import _archive_billing, _cost_compare_payload

try:
    from server.libs.hooks import Hooks as _DashboardHooks  # repo-relative spelling
except ImportError:  # flat Modal-image layout: libs/ ships as a sibling package
    from libs.hooks import Hooks as _DashboardHooks

# Lifecycle hooks for the dashboard surface (vendored seam in libs/hooks.py).
# Separate instance from modal_service's: the dashboard has its own lifecycle
# and must not import modal_service. Closed tag set (new tags = a release):
#   dashboard.boot.pre / dashboard.boot.post : around build_dashboard_api()
dashboard_hooks = _DashboardHooks(("dashboard.boot.pre", "dashboard.boot.post"), name="modal-inference-dashboard")

USAGE_DIR = "/usage"
USAGE_LEDGER_DIR = f"{USAGE_DIR}/events"
CATALOG_PATH = Path(os.getenv("MODEL_CATALOG_PATH", "/root/models.json"))


def _configured_members(resolved: dict[str, object] | None) -> list[str]:
    """The hot set the deployed target is CONFIGURED to serve, from the catalog.

    Distinct from the live `members` field (published by a running container's
    heartbeat, null at scale-to-zero): this is answerable at zero spend, which
    is when "what is in the hot seat?" is otherwise unanswerable.
    """
    members = (resolved or {}).get("members")
    if not isinstance(members, list):
        return []
    return [str(m.get("alias", "")) for m in members if isinstance(m, dict)]


GPU_HOURLY_RATES = {"H200": 2.35, "B200": 3.53, "H100": 2.10, "L40S": 1.10, "A10G": 0.60}
HEARTBEAT_LIVE_CUTOFF_SECONDS = 90


def _target_spec(alias: str, groups: dict[str, object], models: dict[str, object]) -> dict[str, object]:
    """GPU spec for a serve target: resolve via catalog, else a plain-profile fallback."""
    try:  # resolve_serve_target handles group AND alias names; raises on unknown ones
        resolved = resolve_serve_target(alias)
        return {
            "gpu": str(resolved.get("gpu", "H100")),
            "gpuCount": int(resolved.get("gpu_count", 1)),
            "runtime": str(resolved.get("runtime", "unknown")),
        }
    except (ValueError, KeyError, RuntimeError):
        profile = models.get(alias) if isinstance(models.get(alias), dict) else {}
        if isinstance(groups.get(alias), dict):
            profile = groups[alias]
        if not isinstance(profile, dict):
            profile = {}
        return {
            "gpu": str(profile.get("gpu", "H200")),
            "gpuCount": int(profile.get("gpuCount", 1) or 1),
            "runtime": str(profile.get("runtime", "unknown")),
        }


def _fleet_state_row(
    state: dict[str, object], groups: dict[str, object], models: dict[str, object]
) -> dict[str, object]:
    """Fleet row from one serving-state file (live truth for that container)."""
    cid = str(state.get("container_id") or "unknown")
    heartbeat = float(state.get("heartbeat", 0) or 0)
    age = time.time() - heartbeat
    live = bool(state.get("ok")) and 0 <= age < HEARTBEAT_LIVE_CUTOFF_SECONDS
    alias = str(state.get("alias") or "")
    spec = _target_spec(alias, groups, models)
    members = state.get("members")
    return {
        "container_id": cid,
        "alias": alias,
        "members": [str(m) for m in members] if isinstance(members, list) else [],
        "runtime": spec["runtime"],
        "gpu": spec["gpu"],
        "gpu_count": spec["gpuCount"],
        "status": "serving" if live else "stopped",
        "heartbeat_age_seconds": round(age, 1) if age >= 0 else None,
        "slots": state.get("slots") if live else None,
        "gate": state.get("gate") if live else None,
    }


def _fleet_registry_row(
    cid: str, reg: dict[str, object], groups: dict[str, object], models: dict[str, object]
) -> dict[str, object]:
    """Fleet row for a registered container with no readable state file (stopped)."""
    alias = str(reg.get("alias", ""))
    spec = _target_spec(alias, groups, models)
    members = reg.get("members")
    return {
        "container_id": cid,
        "alias": alias,
        "members": [str(m) for m in members] if isinstance(members, list) else [],
        "runtime": spec["runtime"],
        "gpu": spec["gpu"],
        "gpu_count": spec["gpuCount"],
        "status": "stopped",
        "registered_at": float(reg.get("registered_at", 0) or 0),
        "heartbeat_age_seconds": None,
        "slots": None,
        "gate": None,
    }


def _load_registry_12h(path: Path) -> dict[str, dict[str, object]]:
    """Last registration per container id from the boot registry, within 12h.

    Append-only jsonl: containers restart often, so keep only the newest row
    per id. Rows older than 12h are dropped (containers are ephemeral; long-
    dead boots are display noise). File errors and per-line JSON garbage
    (partial last line) are both suppressed: a registry is best-effort.
    """
    registry: dict[str, dict[str, object]] = {}
    with contextlib.suppress(OSError), path.open(encoding="utf-8") as handle:
        for line in handle:
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(line)
                if isinstance(value, dict) and value.get("container_id"):
                    cid = str(value["container_id"])
                    seen = float(value.get("registered_at", 0) or 0)
                    if seen >= time.time() - 12 * 3600:
                        prior = registry.get(cid)
                        if prior is None or seen >= float(prior.get("registered_at", 0) or 0):
                            registry[cid] = value
    return registry


def _gpu_fleet() -> dict[str, object]:
    """Every GPU lane the dashboard can see, LIVE or recently registered.

    Joins three Volume sources, each covering a different truth:
    - `serving-state-<cid>.json` (fresh heartbeat ≤90s ago) proves a container
      is alive RIGHT NOW and records its alias + slot/gate telemetry.
    - `gpu-containers.jsonl` (append-only boot registry) records what booted
      in the last 12h, including containers whose heartbeat is already stale
      (hard-killed, OOM): shown as "stopped".
    - `models.json` catalog maps a serve target (group OR alias) to its
      gpu/gpu_count/runtime, so each lane shows what hardware it holds and
      what holding it costs per hour.

    `always_on_usd_per_hour` is the arithmetic answer to "what does this
    posture cost per hour if nothing scales down": sum over ALIVE containers
    of gpu_count x the gpu's hourly rate. It is a burn rate, NOT a bill -
    real spend accrues in Modal's billing API (rendered by the billing and
    cost sections).
    """
    state_dir = Path(USAGE_DIR)
    catalog = json.loads(CATALOG_PATH.read_text()) if CATALOG_PATH.exists() else {}
    groups = catalog.get("serveGroups", {}) if isinstance(catalog, dict) else {}
    models = catalog.get("models", {}) if isinstance(catalog, dict) else {}
    registry = _load_registry_12h(state_dir / "gpu-containers.jsonl")
    fleet_rows: list[dict[str, object]] = []
    alive_gpus: dict[str, int] = {}
    seen_cids: set[str] = set()
    for state_path in sorted(state_dir.glob("serving-state-*.json")):
        with contextlib.suppress(json.JSONDecodeError, OSError):
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict):
                continue
            cid = str(state.get("container_id") or state_path.stem.removeprefix("serving-state-"))
            if not cid or cid == "unknown":
                continue
            heartbeat = float(state.get("heartbeat", 0) or 0)
            # The volume accumulates one state file per container boot for the
            # whole month; only recent boots are fleet candidates (same window
            # as the registry).
            if heartbeat and heartbeat < time.time() - 12 * 3600:
                continue
            seen_cids.add(cid)
            row = _fleet_state_row(state, groups, models)
            if str(row["status"]) == "serving":
                gpu = str(row["gpu"])
                alive_gpus[gpu] = alive_gpus.get(gpu, 0) + int(row["gpu_count"])
            fleet_rows.append(row)
    # Registrations for containers that never wrote a heartbeat file (or whose
    # state file was already pruned): show them, as stopped, so `modal-inference shutdown`
    # style postmortems stay possible from the dashboard alone.
    for cid, reg in registry.items():
        if cid not in seen_cids:
            fleet_rows.append(_fleet_registry_row(cid, reg, groups, models))
    always_on = round(sum(GPU_HOURLY_RATES.get(g, 2.35) * n for g, n in alive_gpus.items()), 2)
    # Serving first, then most-recently-registered stopped rows.
    fleet_rows.sort(
        key=lambda r: (
            0 if str(r["status"]) == "serving" else 1,
            -(float(r.get("registered_at", 0) or -float(r.get("heartbeat_age_seconds") or 0))),
        )
    )
    return {
        "rows": fleet_rows,
        "alive_gpu_counts": alive_gpus,
        "always_on_usd_per_hour": always_on,
        "gpus_billed_while_idle": (
            "Modal bills every ACTIVE container-second whether or not requests flow;"
            " scaledown_window (300s) resets on any request."
        ),
    }


def _dashboard_login_html() -> str:
    # Hash-fragment autologin: `#key=<credential>` in the bookmark URL is filled
    # and submitted by JS, then the fragment is stripped (it never reaches the
    # server; credentials should not land in browsing history via query params).
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inference Dashboard Login</title>
<style>
body{font:16px system-ui,sans-serif;max-width:28rem;margin:5rem auto;padding:0 1rem;background:#101218;color:#edf0f7}
main{padding:2rem;border:1px solid #303746;border-radius:10px;background:#171b24}
input,button{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin-top:.7rem}
button{cursor:pointer}
</style></head>
<body><main><h1>Inference Dashboard</h1><p>Sign in to view private runtime usage.</p>
<form method="post" action="/_dashboard/login"><label>Dashboard credential
<input name="credential" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form>
<script>
(function(){
  var m = /^#key=(.+)$/.exec(window.location.hash);
  if (!m) return;
  var input = document.querySelector('input[name=credential]');
  input.focus(); input.value = decodeURIComponent(m[1]);
  history.replaceState(null, '', window.location.pathname + window.location.search);
  document.querySelector('form').submit();
})();
</script>
</main></body></html>"""


def build_dashboard_api(usage_volume=None) -> FastAPI:
    """Construct the dashboard ASGI app (fastapi). Deploy root mounts it."""
    dashboard_hooks.fire("dashboard.boot.pre", {"profile": os.getenv("MODEL_PROFILE", "").strip()})
    deployed_profile = os.getenv("MODEL_PROFILE", "").strip()
    app_name = os.getenv("APP_NAME", "modal-inference-server")
    resolved = resolve_serve_target(deployed_profile) if deployed_profile else None
    dashboard_token = os.getenv("MODAL_INFERENCE_DASHBOARD_TOKEN", "").strip()
    cookie_name = "modal_inference_dashboard_session"
    session_max_age = 7 * 24 * 3600

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        yield

    api = FastAPI(title=f"{app_name} dashboard", lifespan=lifespan)

    _billing_cache: dict[str, object] = {
        "ts": 0.0,
        "workspace_disabled": False,
        "metered_month_usd": None,
        "metered_today_usd": None,
        "workspace_billed_month_usd": None,
        "workspace_credits_month_usd": None,
        "daily_breakdown": [],
        "resource_split": {},
        "hourly_rows": [],
        "error": None,
    }

    async def _billing_snapshot(force: bool = False) -> dict[str, object]:
        import datetime as _dt

        now = time.time()
        if (
            not force
            and now - float(_billing_cache["ts"] or 0) < 300
            and _billing_cache["metered_month_usd"] is not None
        ):
            return dict(_billing_cache)
        try:
            import modal

            workspace = modal.Workspace.from_context()
            month_start = _dt.datetime.now(_dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            today_start = _dt.datetime.now(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)

            def _ours(item: object) -> bool:
                # Prefer the explicit "project" tag (set on this App); fall back to an
                # exact description match for historical apps deployed before tagging.
                return item.tags.get("project") == app_name or (
                    "project" not in item.tags and item.description == app_name
                )

            daily_items = (
                await workspace.billing.report.aio(
                    start=month_start, end=today_start, resolution="d", tag_names=["project"]
                )
                if today_start > month_start
                else []
            )
            # Hourly resolution is capped at 7-day spans; serving era < 7 days,
            # but degrade to 7 days max so long-lived deployments keep working.
            hourly_start = max(month_start, _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7) + _dt.timedelta(hours=1))
            hourly_items = await workspace.billing.report.aio(start=hourly_start, resolution="h", tag_names=["project"])
            summary = await workspace.billing.summary.aio()

            hourly_ours = [item for item in hourly_items if _ours(item)]
            today_usd = sum(float(item.cost) for item in hourly_ours if item.interval_start >= today_start)
            # Daily rows cover month_start..today_start; hourly rows only cover
            # the last 7 days: overlap daily rows that fall inside the hourly
            # window (before today) are the same dollars, so take each dollar once.
            hourly_cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7) + _dt.timedelta(hours=1)
            month_usd = sum(
                float(item.cost) for item in daily_items if _ours(item) and item.interval_start < hourly_cutoff
            ) + sum(float(item.cost) for item in hourly_ours)
            # Where the money went: per-day + per-GPU-resource split of the app's
            # own metered rows (serving vs experiments can't be separated in the
            # billing API, but the daily shape + resource mix shows it).
            daily_breakdown: list[dict[str, object]] = []
            resource_split: dict[str, float] = {}
            for item in daily_items:
                if not _ours(item):
                    continue
                day_resources = {}
                for resource, cost in (item.cost_by_resource or {}).items():
                    cost_f = float(cost)
                    if cost_f <= 0:
                        continue
                    day_resources[resource] = round(cost_f, 4)
                    resource_split[resource] = resource_split.get(resource, 0.0) + cost_f
                if day_resources:
                    daily_breakdown.append({"day": str(item.interval_start)[:10], "resources": day_resources})
            resource_split = {k: round(v, 2) for k, v in sorted(resource_split.items(), key=lambda kv: -kv[1])}
            hourly_rows = [{"hour": item.interval_start.timestamp(), "usd": float(item.cost)} for item in hourly_ours]
            _archive_billing([item for item in daily_items if _ours(item)], hourly_ours)
            _billing_cache.update(
                ts=now,
                metered_month_usd=month_usd,
                metered_today_usd=today_usd,
                workspace_billed_month_usd=float(summary.billed_cost),
                workspace_credits_month_usd=float(-summary.adjustments.get("Credits", 0)),
                daily_breakdown=daily_breakdown,
                resource_split=resource_split,
                hourly_rows=hourly_rows,
                error=None,
            )
        except Exception as exc:
            message = str(exc)
            if "disabled" in message.lower():
                # Workspace-level disable (e.g. spend/usage limit reached): Modal edge
                # returns 404 "workspace ... disabled" and halts workloads. Tag it so
                # the overview can render the banner instead of a generic error.
                _billing_cache.update(
                    ts=now,
                    error=(
                        "workspace disabled: spend/usage limit reached (raise it in Modal's Usage & Billing settings)"
                    ),
                    workspace_disabled=True,
                )
            else:
                _billing_cache.update(ts=now, error=message[:200])
        return dict(_billing_cache)

    def session_value() -> str:
        issued = str(int(time.time()))
        signature = hmac.new(dashboard_token.encode(), issued.encode(), hashlib.sha256).hexdigest()
        return f"{issued}.{signature}"

    def valid_session(request: Request) -> bool:
        if not dashboard_token:
            return False
        raw = request.cookies.get(cookie_name, "")
        issued, separator, supplied = raw.partition(".")
        if not separator or not issued.isdigit() or time.time() - int(issued) > session_max_age:
            return False
        expected = hmac.new(dashboard_token.encode(), issued.encode(), hashlib.sha256).hexdigest()
        return secrets.compare_digest(supplied, expected)

    def authorized(request: Request) -> bool:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {dashboard_token}" if dashboard_token else ""
        return valid_session(request) or (bool(dashboard_token) and hmac.compare_digest(supplied, expected))

    @api.get("/_dashboard/login")
    async def login_page():
        return Response(content=_dashboard_login_html(), media_type="text/html")

    @api.post("/_dashboard/login")
    async def login(request: Request):
        body = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        supplied = body.get("credential", [""])[0]
        if not dashboard_token or not hmac.compare_digest(supplied, dashboard_token):
            return Response(content=_dashboard_login_html(), status_code=401, media_type="text/html")
        response = RedirectResponse("/_dashboard", status_code=303)
        response.set_cookie(
            cookie_name, session_value(), max_age=session_max_age, httponly=True, secure=True, samesite="lax"
        )
        return response

    @api.get("/_dashboard/logout")
    async def logout():
        response = RedirectResponse("/_dashboard/login", status_code=303)
        response.delete_cookie(cookie_name)
        return response

    @api.get("/_dashboard/assets/dashboard.js")
    async def dashboard_js_asset():
        return Response(content=Path("/root/dashboard/dashboard.js").read_text(), media_type="application/javascript")

    @api.get("/_dashboard/api/stats")
    async def stats(request: Request):
        if not authorized(request):
            return Response(status_code=401, content="unauthorized")
        with contextlib.suppress(Exception):
            await usage_volume.reload.aio()
        events: list[dict[str, object]] = []
        ledger_dir = Path(USAGE_LEDGER_DIR)
        if ledger_dir.exists():
            for path in ledger_dir.glob("*.jsonl"):
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        with contextlib.suppress(json.JSONDecodeError):
                            value = json.loads(line)
                            if isinstance(value, dict):
                                events.append(value)
        catalog = json.loads(CATALOG_PATH.read_text()).get("models", {})
        if not isinstance(catalog, dict):
            catalog = {}
        gpu_ids: list[str] = []
        boot_history: list[dict[str, object]] = []
        gpu_registry = Path(USAGE_DIR) / "gpu-containers.jsonl"
        if gpu_registry.exists():
            # Append-only boot registry: keep the LAST registration per container
            # id, then drop anything older than 12h (containers are ephemeral;
            # long-dead boots should not show as candidates). gpu_stop already
            # intersects with live Modal containers, so this is display hygiene.
            latest: dict[str, float] = {}
            # Boot time IS when a serve-group change takes effect, so the same
            # log is the hot-set change history. Keep the members recorded at
            # each boot, newest first, and collapse consecutive identical sets
            # so the dashboard shows changes rather than every restart.
            boots: list[dict[str, object]] = []
            with gpu_registry.open(encoding="utf-8") as handle:
                for line in handle:
                    with contextlib.suppress(json.JSONDecodeError):
                        value = json.loads(line)
                        if isinstance(value, dict) and value.get("container_id"):
                            cid = str(value["container_id"])
                            seen = float(value.get("registered_at", 0) or 0)
                            latest[cid] = max(latest.get(cid, 0.0), seen)
                            recorded = value.get("members")
                            boots.append(
                                {
                                    "container_id": cid,
                                    "registered_at": seen,
                                    "alias": str(value.get("alias", "")),
                                    "members": [str(m) for m in recorded] if isinstance(recorded, list) else [],
                                }
                            )
            cutoff = time.time() - 12 * 3600
            gpu_ids = [
                cid for cid, seen in sorted(latest.items(), key=lambda item: item[1], reverse=True) if seen >= cutoff
            ]
            for boot in sorted(boots, key=lambda b: float(b["registered_at"]), reverse=True):
                if float(boot["registered_at"]) < cutoff:
                    continue
                if not boot["members"]:
                    # Pre-feature boot: no hot set recorded, so it carries no
                    # change information. Showing it would read as a spurious
                    # transition to an empty set.
                    continue
                if boot_history and boot_history[-1]["members"] == boot["members"]:
                    # Same hot set as the newer boot: not a change, just a restart.
                    continue
                boot_history.append(boot)
                if len(boot_history) >= 5:
                    break
        event_count = len(events)
        prompt_tokens = sum(
            int(event["prompt_tokens"]) for event in events if isinstance(event.get("prompt_tokens"), int)
        )
        completion_tokens = sum(
            int(event["completion_tokens"]) for event in events if isinstance(event.get("completion_tokens"), int)
        )
        gpu_seconds = sum(float(event.get("gpu_seconds", 0) or 0) for event in events)
        # Honest serving status: per-container heartbeat files written by each
        # GPU container's waiting thread (updates every 30s while the server
        # process lives). Stale beyond 90s means that container is hard-dead.
        # Multi-container safe: pick the freshest live state as the primary
        # (legacy single file still honored for pre-multi-container deploys).
        serving_state: dict[str, object] = {}
        candidates: list[dict[str, object]] = []
        state_dir = Path(USAGE_DIR)
        for state_path in [*state_dir.glob("serving-state-*.json"), state_dir / "serving-state.json"]:
            if not state_path.exists():
                continue
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(state_path.read_text())
                if isinstance(value, dict):
                    candidates.append(value)
        live_candidates = [
            c for c in candidates if bool(c.get("ok")) and (time.time() - float(c.get("heartbeat", 0) or 0)) < 90
        ]
        if live_candidates:
            serving_state = max(live_candidates, key=lambda c: float(c.get("heartbeat", 0) or 0))
        elif candidates:
            serving_state = max(candidates, key=lambda c: float(c.get("heartbeat", 0) or 0))
        heartbeat = float(serving_state.get("heartbeat", 0) or 0)
        serving_live = bool(serving_state.get("ok")) and (time.time() - heartbeat) < 90
        serving_detail = str(serving_state.get("detail", ""))
        billing = await _billing_snapshot()
        if resolved is not None:
            members = resolved.get("members") or []
            first = members[0] if isinstance(members, list) and members else {}
            tuning = resolved.get("tuning") if isinstance(resolved.get("tuning"), dict) else {}
            deployment = {
                "alias": resolved.get("name", deployed_profile),
                "model": str(first.get("model", "")) if isinstance(first, dict) else "",
                "revision": str(first.get("revision", "")) if isinstance(first, dict) else "",
                "runtime": resolved.get("runtime", "unknown"),
                "gpu": resolved.get("gpu", ""),
                "gpu_count": resolved.get("gpu_count", 0),
                "max_len": int(tuning.get("contextTokens", 0)) if tuning else 0,
                "profile": {},
            }
            active_tuning = str(resolved.get("tuning_name", "baseline"))
            tuning_block = dict(tuning)
        else:
            deployment = {
                "alias": deployed_profile,
                "runtime": "unknown",
                "model": "",
                "revision": "",
                "gpu": "",
                "gpu_count": 0,
                "max_len": 0,
                "profile": {},
            }
            active_tuning, tuning_block = "none", {}
        deployment_data = {
            "alias": deployment["alias"],
            "model": deployment["model"],
            "revision": deployment["revision"],
            "runtime": deployment["runtime"],
            "gpu": deployment["gpu"],
            "gpu_count": deployment["gpu_count"],
            "context_tokens": deployment["max_len"],
            "status": "serving" if serving_live else "stopped",
            "health": "serving" if serving_live else "stopped",
            "serving_detail": serving_detail,
            "container_id": serving_state.get("container_id", ""),
            "heartbeat_age_seconds": round(time.time() - heartbeat, 1) if heartbeat else None,
            "active_tuning": active_tuning,
            "tuning": {"profile": active_tuning, **tuning_block},
            # Per-slot state published by the GPU container's heartbeat thread
            # (parsed from llama-server's log). Absent on old containers.
            "slots": serving_state.get("slots") if serving_live else None,
            # Proxy slot-gate counters: distinguishes fleet requests parked
            # at the gate (silent) from ones actually inside llama.
            "gate": serving_state.get("gate") if serving_live else None,
            # Co-resident hot set: which aliases this container serves, and the
            # per-alias gate breakdown (aggregate stays in `gate`).
            "members": serving_state.get("members") if serving_live else None,
            "gate_aliases": serving_state.get("gate_aliases") if serving_live else None,
            # The CONFIGURED hot set, known from the catalog whether or not a
            # container is up. `members` above is the live/resident truth and
            # goes null at scale-to-zero, which left "what is in the hot seat?"
            # unanswerable exactly when nothing was running.
            "configured_members": _configured_members(resolved),
            # Hot-set change history: boot time is when a serve-group change
            # lands, so consecutive-identical sets are collapsed to changes.
            "boot_history": boot_history,
        }
        if serving_live and not serving_state.get("model_loaded"):
            deployment_data["status"] = "booting"
        catalog_data = []
        for alias, profile in catalog.items():
            if isinstance(profile, dict):
                active, selected = _tuning_profile(profile)
                catalog_data.append(
                    {
                        "alias": alias,
                        "model": str(profile.get("model", "")),
                        "revision": str(profile.get("revision", "")),
                        "runtime": str(profile.get("runtime", "vllm")),
                        "gpu": str(profile.get("gpu", "")),
                        "gpu_count": int(profile.get("gpuCount", 0)),
                        "context_tokens": int(profile.get("maxContextTokens", 0)),
                        "enabled": bool(profile.get("enabled")),
                        "status": str(profile.get("status", "")),
                        "active_tuning": active,
                        "tuning": selected,
                        "state": "deployed" if alias == deployment.get("alias") else "catalog-only",
                    }
                )
        cost_compare = _cost_compare_payload(events, billing)
        return {
            "schema_version": 2,
            "deployment": deployment_data,
            "gpu_fleet": _gpu_fleet(),
            "usage": {
                "requests": event_count,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "gpu_seconds": gpu_seconds,
                "metered_today_usd": billing.get("metered_today_usd"),
                "metered_month_to_date_usd": billing.get("metered_month_usd"),
                "workspace_billed_month_usd": billing.get("workspace_billed_month_usd"),
                "workspace_credits_month_usd": billing.get("workspace_credits_month_usd"),
                "billing_daily_breakdown": billing.get("daily_breakdown"),
                "billing_resource_split": billing.get("resource_split"),
                "billing_error": billing.get("error"),
                "workspace_disabled": bool(billing.get("workspace_disabled")),
                "billing_updated_at": billing.get("ts"),
            },
            "cost_compare": cost_compare,
            "catalog": sorted(catalog_data, key=lambda item: item["alias"]),
            "gpu_containers": sorted(gpu_ids),
            "recent": events[-100:],
            "events": events[-1000:],
            "event_count": event_count,
            "model_profiles": sorted({str(event.get("model", "")) for event in events if event.get("model")}),
        }

    @api.get("/_dashboard")
    async def dashboard_page(request: Request):
        if not valid_session(request):
            return RedirectResponse("/_dashboard/login", status_code=303)
        return Response(content=Path("/root/dashboard/index.html").read_text(), media_type="text/html")

    @api.post("/_dashboard/api/billing/refresh")
    async def billing_refresh(request: Request):
        if not authorized(request):
            return Response(status_code=401, content="unauthorized")
        return await _billing_snapshot(force=True)

    @api.post("/_dashboard/api/runtime-tuning")
    async def runtime_tuning(request: Request):
        """Flex tuning knobs (numParallel, contextTokens) WITHOUT a redeploy.

        Body: {alias?: str=deployed, numParallel?, contextTokens?, batch?,
        ubatch?} to set, or {alias?: str, clear: true} to remove that alias's
        overrides entirely. Writes /usage/runtime-overrides.json; the NEXT GPU
        container boot applies it.

        Clearing matters: overrides are invisible at boot (they silently beat
        the baked catalog) and a stale entry from an old experiment quietly
        changes what every later measurement and deployment actually runs.
        Merging forever with no way to remove an entry is how that happens.
        Response: {applied, alias, overrides, note}.
        """
        import json as _json

        if not authorized(request):
            return Response(status_code=401, content="unauthorized")
        path = Path(USAGE_DIR) / "runtime-overrides.json"
        try:
            body = _json.loads((await request.body()).decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            alias = str(body.get("alias") or deployed_profile)
            if not alias:
                raise ValueError("no deployed alias")
            if body.get("clear"):
                overrides = {}
            else:
                allowed = ("numParallel", "contextTokens", "batch", "ubatch")
                overrides = {k: int(body[k]) for k in allowed if k in body and int(body[k]) > 0}
                if not overrides:
                    raise ValueError(f"nothing to set; allowed keys: {allowed} (or pass clear=true)")
        except (ValueError, _json.JSONDecodeError) as exc:
            return Response(status_code=400, content=str(exc))
        try:
            current = _json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(current, dict):
                current = {}
        except (OSError, _json.JSONDecodeError):
            current = {}
        if overrides:
            merged = {**(current.get(alias) if isinstance(current.get(alias), dict) else {}), **overrides}
            current[alias] = merged
        else:
            current.pop(alias, None)
            merged = {}
        if current:
            path.write_text(_json.dumps(current))
        else:
            # Leave no empty file behind: its presence is what makes the boot
            # path take the override branch at all.
            with contextlib.suppress(OSError):
                path.unlink()
        with contextlib.suppress(Exception):
            await usage_volume.commit.aio()
        return {
            "applied": True,
            "alias": alias,
            "overrides": merged,
            "cleared": not overrides,
            "note": "takes effect on the next GPU container boot (modal-inference shutdown cycles it)",
        }

    dashboard_hooks.fire("dashboard.boot.post", {"profile": deployed_profile, "ok": True})
    return api
