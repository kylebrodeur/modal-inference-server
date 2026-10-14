# Quickstart: modal-inference-server

An OpenAI-compatible LLM endpoint on Modal, serving open models through llama.cpp
router mode and vLLM. Current release: v1.6.0.

Start with the [toolkit quickstart](https://github.com/kylebrodeur/modal-toolkit/blob/main/docs/quickstart.md) if `mtk` is not
installed yet.

## 1. Create a project

```bash
mtk init infer-lab
cd infer-lab
```

## 2. Pull the package

```bash
mtk pull inference
```

## 3. Write the overlay

`deploys/inference/deploy.json` names the app `<project>-inference`:

```json
{ "pkg": "inference", "app_name": "infer-lab-inference" }
```

Model aliases and runtime choices live in the repo's own registry. Your overlay
pins them. `deploys/inference/deploy.sh` runs the repo's `modal` entry from the
pulled checkout by path.

## 4. Deploy

```bash
mtk deploy inference
```

Three secrets are declared: `inference-auth-secret` (`API_TOKEN`, generated),
`modal-inference-server-dashboard` (`MODAL_INFERENCE_DASHBOARD_TOKEN`, generated),
and `modal-inference-server-huggingface` (`HF_TOKEN`, supplied by you for gated
downloads). `mtk deploy` asks before creating any missing secret.

`mtk inference <command>` passes through to the repo's own `modal-inference` CLI.
The overlay still owns the deploy.

## 5. Verify

```bash
mtk doctor --pkg inference
curl -s -H "Authorization: Bearer $API_TOKEN" https://<your-workspace>--<app>-serve.modal.run/v1/models
```

`/v1/models` lists the served aliases, which is the current hot set.

## Next

- Models, tuning, cost, and troubleshooting: [how-to](./how-to.md)
