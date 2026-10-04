"""Cost model: serving-hour attribution, blended per-token rates, external counterfactuals.

Pure module — Modal objects are injected (thread_publish for the volume commit).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

USAGE_DIR = "/usage"
PREFILL_TOK_PER_SEC = 2656.0
DECODE_TOK_PER_SEC = 53.5
H200_RATE_PER_HOUR = 2.35
DAY_IDLE_OVERHEAD_HOURS = 0.192
thread_publish = None  # injected by the root module (debounced volume commit)


def _cost_curve() -> list[dict[str, object]]:
    """Blended $/M vs daily volume for this deployment's token mix ratios.

    Shows the real leverage: idle share evaporates with volume, so the blended
    rate falls toward the mix's dense-time rate. Heavy agentic mixes decode
    more (53 tok/s phase) and land higher asymptotes.
    """
    curve: list[dict[str, object]] = []
    for label, in_share in (("chat (99% in)", 0.992), ("agentic (80% in)", 0.80), ("output-heavy (50% in)", 0.50)):
        for tpd in (400_000, 2_000_000, 12_000_000, 50_000_000):
            in_tok = tpd * in_share
            out_tok = tpd * (1 - in_share)
            dense = (in_tok / PREFILL_TOK_PER_SEC + out_tok / DECODE_TOK_PER_SEC) / 3600
            billed = dense + DAY_IDLE_OVERHEAD_HOURS
            curve.append(
                {
                    "mix": label,
                    "tokens_per_day": tpd,
                    "usd_per_m_blended": round(billed * H200_RATE_PER_HOUR / tpd * 1_000_000, 3),
                    "idle_share_of_bill": round(DAY_IDLE_OVERHEAD_HOURS / billed, 2),
                }
            )
    return curve


def _estimated_split(actual_usd: float, prompt: int, completion: int) -> dict[str, object]:
    """Phase-split estimated $/M rates from the GPU time the mix implies."""
    t_in = prompt / PREFILL_TOK_PER_SEC if prompt else 0.0
    t_out = completion / DECODE_TOK_PER_SEC if completion else 0.0
    total = t_in + t_out
    if not total or not prompt or not completion:
        return {"input_usd_per_m": None, "output_usd_per_m": None}
    return {
        "input_usd_per_m": round(actual_usd * (t_in / total) / prompt * 1_000_000, 4),
        "output_usd_per_m": round(actual_usd * (t_out / total) / completion * 1_000_000, 4),
        "gpu_time_share": {"input": round(t_in / total, 3), "output": round(t_out / total, 3)},
        "method": (
            f"ledger mix x measured H200 rates "
            f"(prefill {PREFILL_TOK_PER_SEC:.0f}/decode {DECODE_TOK_PER_SEC:.1f} tok/s)"
            " -> phase time share, dollar attributed"
        ),
    }


def _external_rows(
    by_id: dict[str, object], prompt: int, completion: int, actual_usd: float
) -> list[dict[str, object]]:
    """Counterfactual rows: what THIS month's token mix would have cost on each API."""
    rows: list[dict[str, object]] = []
    for name in (
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
    ):
        target = by_id.get(name)
        if not isinstance(target, dict):
            continue
        r_in = float(target["input_usd_per_m"])
        r_out = float(target["output_usd_per_m"])
        r_cached = float(target.get("cached_input_usd_per_m") or r_in)
        same_mix = prompt / 1e6 * r_in + completion / 1e6 * r_out
        same_mix_cached = prompt / 1e6 * r_cached + completion / 1e6 * r_out
        tokens = prompt + completion
        if actual_usd > 0:
            ratio = same_mix / actual_usd
            verdict = (
                f"{name} would cost {ratio:.1f}x what our GPUs billed"
                if ratio > 1
                else f"{name} is {1 / ratio:.1f}x cheaper"
            )
        else:
            verdict = "insufficient data"
        rows.append(
            {
                "model": name,
                "provider": target["provider"],
                "input_usd_per_m": r_in,
                "output_usd_per_m": r_out,
                "cached_input_usd_per_m": target.get("cached_input_usd_per_m"),
                # Rate-vs-rate: what one blended M tokens costs on this API for
                # the same in/out mix — directly comparable with ours.
                "usd_per_m_this_mix": round(same_mix / tokens * 1_000_000, 4) if tokens else None,
                "usd_per_m_this_mix_cached": round(same_mix_cached / tokens * 1_000_000, 4) if tokens else None,
                "same_mix_cost_usd": round(same_mix, 4),
                "same_mix_cost_cached_input_usd": round(same_mix_cached, 4),
                "multiplier": round(same_mix / actual_usd, 2) if actual_usd > 0 else None,
                "verdict": verdict,
            }
        )
    return rows


