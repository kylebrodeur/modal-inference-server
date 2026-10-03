# Examples

Minimal, standard-library-only scripts showing the deployed endpoints.

## Run

1. Deploy the server in this repo (see README.md).
2. Export the base URL and auth token for `MODAL_INFERENCE_URL` / `MODAL_INFERENCE_API_TOKEN`, plus `MODAL_INFERENCE_MODEL` with a deployed catalog alias (e.g. `qwen3.8-27b`; the script's `default` fallback is not a shipped alias).
3. Run: `uv run examples/chat_example.py`

## Modify for your data

Each example is intentionally minimal (no SDK deps) so it can be copied
directly into your own stack.