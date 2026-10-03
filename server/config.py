"""Configuration constants and env-overridable knobs for the Modal inference service."""

from __future__ import annotations

import os

APP_NAME = os.environ.get("MODAL_INFERENCE_APP_NAME", "modal-inference-server")

MODELS_FILE = os.environ.get("MODAL_INFERENCE_MODELS_FILE", "models.json")

AUTH_SECRET_NAME = os.environ.get("MODAL_INFERENCE_AUTH_SECRET", "inference-auth-secret")

_gpu_env = os.environ.get("MODAL_INFERENCE_GPU", "H100")
GPU = _gpu_env.strip() or None

SCALEDOWN_WINDOW = int(os.environ.get("MODAL_INFERENCE_SCALEDOWN_WINDOW", "300"))

MAX_CONCURRENT_INPUTS = int(os.environ.get("MODAL_INFERENCE_MAX_CONCURRENT", "8"))

EXPORT_LIMIT_MAX = int(os.environ.get("MODAL_INFERENCE_EXPORT_LIMIT_MAX", "500"))
