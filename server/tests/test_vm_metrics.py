"""Tests for the vendored stdlib-only VictoriaMetrics writer (server/vm_metrics.py).

Subset of the canonical toolkit/tests/test_metrics.py, adapted for the vendored
copy: the env var is MODAL_INFERENCE_VM_URL and the module imports as a flat
server module (server/ is put on sys.path below, matching test_config.py).
"""

from __future__ import annotations

import ast
import http.server
import pathlib
import sys
import threading
from typing import ClassVar

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import vm_metrics


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """Minimal VM stand-in: 204 for every POST, recording what arrived."""

    captured: ClassVar[list[tuple[str, bytes]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).captured.append((self.path, body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture()
def vm_server():
    """Spin a real stdlib HTTP server on a random port; yield (url, captured)."""
    _CaptureHandler.captured = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    yield url, _CaptureHandler.captured
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_singleton():
    vm_metrics._writer = None
    yield
    vm_metrics._writer = None


def dead_port_url() -> str:
    """A URL guaranteed to have nothing listening (bind-then-close)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_port
    server.server_close()
    return f"http://127.0.0.1:{port}"


def test_line_protocol_escapes_tag_values() -> None:
    line = vm_metrics.line_protocol("m", 1, tags={"a": "x,y", "b": "k=v", "c": "back\\slash", "d": "plain"}, ts=100)
    assert line == "m,a=x\\,y,b=k\\=v,c=back\\\\slash,d=plain 1 100"


def test_line_protocol_escapes_measurement_spaces() -> None:
    line = vm_metrics.line_protocol("inference gate skip", 2, ts=50)
    assert line == "inference_gate_skip 2 50"


def test_write_passes_explicit_ts_through(vm_server) -> None:
    url, captured = vm_server
    writer = vm_metrics.VMWriter(url=url)
    assert writer.write("inference_request", 1, tags={"path": "chat_completions"}, ts=1699999999) is True
    assert len(captured) == 1
    path, body = captured[0]
    assert body == b"inference_request,path=chat_completions 1 1699999999"
    assert path.startswith("/api/v2/write?")
    assert "precision=s" in path and "org=-" in path and "bucket=-" in path


def test_write_returns_false_and_never_raises_on_connection_failure(capsys) -> None:
    writer = vm_metrics.VMWriter(url=dead_port_url(), timeout=1.0)
    assert writer.write("inference_probe", 1) is False
    assert "mtk metrics:" in capsys.readouterr().err


def test_singleton_honors_env_set_before_first_write(vm_server) -> None:
    url, captured = vm_server
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(vm_metrics.ENV_URL, url)
        vm_metrics._writer = None  # create-after-env-set ordering
        assert vm_metrics.write_metric("inference_probe", 1) is True
    assert len(captured) == 1
    assert url in vm_metrics._writer.url


def test_effective_url_defaults_then_env(vm_server) -> None:
    url, _captured = vm_server
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv(vm_metrics.ENV_URL, raising=False)
        assert vm_metrics.effective_url() == (vm_metrics.DEFAULT_URL, "default")
        mp.setenv(vm_metrics.ENV_URL, url + "/")
        assert vm_metrics.effective_url() == (url, f"env {vm_metrics.ENV_URL}")


def test_module_top_level_imports_are_stdlib_only() -> None:
    src = pathlib.Path(vm_metrics.__file__).read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    non_stdlib = imported - sys.stdlib_module_names
    assert non_stdlib == set(), f"non-stdlib imports at module top: {non_stdlib}"


def test_env_var_is_inference_namespaced() -> None:
    # Vendoring delta guard: the only behavioral divergence from the canonical
    # toolkit/metrics.py is the env var name.
    assert vm_metrics.ENV_URL == "MODAL_INFERENCE_VM_URL"
    assert vm_metrics.DEFAULT_URL == "http://localhost:8428"
