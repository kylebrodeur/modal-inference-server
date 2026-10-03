"""Pure parsers for llama-server log lines -> per-slot serving state.

Used by both the GPU container's heartbeat thread (publishes slot state to
serving-state.json for the dashboard) and scripts/slot_status.py (CLI view).
No I/O here: input is log text, output is plain dicts, so both callers stay
honest to the same parsing rules.

Recognized lines (llama-server log-verbosity 4, --no-log-prefix as ollama
launches it):

    slot   operator(): id  3 | task 568 | new prompt, n_ctx_slot = ...
    slot print_timing: id  2 | task 356 | prompt processing, n_tokens = ...,
        progress = 0.74, t = 103.05 s / 1294.71 tokens per second
    slot print_timing: id  2 | task 356 | n_gen = 100, tg = 4.43 t/s
    slot  release: id  2 | task 356 | stop processing: n_tokens = ..., truncated = 0
    slot print_timing: id  0 | task 27 | total time = 204798.52 ms / 193226 tokens
"""

from __future__ import annotations

import re

_SLOT_MARK = re.compile(r"slot\s+([a-z_()]+):\s+id\s+(\d+)\s+\|\s+task\s+(-?\d+)\s+\|\s+(.*)")
_PROGRESS = re.compile(
    r"n_tokens\s*=\s*([\d,]+),\s*progress\s*=\s*([\d.]+),\s*t\s*=\s*([\d.]+)\s*s\s*/\s*([\d,.]+)\s+tokens per second"
)
_TG = re.compile(r"n_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s")
_TOTAL = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s+tokens")
_NEW = re.compile(r"new prompt, n_ctx_slot\s*=\s*(\d+),\s*n_keep\s*=\s*(\d+),\s*task\.n_tokens\s*=\s*(\d+)")
_RELEASE = re.compile(r"stop processing: n_tokens\s*=\s*([\d,]+),\s*truncated\s*=\s*(\d+)")


def parse_slot_lines(lines: list[str]) -> dict[int, dict[str, object]]:
    """Fold log lines into the latest state per slot id."""
    slots: dict[int, dict[str, object]] = {}
    for line in lines:
        match = _SLOT_MARK.search(line)
        if not match:
            continue
        _, slot_id_s, task_id_s, rest = match.groups()
        slot_id = int(slot_id_s)
        entry: dict[str, object] = {"task": int(task_id_s)}

        if progress := _PROGRESS.search(rest):
            entry.update(
                phase="prefill",
                n_tokens=int(progress.group(1).replace(",", "")),
                progress=float(progress.group(2)),
                elapsed_s=float(progress.group(3)),
                tok_per_s=float(progress.group(4).replace(",", "")),
            )
        elif tg := _TG.search(rest):
            entry.update(phase="decode", n_gen=int(tg.group(1)), tok_per_s=float(tg.group(2)))
        elif new := _NEW.search(rest):
            entry.update(
                phase="starting",
                ctx_slot=int(new.group(1)),
                prompt_tokens=int(new.group(3)),
            )
        elif release := _RELEASE.search(rest):
            entry.update(
                phase="released",
                n_tokens=int(release.group(1).replace(",", "")),
                truncated=bool(int(release.group(2))),
            )
        elif total := _TOTAL.search(rest):
            entry.update(
                phase="done",
                total_s=round(float(total.group(1)) / 1000, 1),
                total_tokens=int(total.group(2)),
            )
        else:
            continue  # unrecognized operator line; keep previous state

        slots[slot_id] = entry
    return slots


def summarize(lines: list[str]) -> dict[str, object]:
    """Convenience: slot map + recent completion walls for compact surfaces."""
    slots = parse_slot_lines(lines)
    completions = []
    for line in lines:
        match = _SLOT_MARK.search(line)
        if not match:
            continue
        rest = match.group(4)
        if total := _TOTAL.search(rest):
            completions.append(
                {
                    "task": int(match.group(3)),
                    "total_s": round(float(total.group(1)) / 1000, 1),
                    "total_tokens": int(total.group(2)),
                }
            )
    return {"slots": slots, "completions": completions[-5:]}
