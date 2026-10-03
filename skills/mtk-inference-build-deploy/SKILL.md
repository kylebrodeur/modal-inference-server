---
name: mtk-inference-build-deploy
description: Use when seeding weights and deploying or upgrading an inference profile - seed.json staging, the three secrets, and drift-gated deploy.
---

# Inference: seed, deploy, upgrade

## Seed weights first

Fresh model Volumes need weights staged BEFORE deploy. Declare the units in
the overlay's `deploys/inference-server/seed.json` (shape v1: one unit per
logical set, each with the upstream profile and its aliases):

```json
{
  "schemaVersion": 1,
  "alpha": {"profile": "llama-hemmingway", "aliases": ["hemmingway-1"]}
}
```

Then stage (runs the upstream `bootstrap_model` from the VENDORED copy, with
the overlay's env plus the unit profile, so it writes THIS project's Volume -
never the upstream app's):

```bash
mtk seed inference
```

Completion is recorded per `unit:alias` in the project record, so a re-run
touches only missing aliases. `--force` re-seeds everything. Large models take
hours; seed runs the real upstream command, so watch its output.

## Deploy

```bash
mtk deploy inference
```

Order, all refusals before any account write: overlay preflight, vendor drift
check, secrets gate (three declared secrets; asks per missing), `deploy.sh`.
Drifted vendor copies need the explicit `--allow-local-changes` (recorded after
success, never the default).

## Verify

```bash
mtk doctor --pkg inference
curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/v1/models
```

`/v1/models` lists the served aliases (the current hot set).

## Upgrade

```bash
mtk pull inference --tag <new-release>   # asks before replacing the pin
mtk seed inference                       # seeds any new aliases
mtk deploy inference
```

Engines are subprocesses: warm, do not snapshot. A GPU stop is billed at
scaledown; check `mtk cost` when a lane seems hot.