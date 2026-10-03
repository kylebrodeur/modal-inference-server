// Formatting helpers + ledger analytics shared by dashboard components.

export const esc = (value: unknown): string => String(value ?? "—");
export const fmt = (value: unknown): string => Number(value || 0).toLocaleString();
export const fmtTok = (v: number): string =>
  v >= 1e9 ? (v / 1e9).toFixed(1) + "B"
  : v >= 1e6 ? (v / 1e6).toFixed(v >= 1e7 ? 1 : 2) + "M"
  : v >= 1e3 ? (v / 1e3).toFixed(1) + "K"
  : String(Math.round(v || 0));
export const fmtUsd = (v: number | null): string =>
  v == null ? "Unavailable" : "$" + Number(v).toFixed(v >= 100 ? 2 : 4);
export const statusClass = (value: unknown): string =>
  value === "serving" || value === "healthy" || value === "deployed" ? "good"
  : value === "booting" ? "warn"
  : value === "error" || value === "unavailable" || value === "stopped" ? "bad" : "warn";

import type { UsageEvent } from "./types.js";

// Requests whose ledger row carries both elapsed time and a prompt-token count.
export const timed = (events: UsageEvent[]): UsageEvent[] =>
  events.filter(
    e => Number.isInteger(e.prompt_tokens) && typeof e.elapsed_seconds === "number" && e.elapsed_seconds > 0
  );

// Generation throughput over the ledger: aggregate + median per-request tok/s.
// Missing token data is availability information, never zero.
export function tokPerSec(events: UsageEvent[]): { avg: number; median: number; n: number } | null {
  const rows = timed(events).filter(e => Number.isInteger(e.completion_tokens) && (e.completion_tokens ?? 0) > 0);
  if (!rows.length) return null;
  const totalTok = rows.reduce((a, e) => a + (e.completion_tokens ?? 0), 0);
  const totalSec = rows.reduce((a, e) => a + (e.elapsed_seconds ?? 0), 0);
  const perReq = rows.map(e => (e.completion_tokens ?? 0) / (e.elapsed_seconds ?? 1)).sort((a, b) => a - b);
  return { avg: totalTok / totalSec, median: perReq[Math.floor(perReq.length / 2)], n: rows.length };
}

export interface HourBucket {
  t: number;
  req: number;
  ctok: number;
  rates: number[];
}

// n hourly buckets ending at the current hour; per-request rate samples per bucket.
export function hourlyBuckets(events: UsageEvent[], n = 24, spanMs = 3_600_000): HourBucket[] {
  const end = Date.now();
  const start = end - n * spanMs;
  const buckets: HourBucket[] = [];
  for (let i = 0; i < n; i++) buckets.push({ t: start + i * spanMs, req: 0, ctok: 0, rates: [] });
  for (const e of events) {
    const at = Number(e.recorded_at || 0) * 1000;
    if (!(at >= start && at < end)) continue;
    const b = buckets[Math.min(n - 1, Math.floor((at - start) / spanMs))];
    if (!b) continue;
    b.req++;
    if (Number.isInteger(e.completion_tokens)) b.ctok += e.completion_tokens ?? 0;
    if (typeof e.elapsed_seconds === "number" && e.elapsed_seconds > 0 && (e.completion_tokens ?? 0) > 0)
      b.rates.push((e.completion_tokens ?? 0) / e.elapsed_seconds);
  }
  return buckets;
}