def _alias_cost_row(
    rows: list[dict[str, object]], totals: int, metered: float, by_id: dict[str, dict[str, object]]
) -> dict[str, object]:
    """Actual metered GPU spend for one alias + what the same tokens cost on external APIs."""
    prompt = sum(int(r.get("prompt_tokens", 0) or 0) for r in rows)
    completion = sum(int(r.get("completion_tokens", 0) or 0) for r in rows)
    if prompt + completion == 0:
        return {"error": "no token data recorded"}
    share = (prompt + completion) / totals if totals else 0.0
    actual_usd = metered * share
    tokens = prompt + completion
    tokens = prompt + completion
    per_token = {
        "usd_per_m_all_tokens": round(actual_usd / tokens * 1_000_000, 4) if tokens else None,
        "estimated": _estimated_split(actual_usd, prompt, completion),
    }
    return {
        "ledger": {
            "requests": len(rows),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "actual_gpu_cost_usd": round(actual_usd, 4),
            "actual_gpu_cost_basis": "token-share of serving-hour cost (excludes experiments/churn hours)",
        },
        "per_token": per_token,
        "external": _external_rows(by_id, prompt, completion, actual_usd),
    }


def _load_external_rates() -> dict[str, dict[str, object]]:
    """model-id -> rate row from the baked external rate cards (empty on missing file)."""
    try:
        rates = json.loads(Path("/root/external-rate-cards.json").read_text())
    except (OSError, json.JSONDecodeError):
        rates = {}
    by_id: dict[str, dict[str, object]] = {}
    for provider_name, provider in rates.get("providers", {}).items():
        for model in provider.get("models", []):
            if isinstance(model, dict) and model.get("input_usd_per_m") is not None:
                by_id[str(model["id"])] = {**model, "provider": provider_name}
    return by_id


def _serving_hours(events: list[dict[str, object]]) -> set[int]:
    """Ledger request hours, with ±1h margin (boot before + scaledown after)."""
    hours: set[int] = set()
    for e in events:
        at = int(e.get("recorded_at", 0) or 0)
        if at and isinstance(e.get("completion_tokens"), int):
            hours.update({(at - 3600) // 3600, at // 3600, (at + 3600) // 3600})
    return hours


def _serving_cost_usd(events: list[dict[str, object]], hourly_rows: list[dict[str, object]]) -> float:
    """App dollars billed ONLY in hours where real inference traffic flowed (all resources).

    Excludes early experiment days (no ledger requests) and churn-only
    hours where nobody actually requested anything.
    """
    hours = _serving_hours(events)
    return round(
        sum(float(row["usd"]) for row in hourly_rows if int(row.get("hour", 0)) // 3600 in hours),
        4,
    )


def _cost_compare_payload(events: list[dict[str, object]], billing: dict[str, object]) -> dict[str, object]:
    """Self-host vs public-API comparison from the ledger and baked rate cards.

    Mirrors `modal-inference cost compare`: model share of metered cost from token mix,
    per-M self-host $ rates, and each external model's same-mix cost.
    """
    metered = billing.get("metered_month_usd") if isinstance(billing, dict) else None
    serving_usd = _serving_cost_usd(events, billing.get("hourly_rows") or [])
    totals = sum(
        int(e.get("prompt_tokens", 0) or 0) + int(e.get("completion_tokens", 0) or 0)
        for e in events
        if isinstance(e, dict)
    )
    result: dict[str, object] = {
        "metered_month_to_date_usd": metered if isinstance(metered, (int, float)) else None,
        "serving_cost_usd": serving_usd,
        "cost_curve": _cost_curve(),
        "app_per_token": {
            "usd_per_m_all_tokens": round(serving_usd / totals * 1_000_000, 4) if totals and serving_usd > 0 else None,
            "note": (
                "H200 spend only in hours with real traffic / all ledger tokens — "
                "excludes experiments and idle churn"
            ),
        },
        "models": {},
    }
    if not isinstance(metered, (int, float)):
        return result
    by_id = _load_external_rates()
    by_alias: dict[str, list[dict[str, object]]] = {}
    for e in events:
        if isinstance(e, dict) and e.get("model"):
            by_alias.setdefault(str(e["model"]), []).append(e)
    for alias, rows in sorted(by_alias.items()):
        result["models"][alias] = _alias_cost_row(rows, totals, serving_usd, by_id)
    return result


def _archive_billing(daily_ours: list[object], hourly_ours: list[object]) -> None:
    """Append fetched billing rows to /usage/billing-history.jsonl, deduped by key.

    Modal's report API won't span beyond 7 days hourly, so archiving on every
    successful pull accumulates the full history on the Volume for free.
    """
    path = Path(USAGE_DIR) / "billing-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                with contextlib.suppress(json.JSONDecodeError):
                    value = json.loads(line)
                    if isinstance(value, dict):
                        seen.add(f"{value.get('kind')}:{value.get('key')}")

    rows: list[dict[str, object]] = []
    for item in daily_ours:
        key = str(item.interval_start)
        if f"day:{key}" in seen:
            continue
        rows.append(
            {
                "kind": "day",
                "key": key,
                "archived_at": time.time(),
                "usd": float(item.cost),
                "resources": {k: float(v) for k, v in (item.cost_by_resource or {}).items()},
            }
        )
    for item in hourly_ours:
        key = str(item.interval_start)
        if f"hour:{key}" in seen:
            continue
        rows.append(
            {
                "kind": "hour",
                "key": key,
                "archived_at": time.time(),
                "usd": float(item.cost),
                "resources": {k: float(v) for k, v in (item.cost_by_resource or {}).items()},
            }
        )
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")

    if thread_publish is not None:
        publish = thread_publish

        def _publish_safe() -> None:
            with contextlib.suppress(Exception):
                publish()

        threading.Thread(target=_publish_safe, daemon=True).start()
