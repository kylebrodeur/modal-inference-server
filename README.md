# Modal Inference Server

High-performance, GPU-accelerated LLM inference infrastructure deployed on Modal. This system provides a production-ready bridge between open-weight model registries and OpenAI-compatible API endpoints.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Modal](https://img.shields.io/badge/platform-Modal-green)](https://modal.com)
[![Runtime](https://img.shields.io/badge/runtime-GPU-orange)](https://modal.com/docs)
[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-pink.svg)](https://github.com/sponsors/kylebrodeur)

## Architecture Pillars

### 1. Routing
The server implements a sophisticated routing layer to maximize GPU throughput while maintaining low latency:
- **Hot-Set Routing**: Utilizes `llama-server` in router mode to co-host multiple model aliases within a single GPU container, eliminating eviction overhead for frequently used model pairs.
- **Slot-Gating**: Employs a real-time semaphore system based on actual `llama.cpp` slot grants, preventing the "silent clamping" common in generic inference wrappers.
- **Prefill Keepalive**: A custom proxy layer injects SSE keepalive comments during the prefill phase, ensuring streaming clients do not time out on large context prompts.

### 2. Serving
Model lifecycle and residency are managed through a deterministic catalog:
- **Registry-Driven**: All model configurations, quantizations, and revisions are pinned in `models.json`.
- **Hybrid Backends**: Seamlessly switches between `Ollama`, upstream `llama.cpp`, and `vLLM` based on the model profile's requirements.
- **Persistent Storage**: Uses Modal Volumes for shared model weights and engine caches, ensuring fast cold-starts across container restarts.

### 3. Scale-to-Zero
Optimized for cost-efficiency without sacrificing reliability:
- **Dynamic Autoscaling**: Configured with `MIN_CONTAINERS=0`, allowing the infrastructure to scale to zero when idle.
- **Controlled Warmup**: Implements CUDA-graph bucket warming and pre-loading sequences to reduce the "first-token" latency of newly spawned containers.
- **Eager Shutdown**: Includes administrative tooling (`gpu_stop_eager`) to force-zero GPU workers during maintenance or cost-capping events.

## Quick Start

### Prerequisites
- [Modal](https://modal.com) account and CLI installed.
- [uv](https://github.com/astral-sh/uv) for fast Python dependency management.

### Setup & Deployment

1. **Authenticate with Modal**
   ```bash
   modal setup
   ```

2. **Bootstrap a Model**
   Download a pinned model from the catalog into the shared Modal Volume:
   ```bash
   uv run modal run server/modal_service.py::bootstrap_model --alias <model-alias>
   ```

3. **Deploy the Server**
   Deploy the service for a specific model profile:
   ```bash
   MODEL_PROFILE=<model-alias> uv run modal deploy server/modal_service.py
   ```

## Configuration

The server is configured via environment variables and `models.json`:

| Variable | Description | Default |
|----------|-------------|----------|
| `MODEL_PROFILE` | The alias of the model to serve | `""` |
| `MAX_CONTAINERS` | Maximum GPU containers to scale out | `1` |
| `MIN_CONTAINERS` | Minimum GPU containers (set to 0 for scale-to-zero) | `0` |
| `SCALEDOWN_WINDOW`| Seconds of inactivity before scaling down | `300` |
| `GATE_WAIT_SECONDS`| Client wait time before returning a 429 | `240` |
