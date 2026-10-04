/**
 * Warm the self-hosted Modal inference service when a session starts.
 *
 * The service scales to zero when idle, so the first request of a session
 * otherwise pays a cold boot — measured 150-470s depending on whether Modal
 * has to re-acquire an H200. That boot is unavoidable, but it does not have
 * to happen while you wait: this extension kicks it off at session start and
 * returns immediately.
 *
 * Scope is deliberately narrow:
 *   - Only fires when the session's model is on the configured provider
 *     (`MODAL_INFERENCE_PROVIDER`, default `modal-inference`), so sessions
 *     against another provider never pay for a GPU they will not use.
 *     Matching is a substring check against `model.provider`, so a renamed or
 *     aliased provider still matches as long as it contains the substring.
 *   - A plain `GET /v1/models` is the trigger. Modal starts the container on
 *     the first request to the app, and `/v1/models` is the cheapest request
 *     that does it; the service's own warm path uses the same probe.
 *   - One probe per process, ever. Pi fires session_start for
 *     resume/fork/reload too, and re-probing a warm container on every one of
 *     those would be noise.
 *   - Never blocks, never throws into the session: warming is best-effort by
 *     definition, and a failed probe says nothing about whether the session
 *     itself can work (the model may be reachable even when the probe path is
 *     not).
 *
 * The token is read from the same env var the provider resolves for auth
 * (`$MODAL_PROXY_TOKEN`), so there is no second source of truth to drift.
 */

import type { ExtensionAPI, ExtensionContext, SessionStartEvent } from "@earendil-works/pi-coding-agent";

/** Provider whose models live on the self-hosted Modal service. */
const PROVIDER = process.env.MODAL_INFERENCE_PROVIDER ?? "modal-inference";
/**
 * Warm probe path. Must match the service's own health path: `/v1/models`
 * exists on every backend the proxy fronts. Probing a backend-specific path
 * returns 404 — which still boots the container, but reports a failure for a
 * healthy service.
 */
const HEALTH_PATH = "/v1/models";
/** Give up on a cold boot without failing the session. */
const TIMEOUT_MS = 20_000;

/**
 * The served hot set, read from a `/v1/models` body.
 *
 * The proxy filters that endpoint to the target's *preloaded* members, so it
 * already answers "what is in the hot seat" — the request was being made for
 * warming anyway, so this costs nothing extra. Returns null when the shape is
 * unexpected, so a format change degrades to the old message rather than
 * claiming an empty hot set.
 */
const readHotSet = (payload: unknown): string[] | null => {
  if (typeof payload !== "object" || payload === null) return null;
  if (!("data" in payload)) return null;
  const data = payload.data;
  if (!Array.isArray(data)) return null;
  const ids: string[] = [];
  for (const entry of data) {
    if (typeof entry !== "object" || entry === null) continue;
    if (!("id" in entry)) continue;
    const id = String(entry.id);
    // Backends suffix tags (`gemma-4-31b:latest`); the catalog alias is the stem.
    const alias = id.split(":")[0];
    // Dedupe: a hot set is small, and the same alias can repeat across tags.
    if (alias && !ids.includes(alias)) ids.push(alias);
  }
  return ids.length ? ids : null;
};

export default async function modalWarm(api: ExtensionAPI): Promise<void> {
  /** One probe per process: session_start also fires for resume/fork/reload. */
  let probeStarted = false;

  /** Surface a one-line notice in the transcript without triggering a turn. */
  const notify = (text: string): void => {
    api.sendMessage({
      customType: "modal-warm",
      content: text,
      display: true,
    });
  };

  const warm = async (ctx: ExtensionContext): Promise<void> => {
    const model = ctx.model;
    // `model` is optional on the context (no model chosen yet); its `provider`
    // and `baseUrl` are typed on Model, so this needs no narrowing.
    if (!model) return;
    if (!model.provider.includes(PROVIDER)) return;
    if (probeStarted) return;
    probeStarted = true;
    if (!model.baseUrl) return;

    // `baseUrl` already ends in /v1 (it is the OpenAI-compatible base), and
    // HEALTH_PATH is app-root-relative and begins with /v1 itself, so strip
    // the provider's suffix before joining or the path doubles to /v1/v1/...
    const root = model.baseUrl.replace(/\/v1\/?$/, "");
    const url = `${root}${HEALTH_PATH}`;
    const token = process.env.MODAL_PROXY_TOKEN ?? "";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        method: "GET",
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        signal: controller.signal,
      });
      // The same request that warms the container also carries the hot set:
      // the proxy filters /v1/models to the target's preloaded members. Parse
      // best-effort — a warm answer with an unreadable body is still warm.
      let hotSet: string[] | null = null;
      if (response.ok) {
        hotSet = await response
          .json()
          .then(readHotSet)
          .catch(() => null);
      }
      const setSuffix = hotSet ? ` · hot set ${hotSet.join(" + ")}` : "";
      // A cold boot commonly answers before the container is ready (Modal
      // redirects, or the proxy 503s, while it starts), so a non-2xx here is
      // not proof the service is broken — only a 2xx proves it is ready.
      if (response.ok) {
        notify(`modal-warm: ${PROVIDER} warm${setSuffix}`);
      } else if (response.status === 401 || response.status === 403) {
        // Distinct from "still booting": a rejected token never becomes ready,
        // so say so instead of sending the session looking for a slow boot.
        notify(`modal-warm: proxy rejected the token (HTTP ${response.status}) — check MODAL_PROXY_TOKEN`);
      } else {
        notify(`modal-warm: cold boot started or still running (HTTP ${response.status})${setSuffix}`);
      }
    } catch {
      // Timed out or unreachable. On a cold start this is the NORMAL outcome:
      // Modal begins booting the container on this very request, and a ~5 min
      // model load beats the 20s probe, so the abort is evidence of a boot in
      // progress rather than a failure. Say that, because "may be slow" reads
      // as a problem when the next request will simply wait for the load.
      notify(
        `modal-warm: no answer within ${TIMEOUT_MS / 1000}s — either a cold boot is running (~5 min, the next request waits for it) or the service is down`,
      );
    } finally {
      clearTimeout(timer);
    }
  };

  api.on("session_start", (event: SessionStartEvent, ctx: ExtensionContext) => {
    // Forking from a live session means a container is already warm; skip it.
    if (event.reason === "fork") return;
    // Fire-and-forget: session_start must not block on a GPU boot.
    void warm(ctx);
  });
}
