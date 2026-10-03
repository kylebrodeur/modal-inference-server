# ---
# cmd: ["modal", "run", "06_gpu_and_ml/llm-serving/cost_discipline_serving.py"]
# lambda-test: true
# ---

# # Cost-disciplined LLM serving: hot sets, warm probes, and honest bills

# This example shows the serving half of the cost-discipline pattern (the
# embedding half lives in `06_gpu_and_ml/embeddings/cost_discipline_embeddings.py`).
# The full production version, with OpenAI-compatible routing, hot-set swap
# semantics, and real slot gating, lives at
# [modal-inference-server](https://github.com/kylebrodeur/modal-inference-server)
# (its [runbook](https://github.com/kylebrodeur/modal-inference-server/blob/main/docs/RUNBOOK.md)
# documents the operational traps this example avoids: the GGUF arch-string
# mismatch, half-finished bootstrap markers, and the trace-budget math).

# Three ideas, all driven by the same fact: **Modal bills per GPU-hour, never
# per token.**
#
# 1. **Scale-to-zero is the default posture.** Idle GPU time is the bill.
# 2. **A warm probe at session start hides the cold boot.** The client kicks
#    off the container boot while the user types.
# 3. **The bill shape is a verb.** Always-on burn vs session GPU-seconds is
#    printed, never guessed.

import modal

MINUTES = 60  # seconds

app = modal.App(name="example-cost-discipline-serving")

# A small instruction-tuned model on an A10G keeps the example cheap and fast
# to boot. Model weights are cached on a Volume so boots skip the download.
MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
MODEL_DIR = "/model"
model_volume = modal.Volume.from_name("cost-discipline-llm-cache", create_if_missing=True)

def download_model():
    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_ID, cache_dir=MODEL_DIR)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(
        "transformers==4.47.1",
        "torch==2.5.1",
        "accelerate==1.2.1",
        "huggingface-hub==0.26.2",
        "fastapi[standard]==0.115.6",
    )
    .env({"HF_HOME": MODEL_DIR})
    .run_function(download_model, volumes={MODEL_DIR: model_volume})
)

# The serving class: scale to zero after 3 idle minutes, gate concurrency at
# one generate-per-GPU for predictable latency on this small model.
@app.cls(
    image=image,
    gpu="A10G",
    volumes={MODEL_DIR: model_volume},
    scaledown_window=3 * MINUTES,
    secrets=[modal.Secret.from_name("huggingface")],  # optional; public models don't need it
)
@modal.concurrent(max_inputs=2)
class LLMService:
    @modal.enter()
    def load_model(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, cache_dir=MODEL_DIR)
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, cache_dir=MODEL_DIR, torch_dtype=torch.bfloat16, device_map="auto"
        )

    @modal.method()
    def chat(self, prompt: str, max_new_tokens: int = 64) -> str:
        import torch

        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens)
        return self.tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)

# ## The warm probe

# The warm probe's return value includes how long the boot took, so the
# operator learns the real cold-boot number instead of guessing. (In the
# production fleet this probe runs at agent session start, hidden in the
# time the user spends typing.)

@app.function(image=image, volumes={MODEL_DIR: model_volume})
def warm_probe() -> str:
    import time

    t0 = time.perf_counter()
    LLMService().chat.remote("warm", max_new_tokens=1)
    return f"cold boot took {time.perf_counter() - t0:.1f}s"

# ## The session: one warm probe, then real work

@app.local_entrypoint()
def main():
    import time

    t0 = time.perf_counter()
    print(warm_probe.remote())

    for prompt in ["What is the best posture for idle GPU serving? One sentence.",
                   "Why hide a cold boot behind a warm probe? One sentence."]:
        reply = LLMService().chat.remote(prompt)
        print(f"> {prompt}\n{reply}\n")

    print(f"session wall time: {time.perf_counter() - t0:.1f}s")
    print("always-on burn: $0 (scaled to zero; idle window 3 min)")
    print("session GPU cost: A10G-seconds only")

# Run it (the first run pays the model download into the Volume; later runs
# boot from cache):
#
# ```bash
# modal run cost_discipline_serving.py
# ```
#
# When you're done, stop the app or let the idle window do it:
#
# ```bash
# modal app stop example-cost-discipline-serving
# ```