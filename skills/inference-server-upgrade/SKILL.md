---
name: inference-server-upgrade
description: Upgrade + deploy-boundary discipline for the modal-inference-server checkout: tag-based upgrades, overlay-only deploys, hot-set awareness.

Use when upgrading/redeploying this repo, touching inference lanes, or before modal commands inside this checkout.
license: Apache-2.0
metadata:
  author: kylebrodeur
  family: modal-toolkit
  repo: modal-inference-server
---

# inference-server: upgrade + deploy boundary (lane agents)

Same contract as every family repo (see this repo's AGENTS.md):
SOURCE, never a deploy target from a lane; upgrades land by TAG.

```bash
# 0. provenance preflight (system workspace):
tools/guards/deploy-provenance.sh <overlay-dir>

# 1. upgrade by tag + verify
git fetch --tags && git checkout <tag>
uv run --project server pytest server/tests -q
```

Inference-specific cautions an overlay must respect:

- Deploys are per-TARGET (`MODEL_PROFILE` env at deploy = the alias or
  serve group a container preloads). An overlay deploy changes the
  target for THAT app only; never redeploy with a different profile
  without checking which lane owns the app (`modal app list`).
- Scale-to-zero + `MODAL_INFERENCE_MIN_CONTAINERS=1` lanes cost money
  every hour: any NEW app you must stand up gets
  `MTK_APP_SLUG`-composed names and is `modal app stop`ped the moment
  the task ends.
- Hooks: `lane.boot.pre/post` + `request.pre/post` + `inject.pre`
  fire from `server/modal_service.py` (dashboard has its own instance).
  `inject.pre` is the body-aware seam: it hands a handler the parsed,
  mutable chat body for `POST /chat/completions` before forwarding
  (shadow-memory style injection); it fires only when registered. Lane
  observers register here; boot-provenance observer =
  `tools/guards/boot-provenance.py` in the system workspace.
- The metrics/usage surface is append-only (`/usage` ledger): a rogue
  deploy sharing this lane's Volume would corrupt ledger continuity —
  one more reason deploys are overlay-only.
