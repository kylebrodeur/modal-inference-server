"""Validation test for the SHIPPED example catalog (server/models.json).

The example catalog is what a user copies to start; it must resolve cleanly
through the same code that boots a deployment. This guards the v1.1.0 bug where
serveGroups used the key `members` (the resolved OUTPUT shape) instead of the
input key `aliases`, so the shipped example failed validation while the suite
stayed green.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import modal_inference_catalog as cat

EXAMPLE_CATALOG = pathlib.Path(__file__).resolve().parent.parent / "models.json"


@pytest.fixture(autouse=True)
def _point_catalog_at_example(monkeypatch):
    # The module resolves CATALOG_PATH once at import; aim it at the shipped example.
    monkeypatch.setattr(cat, "CATALOG_PATH", EXAMPLE_CATALOG)


def test_example_catalog_exists_and_parses():
    data = json.loads(EXAMPLE_CATALOG.read_text())
    assert isinstance(data.get("models"), dict) and data["models"]


def test_every_example_model_resolves():
    data = json.loads(EXAMPLE_CATALOG.read_text())
    for alias in data["models"]:
        resolved = cat.resolve_serve_target(alias)
        assert resolved["name"] == alias
        assert resolved["runtime"] in ("vllm", "ollama", "llama")


def test_every_example_serve_group_resolves():
    # The regression this file exists for: a group must resolve, which fails
    # loudly ("requires a non-empty aliases list") if the catalog uses `members`.
    data = json.loads(EXAMPLE_CATALOG.read_text())
    groups = data.get("serveGroups", {})
    for name in groups:
        resolved = cat.resolve_serve_target(name)
        assert resolved["is_group"] is True
        assert resolved["members"], f"group {name!r} resolved with no members"


def test_example_serve_groups_use_aliases_key_not_members():
    data = json.loads(EXAMPLE_CATALOG.read_text())
    for name, group in data.get("serveGroups", {}).items():
        assert "aliases" in group, f"serve group {name!r} must declare `aliases`, not `members`"
        assert "members" not in group, f"serve group {name!r} uses the resolved-output key `members`"
