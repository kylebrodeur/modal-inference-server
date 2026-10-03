// Views: top-level dashboard composition (mount once, update via store watch).
import { refreshBilling, store } from "../lib/store.js";
import type { StatsPayload, CatalogEntry, UsageEvent, CostComparePayload, CostCurvePoint, CollapsedRow } from "../lib/types.js";
import { esc, fmt, fmtTok, fmtUsd, statusClass, tokPerSec, hourlyBuckets } from "../lib/analytics.js";
import { Tile, Meta } from "../components/primitives.js";
import { ChartPair } from "../components/charts.js";
import { html } from "@arrow-js/core";

const PAGE_SIZE = 25;
const pageState = { page: 0, prevPage: 0 };

const ModelCard = (model: CatalogEntry) =>
  html`<article class="model">
    <span class="${'state pill ' + statusClass(model.state)}">${esc(model.state)}</span>
    <h3>${esc(model.alias)}</h3>
    <p>${esc(model.model)}</p>
    <p>Runtime: ${esc(model.runtime)} · GPU: ${esc(model.gpu)} x ${esc(model.gpu_count)}</p>
    <p>Context: ${fmt(model.context_tokens)} · Status: ${esc(model.status)}</p>
    <p>Tuning: ${esc(model.active_tuning || "none")}${model.revision ? ` · ${esc(String(model.revision).slice(0, 12))}` : ""}</p>
  </article>`;

// Ollama OpenAI-compat clients poll /v1/models every ~2s, flooding the table with token-less dashes.
const isTokenless = (e: UsageEvent): boolean => e.prompt_tokens == null && e.completion_tokens == null;
const isCollapsedRow = (row: UsageEvent | CollapsedRow): row is CollapsedRow => "collapsed_path" in row;

// Collapse consecutive newest-first runs (length >= 2) of token-less rows sharing a path.
// Rows with real token counts never collapse. data.recent is newest-first; the representative
// is the first (newest) event of the run.
const collapseRecent = (events: UsageEvent[]): (UsageEvent | CollapsedRow)[] => {
  const out: (UsageEvent | CollapsedRow)[] = [];
  let i = 0;
  while (i < events.length) {
    const event = events[i];
    let j = i + 1;
    if (isTokenless(event)) {
      while (j < events.length && isTokenless(events[j]) && events[j].path === event.path) j++;
      if (j - i > 1) {
        out.push({ collapsed_path: event.path ?? "", count: j - i, representative: event });
        i = j;
        continue;
      }
    }
    out.push(event);
    i = j;
  }
  return out;
};

const RequestRow = (row: UsageEvent | CollapsedRow) => {
  if (isCollapsedRow(row)) {
    const rep = row.representative;
    const repElapsed = typeof rep.elapsed_seconds === "number" ? rep.elapsed_seconds : 0;
    return html`<tr>
    <td>${new Date(Number(rep.recorded_at || 0) * 1000).toLocaleTimeString()}</td>
    <td>${esc(row.collapsed_path) + " ×" + fmt(row.count)}</td>
    <td class="status">${esc(rep.status)}</td>
    <td>${repElapsed.toFixed(2)}s</td>
    <td>—</td>
    <td>—</td>
    <td>—</td>
  </tr>`;
  }
  const event = row;
  const elapsed = typeof event.elapsed_seconds === "number" ? event.elapsed_seconds : 0;
  const ctok = Number.isInteger(event.completion_tokens) ? (event.completion_tokens ?? 0) : null;
  const rate = ctok && ctok > 0 && elapsed > 0 ? `${(ctok / elapsed).toFixed(1)} tok/s` : "—";
  return html`<tr>
    <td>${new Date(Number(event.recorded_at || 0) * 1000).toLocaleTimeString()}</td>
    <td>${esc(event.path)}</td>
    <td class="status">${esc(event.status)}</td>
    <td>${elapsed.toFixed(2)}s</td>
    <td>${Number.isInteger(event.prompt_tokens) ? fmtTok(event.prompt_tokens ?? 0) : "—"}</td>
    <td>${ctok != null ? fmtTok(ctok) : "—"}</td>
    <td>${rate}</td>
  </tr>`;
};

