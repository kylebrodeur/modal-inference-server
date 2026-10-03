# Operations Runbook

This is the generic-standalone version of the lessons learned running a
Modal-hosted OpenAI-compatible inference service in production (2026-09 → 2026-10).
The original private deployment serves real traffic 24/7; this doc carries the
operational patterns that survived contact with reality, without any private
branding or client-specific detail.

Everything here is verified against a live system. Nothing is aspirational.

---

## 0. Mental model

```text
   You (operator)                          Clients (any OpenAI SDK)
        |                                       |
   modal deploy server/modal_service.py    /v1/chat/completions
        |                                       |
        +--> one Modal App instance             |
             serves ONE target:                 |
             - a single alias, OR               |
             - a serve group (hot set)  <-------+
             (2+ models co-resident in one container)
```

A **target** (alias or hot set) determines what preloads at boot. Requests for
an alias *outside* the deployed target still get served: but by **evicting** a
resident model (11-19s per swap, both directions). That is why the target
matters: a hot set is what never swaps.

**Which runtime answers** is chosen by the URL the client points at, not by
model name. Each lane is its own Modal App instance.

### 0.1 Definitions

| Term | Definition |
| :--- | :--- |
| **alias** | A logical model name (e.g., `gemma-4-31b`) that maps to a concrete (model, revision) in `models.json`. |
| **target** | What is actually deployed: one alias, or a serve group of aliases. |
| **hot set / serve group** | Two or more aliases sharing one container, preloaded together. |
| **slot** | llama.cpp's per-model concurrency slot. The *actual* count is measured from boot logs, not trusted from the profile. |
| **lane** | One deployed App (one URL). |

### 0.2 Cost model: serving-hour attribution

Modal bills **per GPU-hour**, never per token: so answer cost questions in this order:

0. **Always-on burn** (the "what does holding this hardware cost" answer):
   GPU hourly rate x 24h, regardless of traffic.
1. **Traffic-driven top-up**: when requests force scaledown-window resets.
2. **Token-counts** are a *demand* signal, not a cost signal.

Billing rows are archived after every successful pull to `usage_volume:/billing-history.jsonl` (deduped `hour:`/`day:` keys), because Modal's report API cannot span >7 days hourly: the archive is the only durable history.

### 0.3 Warm-on-session-start

The service scales to zero, so the first request otherwise pays a 150-470s cold boot.
A client-side warm probe (`GET /v1/models` at session-start, returns immediately)
removes that wait by running the boot while you type your first prompt.

**Timeout on a warm probe is often evidence of a boot in progress, not a fault** -
Modal begins booting on that very request and a ~5 min load outlives a 20s probe.
A 401/403 is separate on purpose: a rejected token never resolves, and reporting
it as "still booting" sends you hunting for a boot that is not happening.

### 0.4 Thinking / trace budget

Inference traces (thinking content) can be 33% of a real fleet session's tokens.
**Rule of thumb: if a deterministic tool call will do the work, traces off;
if the model's judgment IS the work, traces on.**

| Work class | Thinking | Why |
| :--- | :--- | :--- |
| Tool-loop execution (deterministic scripts, edits: dispatch only) | **off** | The script is the intelligence; the model only dispatches. Traces are 5x re-narration. |
| Search / summarize / classify subagents | **off or low** | Answers are extractive; traces cost prefill+decode on every hop. |
| Reviews / audits / adversarial passes | **high** | The trace IS the work: finding missed constraints, edge cases. |
| Planning / architecture | **high** | The reasoning is the deliverable. |

Measured effect (same prompt, one model):
- thinking ON (default): 276 tokens / 6.9s
- thinking OFF (native flag): 54 tokens / ~2s, **more actual content, less rambling**

---

## 1. Install (one-time)

```bash
cd server
uv sync
modal setup
```

## 2. Registering a model

Two runtimes. **Ollama (GGUF) is the default lane**: it runs the big MoEs on
Hopper. vLLM stays for models whose official recipe is a vLLM image.

```bash
# Ollama lane (recommended)
modal run server/modal_service.py::bootstrap_model --alias <alias>

# Then deploy
modal deploy server/modal_service.py
```

Ollama profiles pin by **manifest digest** (64 hex). Leave `--revision` off for
the first bootstrap; it records the digest in the Volume marker, then pin with
Edit `server/models.json` (the catalog) and re-deploy.

### The GGUF arch-string trap

Before registering a GGUF, confirm its `architecture` (HF API `.gguf.architecture`) is in upstream llama.cpp's `src/llama-arch.cpp` **at your pinned llama.cpp build tag**: not merely on `main`. This is an exact string match, and the two sides disagree on spelling: a GGUF may declare `modelnext` while llama.cpp registers `model-next`. A near-miss name fails at **load**, not at register.

Check **engine support first**, then VRAM. A model can be enabled in the catalog and still be unusable either way: `enabled` means "eligible to deploy", not "servable".

### A model can look bootstrapped and still be unservable

