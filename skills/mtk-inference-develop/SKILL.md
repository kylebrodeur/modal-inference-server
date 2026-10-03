---
name: mtk-inference-develop
description: Use when building on a running inference endpoint - the OpenAI-compatible API, tuning profiles, slot/gate telemetry, and the doctor chain.
---

# Inference: develop against a live instance

Applies once `mtk doctor --pkg inference` shows the target deployed and the
serving heartbeat fresh.

## The API

OpenAI-compatible on the deployed host: `/v1/models`, `/v1/chat/completions`
(+ `/v1/embeddings` where a model carries it). Bearer is the `API_TOKEN` from
`inference-auth-secret`. Clients never see raw llama; the proxy owns the slot
gate.

## The repo CLI through mtk

```bash
mtk inference models list --remote   # catalog vs deployed state
mtk inference doctor                 # the repo's own chain (config, deploy, heartbeat, installs, roles)
```

## Tuning and slots

- Tuning profiles apply to serve groups; `baseline` is typical; overrides take
  effect on the NEXT boot, never live.
- Slot telemetry (per-slot phase/rate from llama's log) rides the serving
  heartbeat to the dashboard; slots appear only while requests process - an
  idle fleet shows nothing, which is normal.
- Each serve-group member has its own gate: a busy model cannot park requests
  aimed at an idle one.

## Rules

- One profile per App instance: choose the enabled alias, bootstrap its pinned
  weights, deploy exactly one profile.
- A serve target's members must be seeded before deploy; the deploy refuses a
  target whose model store is missing an alias.
- Never write secret values anywhere; never run `modal deploy` inside the
  vendored copy.

## Verify

Heartbeat freshness (dashboard or `modal-inference doctor`), then a real
completion against the hot set. Cost: `mtk cost` (always-on GPU-hour view).