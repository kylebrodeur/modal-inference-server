# How to operate modal-inference-server

Current release: v1.6.0. The inference repo owns its operator surface in its
own `modal-inference` CLI. `mtk inference <command>` passes through to it.
Start with the [quickstart](./quickstart.md) if you have not deployed yet.

## Models

Models are configured in the repo's own registry and selected by alias.
Confirm the alias list with `/v1/models`. Adding or changing a model is a
repo-side change, then a redeploy through your overlay. Do not hand-edit the
checkout to switch models.

## Runtime and cost

The runtimes are llama.cpp router mode and vLLM. Modal bills per GPU-hour, so
the always-on cost is the number to watch. `mtk cost` shows the per-package
burn, and the repo's `modal-inference` CLI has its own cost and pricing
commands.

## Warm and shut down

The app scales to zero by default. For an eager stop, use the repo's own
commands through `mtk inference`.

Stopping the app is a different action from scaling to zero. `modal app stop`
undeploys the app. It is not the same as scale-to-zero, and `mtk shutdown`
applies a typed-confirmation gate before it runs an undeploy.

## Health

`/v1/models` is the health and inventory check. A cold start looks like
a failure on the first request, so retry once before you debug.

## Related

- Quickstart: [quickstart.md](./quickstart.md)