export function mountDashboard(root: HTMLElement): () => void {
  // Static shell: header lives in index.html; only the body is ours.
  const shell = html`<div class="dashboard-root">
    <div class="error" data-error style="display:none"></div>
    <div class="disabled" data-disabled style="display:none">
      <strong>Modal workspace disabled</strong>
      <p data-disabled-msg></p>
    </div>
    <section data-skel data-overview class="skel"><div class="panel">Loading overview…</div></section>
    <section data-skel data-charts class="skel"><div class="panel">Loading charts…</div></section>
    <section data-skel data-deployment class="skel"><div class="panel">Loading deployment…</div></section>
    <section data-skel data-fleet class="skel"><div class="panel">Loading GPU fleet…</div></section>
    <section data-skel data-billing class="skel"><div class="panel">Loading billing…</div></section>
    <section data-skel data-cost class="skel"><div class="panel">Loading cost compare…</div></section>
    <section data-skel data-catalog class="skel"><div class="panel">Loading catalog…</div></section>
    <section data-skel data-recent class="skel"><div class="panel">Loading requests…</div></section>
  </div>`;
  shell(root);

  const errorEl = root.querySelector<HTMLElement>("[data-error]")!;
  const disabledEl = root.querySelector<HTMLElement>("[data-disabled]")!;
  const slots = {
    overview: root.querySelector<HTMLElement>("[data-overview]")!,
    charts: root.querySelector<HTMLElement>("[data-charts]")!,
    deployment: root.querySelector<HTMLElement>("[data-deployment]")!,
    fleet: root.querySelector<HTMLElement>("[data-fleet]")!,
    billing: root.querySelector<HTMLElement>("[data-billing]")!,
    cost: root.querySelector<HTMLElement>("[data-cost]")!,
    catalog: root.querySelector<HTMLElement>("[data-catalog]")!,
    recent: root.querySelector<HTMLElement>("[data-recent]")!,
  };

  const renderOverview = (data: StatsPayload) => {
    const t = tokPerSec(data.events || []);
    const u = data.usage;
    slots.overview.innerHTML = "";
    html`<div>
      <h2>Overview — ledger totals</h2>
      <div class="grid">
        ${Tile("Requests", fmt(u.requests))}
        ${Tile("Generation throughput", t == null ? "—" : t.avg.toFixed(1) + " tok/s", t == null ? "" : `median ${t.median.toFixed(1)} tok/s · ${t.n} timed reqs`)}
        ${Tile("Prompt tokens", fmtTok(u.prompt_tokens))}
        ${Tile("Generated tokens", fmtTok(u.completion_tokens))}
        ${Tile("Metered today (project)", fmtUsd(u.metered_today_usd))}
        ${Tile("Metered this month (project)", fmtUsd(u.metered_month_to_date_usd))}
      </div>
    </div>`(slots.overview);
    delete slots.overview.dataset.skel;
  };

  const charts = ChartPair(slots.charts);

  const renderCharts = (data: StatsPayload) => {
    charts.update(hourlyBuckets(data.events || []));
    delete slots.charts.dataset.skel;
  };

  const renderDeployment = (data: StatsPayload) => {
    const d = data.deployment;
    const t = d.tuning || {};
    slots.deployment.innerHTML = "";
    const slotData = d.slots;
    const gate = d.gate;
    const slotEntries = slotData ? Object.entries(slotData.slots) : [];
    const slotRows = slotEntries
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([id, s]) => {
        const phase = esc(s.phase);
        const detail =
          s.phase === "prefill"
            ? `${fmtTok(s.n_tokens ?? 0)} tok · ${((s.progress ?? 0) * 100).toFixed(0)}% · ${s.elapsed_s ?? "—"}s in · ${Math.round(s.tok_per_s ?? 0)} tok/s`
            : s.phase === "decode"
              ? `${fmtTok(s.n_gen ?? 0)} gen · ${Math.round(s.tok_per_s ?? 0)} tok/s`
              : s.phase === "starting"
                ? `prompt ${fmtTok(s.n_tokens ?? 0)} tok`
                : s.phase === "released"
                  ? `slot free · last prompt ${fmtTok(s.n_tokens ?? 0)} tok${s.truncated ? " (truncated)" : ""}`
                  : s.phase === "done"
                    ? `${s.total_s ?? "—"}s total · ${fmtTok(s.total_tokens ?? 0)} tok`
                    : "—";
        return html`<tr><td>slot ${esc(id)}</td><td class="status">${phase}</td><td>${detail}</td><td>${esc(String(s.task))}</td></tr>`;
      });
    const completions = (slotData?.completions ?? [])
      .slice()
      .reverse()
      .map((c) => html`<tr><td>task ${esc(String(c.task))}</td><td class="status">done</td><td>${c.total_s}s total · ${fmtTok(c.total_tokens)} tok</td><td>—</td></tr>`);
    const gateLine = gate
      ? `${gate.active} inside llama · ${gate.waiting} parked at gate · ${gate.slots} slots`
      : null;
    const members = d.members ?? [];
    const gateAliases = d.gate_aliases ?? null;
    // Catalog truth, available even at scale-to-zero (where `members` is null).
    const configured = d.configured_members ?? [];
    const bootHistory = d.boot_history ?? [];
    // A co-resident hot set: every member shares one container, so a request
    // for an idle member must not queue behind a busy sibling. Showing the
    // per-alias gate is what makes that visible.
    const memberLine = members.length > 1
      ? members
          .map((m) => {
            const g = gateAliases?.[m];
            return g ? `${m} (${g.active}/${g.slots} busy${g.waiting ? `, ${g.waiting} parked` : ""})` : m;
          })
          .join(" · ")
      : null;
    // When serving, the live set is the truth; at zero, the configured set is
    // what the NEXT boot will load — which is the question being asked then.
    const hotSetLabel = configured.length > 1 || members.length > 1;
    const hotSetValue = members.length ? members.join(" + ") : configured.join(" + ");
    // Change history: only the transitions, since consecutive identical sets
    // collapse server-side. A second entry means the hot set actually changed.
    const historyLines = bootHistory.map((b, i) => {
      const when = new Date(b.registered_at * 1000).toISOString().replace("T", " ").slice(0, 16);
      const set = b.members.length ? b.members.join(" + ") : b.alias || "(unknown)";
      const previous = bootHistory[i + 1];
      const prevSet = previous && previous.members.length ? previous.members.join(" + ") : null;
      const changed = prevSet && prevSet !== set;
      return html`<li>${esc(when)}Z · ${esc(set)}${changed ? html` <span class="subtle">(was ${esc(prevSet ?? "")})</span>` : null}</li>`;
    });
    html`<div>
      <h2>Deployment & active tuning</h2>
      <div class="panel meta">
        ${Meta(hotSetLabel ? "Hot set" : "Active model", esc(hotSetValue))}
        ${hotSetLabel && !members.length && configured.length ? Meta("State", "scaled to zero — configured set loads on next warm") : null}
        ${Meta("Health", esc(d.health))}
        ${d.heartbeat_age_seconds != null ? Meta("Heartbeat age", `${esc(d.heartbeat_age_seconds)}s${d.serving_detail ? ` · ${esc(d.serving_detail)}` : ""}`) : null}
        ${Meta("Runtime", `${esc(d.runtime)} · ${esc(d.gpu)} x ${esc(d.gpu_count)}`)}
        ${Meta("Context (tuning)", `${esc(fmt(Number(t.contextTokens ?? d.context_tokens)))} tokens · parallel ${esc(t.numParallel ?? "—")}`)}
        ${Meta("Revision", d.revision ? `${esc(String(d.revision).slice(0, 12))}…` : "—")}
        ${Meta("Tuning profile", esc(d.active_tuning))}
        ${Meta("Batch / ubatch", `${esc(t.batch ?? "—")} / ${esc(t.ubatch ?? "—")}`)}
        ${Meta("KV cache", esc(t.kvCacheType ?? "—") + (t.cacheReuse === false ? " · reuse off" : " · reuse on"))}
      </div>
      <h3>Hot set changes</h3>
      ${
        historyLines.length
          ? html`<ul class="subtle">${historyLines}</ul>`
          : html`<p class="subtle">No recorded boot changes in the last 12h. Boot time is when a serve-group change takes effect, so this stays empty until the hot set actually changes.</p>`
      }
      <h3>Slots (live from llama-server)</h3>
      ${
        slotData == null
          ? html`<p class="subtle">Slot telemetry appears when a container with the slot parser is serving (deploy after 2026-10-02) and the heartbeat has completed one cycle (~30s).</p>`
          : html`
              ${gateLine ? html`<p class="subtle"><strong>Now:</strong> ${esc(gateLine)}. "Parked" requests wait silently for a slot (client sees only a hanging connection); "inside llama" are actively processing.</p>` : null}
              ${memberLine ? html`<p class="subtle"><strong>Hot set:</strong> ${esc(memberLine)} — one container serving ${esc(String(members.length))} models co-resident. Each has its own gate, so a busy model cannot park requests aimed at an idle one.</p>` : null}
              ${slotEntries.length === 0 && completions.length === 0
                ? html`<p class="subtle">No slot activity yet since this container booted. Slots only appear while a request is being processed or after one finished on this boot — an idle fleet shows nothing here, which is normal, not a failure.</p>`
                : html`<table class="grid">
                    <thead><tr><th>Slot</th><th>Phase</th><th>Detail</th><th>Task</th></tr></thead>
                    <tbody>${slotRows}${completions}</tbody>
                  </table>`}
              <p class="subtle">Phases: starting → prefill (prompt processing, no bytes to the client yet) → decode (token output) → released. "Total" rows are finished requests. A 190K-token prompt at this tuning costs ~160-200s end-to-end — most of it prefill — which is why large sessions hit client timeouts; 10s keepalive comments now hold the connection open through prefill.</p>
            `
      }
    </div>`(slots.deployment);
    delete slots.deployment.dataset.skel;
  };

  const renderFleet = (data: StatsPayload) => {
    const f = data.gpu_fleet;
    slots.fleet.innerHTML = "";
    if (!f || !f.rows.length) {
      html`<div>
        <h2>Active GPUs</h2>
        <div class="panel">No GPU containers registered in the last 12h — everything is scaled to zero.</div>
      </div>`(slots.fleet);
      delete slots.fleet.dataset.skel;
      return;
    }
    const burnTiles: { label: string; value: string; sub?: string }[] = [];
    if (f.always_on_usd_per_hour > 0) {
      burnTiles.push({ label: "Always-on burn (alive GPUs)", value: `$${f.always_on_usd_per_hour.toFixed(2)}/h`, sub: `~$${(f.always_on_usd_per_hour * 24).toFixed(0)}/day · ~$${Math.round(((f.always_on_usd_per_hour * 24 * 365) / 1000) * 10) / 10}k/yr if never scaled down` });
      const splits = Object.entries(f.alive_gpu_counts).map(([g, n]) => `${n}x ${g}`).join(" + ");
      burnTiles.push({ label: "GPUs resident now", value: splits, sub: f.gpus_billed_while_idle });
    }
    const rows = f.rows.map((r) => {
      const hw = `${esc(r.gpu)} x ${esc(r.gpu_count)} · ${esc(r.runtime)}`;
      const set = r.members.length ? esc(r.members.join(" + ")) : esc(r.alias);
      const age = r.heartbeat_age_seconds == null ? "—" : `${esc(String(r.heartbeat_age_seconds))}s`;
      return html`<tr>
        <td><span class="${'state pill ' + statusClass(r.status === "serving" ? "deployed" : "stopped")}">${esc(r.status)}</span></td>
        <td>${esc(set)}</td>
        <td>${hw}</td>
        <td class="status">${esc(r.container_id.slice(0, 24))}</td>
        <td>${age}</td>
      </tr>`;
    });
    html`<div>
      <h2>Active GPUs (all lanes)</h2>
      <div class="grid">${burnTiles.map((t) => Tile(t.label, t.value, t.sub))}</div>
      <table class="grid">
        <thead><tr><th>State</th><th>Hot set</th><th>Hardware</th><th>Container</th><th>Heartbeat</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="subtle">Serving = heartbeat under 90s. A stopped row booted in the last 12h and either scaled down or was hard-killed; its GPU stop billing at scaledown, not at heartbeat loss.</p>
    </div>`(slots.fleet);
    delete slots.fleet.dataset.skel;
  };

  const renderBilling = (data: StatsPayload) => {
    const u = data.usage;
    slots.billing.innerHTML = "";
    html`<div>
      <h2>Modal billing (real, workspace API)</h2>
      <div class="grid">
        ${Tile("Metered today (this project)", fmtUsd(u.metered_today_usd))}
        ${Tile("Metered this month (this project)", fmtUsd(u.metered_month_to_date_usd))}
        ${Tile("Workspace billed this month (after credits)", fmtUsd(u.workspace_billed_month_usd))}
        ${Tile("Workspace credits applied this month", fmtUsd(u.workspace_credits_month_usd))}
        ${Tile("Billing status", u.billing_error ? "Error" : "OK", u.billing_updated_at ? new Date(Number(u.billing_updated_at) * 1000).toLocaleString() : "never")}
      </div>
      <p class="subtle">"Metered" is this project's own consumption cost before any workspace-level credits or adjustments. "Workspace billed" is what the whole workspace is actually invoiced after credits — it can be $0 even while metered cost is nonzero.</p>
      <div class="actions"><button data-refresh-billing @click="${() => void refreshBilling()}">Refresh billing now</button></div>
    </div>`(slots.billing);
    delete slots.billing.dataset.skel;
  };

  const renderCost = (data: StatsPayload) => {
    try {
      renderCostInner(data.cost_compare);
    } catch (err) {
      slots.cost.textContent = "COST ERR: " + String(err);
      delete slots.cost.dataset.skel;
    }
  };
  const renderCostInner = (cc: CostComparePayload | undefined) => {
    slots.cost.innerHTML = "";
    if (!cc || Object.keys(cc.models).length === 0) {
      html`<div>
        <h2>Cost: where the metered money went</h2>
        <div class="panel">No cost data yet${cc?.metered_month_to_date_usd == null ? " — Modal metered billing unavailable" : "."}</div>
      </div>`(slots.cost);
      delete slots.cost.dataset.skel;
      return;
    }
    const u = store.data?.usage;
    const split = u?.billing_resource_split ?? {};
    const splitEntries = Object.entries(split);
    const perTok = cc.app_per_token;
    const total = cc.metered_month_to_date_usd;
    html`<div>
      <h2>Cost: where the metered money went</h2>
      ${(perTok?.usd_per_m_all_tokens != null || splitEntries.length > 0) ? html`<div class="grid">
        ${Tile("Serving cost (traffic hours)", cc.serving_cost_usd != null ? fmtUsd(cc.serving_cost_usd) : "—", "GPU+CPU billed only in hours with real requests")}
        ${Tile("App metered this month (all uses)", total == null ? "—" : fmtUsd(total), splitEntries.length ? splitEntries.map(e => `${e[0]} ${fmtUsd(e[1])}`).join(" · ") : undefined)}
        ${Tile("Cost per token (serving)", perTok?.usd_per_m_all_tokens != null ? "$" + perTok.usd_per_m_all_tokens.toFixed(2) + "/M" : "—", "serving cost ÷ ledger tokens — all tokens, in+out")}
      </div>` : null}
      ${Object.entries(cc.models).map(([alias, row]) => {
        const l = row.ledger;
        const pt = row.per_token;
        const oursRate = pt?.usd_per_m_all_tokens ?? null;
        const est = pt?.estimated;
        const estIn = est?.input_usd_per_m != null ? est.input_usd_per_m.toFixed(2) : null;
        const estOut = est?.output_usd_per_m != null ? est.output_usd_per_m.toFixed(2) : null;
        return html`<div class="panel cost">
          <h3>${esc(alias)} — ours <b>${"$" + (oursRate?.toFixed?.(2) ?? "?")}/M blended</b> <span class="subtle">(estimated $${estIn ?? "—"}/M in · $${estOut ?? "—"}/M out) · ${fmtUsd(l?.actual_gpu_cost_usd ?? null)} total spend for ${fmt(l?.requests ?? 0)} requests, ${fmtTok(l?.prompt_tokens ?? 0)} in / ${fmtTok(l?.completion_tokens ?? 0)} out (${Math.round(((l?.prompt_tokens ?? 0) / ((l?.prompt_tokens ?? 0) + (l?.completion_tokens ?? 1))) * 100)}% input)</span></h3>
          <table>
            <thead><tr><th>Model</th><th>Their $/M (same mix)</th><th>cached-in $/M</th><th>Total spend (same tokens)</th><th>vs ours</th></tr></thead>
            <tbody>
              ${(row.external ?? []).map(ext => {
                // One standard for every row: blended-rate ratio their $/M ÷ our $/M, winner named.
                const theirs = ext.usd_per_m_this_mix ?? null;
                const r = theirs != null && theirs > 0 && oursRate != null && oursRate > 0 ? theirs / oursRate : null;
                const verdictCls = r == null || r === 1 ? "" : r > 1 ? "status good" : "status warn";
                const vsText = r == null
                  ? "—"
                  : r === 1
                    ? "parity (1.00x)"
                    : `${r.toFixed(2)}x${r > 1 ? " ours wins" : " they win"}`;
                return html`<tr>
                <td>${esc(ext.model)}</td>
                <td>${ext.usd_per_m_this_mix != null ? "$" + ext.usd_per_m_this_mix.toFixed(2) : "—"}</td>
                <td>${ext.usd_per_m_this_mix_cached != null ? "$" + ext.usd_per_m_this_mix_cached.toFixed(2) : "—"}</td>
                <td>${"$" + ext.same_mix_cost_usd.toFixed(2)} <span class="subtle">(cached-in: ${"$" + ext.same_mix_cost_cached_input_usd.toFixed(2)})</span></td>
                <td class="${verdictCls}">${vsText}</td>
              </tr>`.key(ext.model);
              })}
            </tbody>
          </table>
        </div>`;
      })}
      ${(cc.cost_curve?.length ? html`<div class="panel cost">
        <h3>Heavier usage → cheaper per M (idle share evaporates)</h3>
        <table>
          <thead><tr><th>Daily volume</th><th>chat (99% in)</th><th>agentic (80% in)</th><th>output-heavy (50% in)</th></tr></thead>
          <tbody>
            ${([400000, 2000000, 12000000, 50000000]).map(tpd => {
              const byKey: Record<string, CostCurvePoint> = {};
              for (const point of cc.cost_curve ?? []) if (point.tokens_per_day === tpd) byKey[point.mix] = point;
              const rate = (key: string) => {
                const point = byKey[key];
                return point ? "$" + point.usd_per_m_blended.toFixed(3) : "—";
              };
              return html`<tr>
                <td>${fmtTok(tpd)}/day</td>
                <td>${rate("chat (99% in)")}</td>
                <td>${rate("agentic (80% in)")}</td>
                <td>${rate("output-heavy (50% in)")}</td>
              </tr>`.key(tpd);
            })}
          </tbody>
        </table>
        <p class="subtle">Blended $/M if that volume ran at each mix on one H200 (incl. ~2 sessions/day of boot + scaledown idle, $2.35/h metered). Your current traffic (~0.4M/day chat-mix) sits at the top-left; the floor is the mix's dense-time rate.</p>
      </div>` : null)}
      <p class="subtle">Rate-vs-rate: serving GPU spend ÷ actual ledger tokens gives our blended $/M (headline above); each API's blend prices the same in/out mix from its rate card; the "total spend" column is those rates applied to all tokens used this era. Our in/out $/M are ESTIMATES: the ledger mix run through measured H200 phase rates (prefill ~2656 tok/s, decode ~53 tok/s) to split the hourly-billed GPU pool by the GPU-time each phase actually implies; the external rows price the same mix from rate cards (their cached-input variant shown too). ${u?.workspace_credits_month_usd ? `Workspace credits covered the whole invoice this month ($${Number(u.workspace_credits_month_usd).toFixed(2)} applied; other apps on the workspace bill separately).` : "Invoice after credits: " + fmtUsd(u?.workspace_billed_month_usd ?? null) + "."}</p>
    </div>`(slots.cost);
    delete slots.cost.dataset.skel;
  };

  const renderCatalog = (data: StatsPayload) => {
    slots.catalog.innerHTML = "";
    html`<div>
      <h2>Model catalog</h2>
      <div class="catalog">${data.catalog.map(m => ModelCard(m).key(m.alias))}</div>
    </div>`(slots.catalog);
    delete slots.catalog.dataset.skel;
  };

  const renderRecent = (data: StatsPayload) => {
    slots.recent.innerHTML = "";
    // Collapse token-less polling runs BEFORE the 25-row page so pagination math sees collapsed rows.
    const all = collapseRecent(data.recent ?? []);
    const pageCount = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
    if (pageState.page >= pageCount) pageState.page = pageCount - 1;
    const start = pageState.page * PAGE_SIZE;
    const slice = all.slice(start, start + PAGE_SIZE);
    html`<div>
      <h2>Recent requests</h2>
      <div class="panel scroll">
        <table>
          <thead><tr><th>Time</th><th>Path</th><th>Status</th><th>Elapsed</th><th>Prompt</th><th>Output</th><th>Tok/s</th></tr></thead>
          <tbody>${slice.map((row, i) => {
            const keyEvent = isCollapsedRow(row) ? row.representative : row;
            return RequestRow(row).key(`${keyEvent.recorded_at ?? "0"}:${i}`);
          })}</tbody>
        </table>
      </div>
      <div class="pager" data-pager>
        <span class="subtle">showing rows (newest first) ${all.length === 0 ? "0" : `${start + 1}–${start + slice.length} of ${all.length}`}</span>
        <button data-page-prev disabled="${pageState.page === 0 ? "disabled" : ""}">◀ Prev</button>
        <span>page ${pageState.page + 1} / ${pageCount}</span>
        <button data-page-next disabled="${pageState.page >= pageCount - 1 ? "disabled" : ""}">Next ▶</button>
      </div>
    </div>`(slots.recent);
    delete slots.recent.dataset.skel;
  };

  function renderTick(): void {
    const data = store.data;
    if (data) {
      errorEl.style.display = "none";
      if (data.usage.workspace_disabled) {
        disabledEl.querySelector<HTMLElement>("[data-disabled-msg]")!.textContent =
          data.usage.billing_error ?? "Spend/usage limit reached. Raise the limit in Modal's Usage & Billing settings.";
        disabledEl.style.display = "";
      } else {
        disabledEl.style.display = "none";
      }
      renderOverview(data);
      renderCharts(data);
      renderDeployment(data);
      renderFleet(data);
      renderBilling(data);
      renderCost(data);
      renderCatalog(data);
      renderRecent(data);
    } else if (store.error) {
      errorEl.textContent = store.error;
      errorEl.style.display = "";
    }
  }
  renderTick();
  const unwatch = watchStore(renderTick);

  const onBillingClick = (event: Event) => {
    const target = event.target as HTMLElement;
    if (target?.dataset?.refreshBilling !== undefined) void refreshBilling();
    if (target?.closest?.("[data-page-prev]")) pageState.page = Math.max(0, pageState.page - 1);
    if (target?.closest?.("[data-page-next]")) pageState.page = pageState.page + 1;
    if (pageState.page !== pageState.prevPage) {
      pageState.prevPage = pageState.page;
      if (store.data) renderRecent(store.data);
    }
  };
  pageState.prevPage = pageState.page;
  root.addEventListener("click", onBillingClick);

  return () => {
    unwatch();
    root.removeEventListener("click", onBillingClick);
    root.replaceChildren();
  };
}

// Thin wrapper so this view owns a single subscription point.
import { watch } from "@arrow-js/core";
function watchStore(tick: () => void): () => void {
  const [, stop] = watch(() => {
    void store.data; // tracked read
    void store.error; // tracked read
    tick();
  });
  return stop;
}