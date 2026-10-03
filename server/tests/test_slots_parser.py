"""Contract tests for the pure llama-server slot-log parser.

Behavior, not plumbing: latest-state-per-slot folding, phase
discrimination (prefill/decode/starting/released/done), comma-thousands,
summarize's completion tail, and unrecognized lines keeping old state.
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from modal_inference_slots import parse_slot_lines, summarize

NEW = "slot   operator(): id  0 | task 568 | new prompt, n_ctx_slot = 12288, n_keep = 5, task.n_tokens = 210"
PREFILL = (
    "slot print_timing: id  0 | task 568 | prompt processing, n_tokens = 1,294, "
    "progress = 0.74, t = 103.05 s / 1294.71 tokens per second"
)
DECODE = "slot print_timing: id  0 | task 568 | n_gen = 100, tg = 4.43 t/s"
DONE = "slot print_timing: id  0 | task 568 | total time = 204798.52 ms / 193226 tokens"
RELEASE = "slot  release: id  0 | task 568 | stop processing: n_tokens = 1,932, truncated = 0"


def test_folds_to_latest_state_per_slot():
    slots = parse_slot_lines([NEW, PREFILL, DECODE])
    assert slots == {
        0: {
            "task": 568,
            "phase": "decode",
            "n_gen": 100,
            "tok_per_s": 4.43,
        }
    }


def test_phase_discrimination():
    assert parse_slot_lines([PREFILL])[0]["phase"] == "prefill"
    assert parse_slot_lines([DECODE])[0]["phase"] == "decode"
    assert parse_slot_lines([DONE])[0]["phase"] == "done"
    assert parse_slot_lines([RELEASE])[0]["phase"] == "released"
    starting = parse_slot_lines([NEW])[0]
    assert starting["phase"] == "starting"
    assert starting["ctx_slot"] == 12288
    assert starting["prompt_tokens"] == 210


def test_comma_thousands_and_seconds_conversion():
    done = parse_slot_lines([DONE])[0]
    assert done["total_s"] == 204.8
    assert done["total_tokens"] == 193226


def test_non_slot_lines_ignored_unrecognized_keeps_previous():
    slots = parse_slot_lines([DECODE, "some other log line", "slot   operator(): id  0 | task 99 | gibberish line"])
    assert slots[0]["task"] == 568  # previous completion state kept


def test_summarize_keeps_last_five_completions_only():
    lines = []
    for i in range(7):
        lines.append(f"slot print_timing: id  0 | task {i} | total time = 1000.0 ms / 10 tokens")
    out = summarize(lines)
    assert [c["task"] for c in out["completions"]] == [2, 3, 4, 5, 6]
    assert out["slots"][0]["phase"] == "done"


def test_multiple_slots_isolated():
    slots = parse_slot_lines([DONE.replace("id  0", "id  1"), DECODE.replace("id  0", "id  2")])
    assert slots[1]["phase"] == "done"
    assert slots[2]["phase"] == "decode"
    assert set(slots) == {1, 2}