`bootstrap` commits the blobs, then writes `/models/<alias>/.model-revision`
containing `{model}@{manifest_sha}`. **If the process dies between those two
steps, the weights are on the Volume but the marker is missing**: and
`_assert_members_bootable()` refuses to boot any group containing that member,
so one half-finished bootstrap silently blocks a whole hot set.

Diagnose without a GPU boot: list the Volume, check for the marker file, and
only then decide whether to re-bootstrap or restore the marker.

---

## 3. Switching models / deploying

One deploy serves one target. `modal deploy server/modal_service.py` is the
only switch path: no alias flip-flop without a redeploy.

### Adding a member to a hot set

A serve group lives in `models.json` under `serveGroups`:

```json
{
  "serveGroups": {
    "local-hot": {
      "aliases": ["gemma-4-31b", "qwen3-coder-next"],
      "tuning": { "contextTokens": 262144, "numParallel": 4 }
    }
  }
}
```

Rules that bite:

- The member key is **`aliases`** (not `members`): the catalog reads
  `serveGroups.<name>.aliases`, and boots fail closed when it is missing.
  `members` is the resolved output shape, never the input key.
- **Every member must already hold its pinned revision on the Volume.** Boot
  refuses groups containing a half-bootstrapped member.
- The group's `tuning` block is **container-global**: member-level `activeTuning`
  does not apply within a group (llama.cpp router mode sets one env for all).

---

## 4. Cost control

The GPU is the expensive part. Two patterns pay for themselves:

- **`gpu_stop_eager`**: force-zero the GPU worker during quiet hours. A
  deployment stays registered; `modal deploy` brings it back cheaply.
- **Scale-to-zero**: leave `scaledown_window` (default 300s) so workers stop
  when idle. First-request pays a cold start; the keepalive probe hides that.

**Before blaming the service for GPU spend, check what is calling it.**
Anything pointed at the inference URL is a wake signal: including a shared
default model-role. A wide default role bills a GPU from every subagent spawn
in every project. Keep defaults on a cheaper provider and opt in per session.

---

## 5. The daily loop

```bash
modal deploy server/modal_service.py     # or re-deploy after a profile change
# ... use it ...
modal run server/modal_service.py::gpu_stop_eager   # end of session
```

### 5.1 This repo's own metrics emissions

The serve app ships the vendored `vm_metrics.py` baked into the image and
emits, when `MODAL_INFERENCE_METRICS=1`: `inference_request` (tags
`path=chat_completions`, `status` class), `inference_prompt_tokens`, and
`inference_completion_tokens` (only when > 0). Point it with
`MODAL_INFERENCE_VM_URL`; `MODAL_INFERENCE_DEVICE_TAG` labels the `device`
tag. This is strictly additive to the `/usage` ledger: the ledger remains the
source of truth for the dashboard and cost math. No `inference_ttft_seconds`
is emitted; the proxy tracks no first-token timestamp.

---

## 6. Troubleshooting

| Symptom | Cause → fix |
| :--- | :--- |
| Request times out on a big prompt | Ollama withholds headers until prompt processing finishes. The keepalive proxy answers streaming immediately with SSE comments; check that the keepalive path is active (see §0.3). |
| Model shows 0 slots / wrong concurrency | Ollama silently clamps some architectures to 1 slot. The proxy measures `n_slots` from llama's load log and sizes its gate to actual, not declared. |
| Hot-set member won't boot | Another member is half-bootstrapped (missing `.model-revision` marker). See §2. |
| GPU burn without traffic | Check who is pointed at the URL before redeploying anything: see §4. |
| "app not deployed; nothing to stop" | Normal: the GPU container is already scaled to zero. Re-deploy to bring it back. |

---

## 7. Known upstream gaps (recorded so you do not re-research)

Two real Ollama gaps surfaced while making co-resident serving work:

1. **The real slot count is not exposed anywhere in the API.** Ollama decides
   `numParallel` per MODEL at load time: the same `ollama serve` may honor
   `np=4` for one model and clamp another to `np=1`. The only trace is a
   `WARN ... does not currently support parallel requests` line plus the
   `n_slots` value on llama's own load line. Without the count, any proxy that
   sizes concurrency from the declared value over-admits and produces silent
   llama-side queueing. Workaround: parse the load log (see `modal_service.py`
   `_measure_member_slots`). Upstream ask: expose slots on `/api/ps`.

2. **Some architectures are blocked from parallel despite a fixed engine.**
   The Ollama scheduler blocklist still cites an old llama.cpp crash that was
   fixed upstream in the engine weeks before the blocklist was updated.
   Net effect: `qwen35`/`qwen35moe` clamp to 1 slot even though the engine
   supports it. Workaround: run the **llama router** runtime, which carries
   per-model args in a preset: no Ollama fork required (see `llama_router.py`).

---

## 8. Rules that persist

- One tuning change at a time, measured against the ledger's median tok/s +
  serving-hour $/M before keeping.
- No deployment while a live session is running on the model.
- Everything cost-visible in the dashboard's fleet + billing sections, not in
  someone's memory.