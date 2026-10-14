---
name: mtk-inference-init-config
description: Use when a project adds the inference package - mtk init, the overlay pair (deploy.json, deploy.sh), and the three-decrets secrets setup.
---

# Inference: init and config for a project

## Init

```bash
mtk init infer-lab
cd infer-lab
mtk pull inference --tag v1.6.0   # or the current release tag
```

`mtk pull` vendors the exact release tag into `vendor/inference-server/` and
writes its provenance. The copy is read-only; drift refuses a deploy.

## The overlay

`deploys/inference-server/deploy.json` names the app `<project>-inference`:

```json
{ "pkg": "inference", "app_name": "infer-lab-inference" }
```

Model aliases and runtime choices live in the repo's own registry; the overlay
pins them (env knobs). `deploy.sh` is the deploy entry point through
`mtk deploy inference`.

## Before the first deploy

- Three secrets are declared: `inference-auth-secret` (`API_TOKEN`, generated),
  `modal-inference-server-dashboard` (`MODAL_INFERENCE_DASHBOARD_TOKEN`,
  generated), and `modal-inference-server-huggingface` (`HF_TOKEN`, supplied by
  you for gated downloads). `mtk deploy` asks before creating any missing one.
- Staging weights is seed work, not deploy work: write the overlay's
  `seed.json`, then `mtk seed inference` (loads from the load-skill's recipes).

## Verify

```bash
mtk doctor --pkg inference
mtk inference models list --remote   # the repo CLI through mtk passthrough
```