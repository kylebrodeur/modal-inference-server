---
name: mtk-inference-setup-hooks
description: Use when wiring overlay hooks on a vendored inference copy - the MODAL_INFERENCE_HOOKS_MODULE door, the closed tag set, and containment rules.
---

# Inference: setup hooks (the overlay extension door)

The serving container hydrates a baked `modal_service.py` with no import path
to overlay modules, so the package exposes an env-named import door:
`MODAL_INFERENCE_HOOKS_MODULE` names an overlay hook module the container
imports right after the hooks instance is created. Unset (the default) stays
inert. A failed import is CONTAINED: reported on boot, never fatal.

## Register hooks

In the overlay (a module your overlay dir owns; the parent dir must be
importable via the image or PYTHONPATH), never in `vendor/`:

```python
from modal_service import hooks

def observe(report):
    print(report)

hooks.register("lane.boot.post", observe)
```

The closed tag set: `lane.boot.pre`, `lane.boot.post`, `request.pre`,
`request.post`, `inject.pre`. Handler errors are contained and reported via
`last_errors(tag)`, never the server.

## Deployment wiring

The overlay's `deploy.json` sets `MODAL_INFERENCE_HOOKS_MODULE` in its env
(the same knobs `deploy.sh` exports), and the deploy-time import door bakes
the module. Verify by booting once and reading the boot log for the contained
`[hooks]` line, or by firing the tag in a test.

## Containment rules

- Never edit vendored code to add a hook; the door exists for the overlay.
- A hook must never raise uncontained: it wraps a GPU container's boot and
  request paths.
- Provenance boot reporting (the lane-deploy observer) registers on
  `lane.boot.post` the same way.