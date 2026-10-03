"""Minimal chat-completion example against the deployed Modal Inference Server.

Usage:
    export MODAL_INFERENCE_URL="https://<workspace>--modal-inference-server.modal.run"
    export MODAL_INFERENCE_API_TOKEN="<your auth token>"
    uv run examples/chat_example.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("MODAL_INFERENCE_URL", "").rstrip("/")
TOKEN = os.environ.get("MODAL_INFERENCE_API_TOKEN", "")
MODEL = os.environ.get("MODAL_INFERENCE_MODEL", "default")

if not BASE or not TOKEN:
    print(
        "Set MODAL_INFERENCE_URL and MODAL_INFERENCE_API_TOKEN first.",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Why is the sky blue?"}],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as response:
        body = json.loads(response.read().decode("utf-8"))

    print(body["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
