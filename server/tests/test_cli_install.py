"""Behavioral test for the `modal-inference install` verb.

The verb was dropped in genericization while `install_provider.py` and
`_auto_install()` survived; this exercises the explicit surface end to end
against a temp HOME + catalog, including the `--check` drift/exit-code path.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import cli
from install_provider import install


def _args(**kw) -> argparse.Namespace:
    base = {
        "check": False,
        "omp_only": False,
        "pi_only": False,
        "pi_agent_dir": "",
        "base_url": "",
        "token": "",
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _catalog(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "solo": {
                        "enabled": True,
                        "runtime": "vllm",
                        "model": "org/solo",
                        "revision": "0" * 40,
                        "gpu": "H200",
                        "maxContextTokens": 8192,
                        "tuning": {"baseline": {"contextTokens": 8192}},
                    }
                }
            }
        )
    )
    return path


@pytest.fixture()
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_catalog_path", lambda: _catalog(tmp_path))
    cli._write_config({"base_url": "https://lane.example.com", "token": "t0k"})
    # install_provider resolves PROVIDER_NAME at import from env; keep it stable.
    import install_provider

    monkeypatch.setattr(install_provider, "PROVIDER_NAME", "modal-inference")
    monkeypatch.setattr(install_provider, "REPO", tmp_path)


def test_install_writes_then_check_passes(_isolate):
    rc = cli.cmd_install(_args(pi_only=True, pi_agent_dir=str(pathlib.Path.home() / "pi-agent")))
    assert rc == 0
    agent_dir = pathlib.Path.home() / "pi-agent"
    written = json.loads((agent_dir / "models.json").read_text())
    provider = written["providers"]["modal-inference"]
    assert provider["models"][0]["id"] == "solo"

    # --check on the freshly written install passes.
    assert cli.cmd_install(_args(check=True, pi_only=True, pi_agent_dir=str(agent_dir))) == 0


def test_install_check_fails_when_missing(_isolate):
    # Configured, but nothing installed at this agent dir yet: --check is drift.
    agent_dir = pathlib.Path.home() / "pi-agent-unused"
    assert cli.cmd_install(_args(check=True, pi_only=True, pi_agent_dir=str(agent_dir))) == 1


def test_install_check_detects_base_url_drift(_isolate):
    agent_dir = pathlib.Path.home() / "pi-agent"
    cli.cmd_install(_args(pi_only=True, pi_agent_dir=str(agent_dir)))
    # The lane URL changed since install -> --check must flag drift (nonzero).
    rc = cli.cmd_install(
        _args(check=True, pi_only=True, base_url="https://other.example.com", pi_agent_dir=str(agent_dir))
    )
    assert rc == 1


def test_install_requires_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.delenv("MODAL_BASE_URL", raising=False)
    monkeypatch.delenv("MODAL_PROXY_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="base_url/token not configured"):
        cli.cmd_install(_args(pi_only=True))


def test_install_provider_check_direct(tmp_path):
    # The provider entry point also supports --check (used by the repo script entry).
    catalog = _catalog(tmp_path)
    rc = install("https://lane.example.com", "t0k", catalog, check=True, only="pi", pi_agent_dir=tmp_path / "pi")
    # Nothing installed yet in this isolated dir -> drift.
    assert rc == 1
