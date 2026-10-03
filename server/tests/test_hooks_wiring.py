"""Tests for the lifecycle hooks wiring in server/modal_service.py and
server/modal_inference_dashboard.py (vendored seam in server/libs/hooks.py).

modal_service imports the `modal` SDK at module level, and the boot helpers
spawn real engines, so the wiring here is verified at two granularities:
structural (the instance + every fire site, statically) and behavioral
(ordered fire + contained errors through the REAL FastAPI proxy handler,
and directly against the shared instance). Tests use sys.path-insert flat
imports, matching test_vm_metrics.py.
"""

from __future__ import annotations

import ast
import contextlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import modal_service
from libs.hooks import Hooks  # noqa: F401  (importable-seam contract: modules bind this class)
from modal_inference_dashboard import build_dashboard_api, dashboard_hooks
from modal_service import DEPLOYED_PROFILE

SVC_TAGS = ("lane.boot.pre", "lane.boot.post", "request.pre", "request.post", "inject.pre")
DASH_TAGS = ("dashboard.boot.pre", "dashboard.boot.post")


@pytest.fixture()
def svc_calls():
    """Record (tag, args) per service-hook fire; isolate registry between tests."""
    calls: list[tuple[str, tuple[object, ...]]] = []
    for tag in modal_service.hooks.tags:
        modal_service.hooks.register(tag, lambda *args, _tag=tag, _calls=calls: _calls.append((_tag, args)))
    yield calls
    modal_service.hooks.clear()


def test_service_instance_declares_the_closed_tag_set() -> None:
    assert modal_service.hooks.name == "modal-inference-server"
    assert modal_service.hooks.tags == SVC_TAGS
    # The wiring module documents the closed set, per the family contract.
    doc = pathlib.Path(modal_service.__file__).read_text()
    for tag in SVC_TAGS:
        assert tag in doc, f"{tag} undocumented in modal_service.py"


def test_dashboard_instance_declares_the_closed_tag_set() -> None:
    assert dashboard_hooks.name == "modal-inference-dashboard"
    assert dashboard_hooks.tags == DASH_TAGS
    assert modal_service.hooks is not dashboard_hooks  # separate spaces
    doc = pathlib.Path(sys.modules["modal_inference_dashboard"].__file__).read_text()
    for tag in DASH_TAGS:
        assert tag in doc, f"{tag} undocumented in modal_inference_dashboard.py"


def test_boot_helper_fire_payloads_are_structurally_wired() -> None:
    """Each _boot_* helper fires lane.boot.pre at entry and .post with ok=True.

    The helpers spawn real engine subprocesses, so payload correctness is
    verified statically rather than by executing a boot.
    """
    tree = ast.parse(pathlib.Path(modal_service.__file__).read_text())
    helpers = {
        "_boot_llama_single": "llama-single",
        "_boot_llama_router": "llama-router",
        "_boot_ollama": "ollama",
        "_boot_vllm": "vllm",
    }
    seen: dict[str, list[str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name in helpers):
            continue
        fires = [n for n in ast.walk(node) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fire"]
        tags = [f.args[0].value for f in fires if isinstance(f.args[0], ast.Constant)]
        seen[node.name] = tags
        assert tags == ["lane.boot.pre", "lane.boot.post"], (node.name, tags)
        post = next(f for f in fires if f.args[0].value == "lane.boot.post")
        payload = post.args[1]
        assert isinstance(payload, ast.Dict)
        values = {k.value: v for k, v in zip(payload.keys, payload.values, strict=True)}
        assert list(values) == ["runtime", "profile", "ok"]
        assert values["runtime"].value == helpers[node.name]
        assert isinstance(values["ok"], ast.Constant) and values["ok"].value is True
        pre = next(f for f in fires if f.args[0].value == "lane.boot.pre")
        pre_keys = [k.value for k in pre.args[1].keys]  # type: ignore[union-attr]
        assert pre_keys == ["runtime", "profile"]
    assert set(seen) == set(helpers)


def test_proxy_fire_sites_cover_every_return_path() -> None:
    """request.pre at proxy entry; request.post precedes every proxy return."""
    tree = ast.parse(pathlib.Path(modal_service.__file__).read_text())
    proxy = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "proxy")
    fires = [n for n in ast.walk(proxy) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fire"]
    pres = [f for f in fires if f.args[0].value == "request.pre"]
    posts = [f for f in fires if f.args[0].value == "request.post"]
    assert len(pres) == 1
    assert len(posts) >= len([n for n in ast.walk(proxy) if isinstance(n, ast.Return) and n.value])
    for node in [n for n in ast.walk(proxy) if isinstance(n, ast.Return) and n.value]:
        window = "\n".join(src_lines[node.lineno - 5 : node.lineno])
        assert 'hooks.fire("request.post"' in window, f"return at {node.lineno} unfired"


src_lines = pathlib.Path(modal_service.__file__).read_text().splitlines()


def _build_proxy_api():
    """The real FastAPI app from VLLMServer.web with stubbed upstreams.

    client is a closure over an AsyncClient pointed at 127.0.0.1:8000; no
    engine is up, so any forwarded request takes the 502 path on a real
    send. The test only needs gate + fire ordering, which precedes that.
    """
    user_cls = modal_service.VLLMServer._get_user_cls()
    web_fn = user_cls.web._get_raw_f()

    class _FakeSelf:
        process = None

    return web_fn(_FakeSelf())


@pytest.fixture()
def proxy_client(tmp_path, monkeypatch):
    """Real proxy app with the usage-ledger redirected into tmp_path.

    The 502 path appends a ledger record under USAGE_LEDGER_DIR (a Volume
    path in-container, read-only on the laptop); the hook contract under
    test is fire behavior, not the ledger, so point the module constant at
    a temp dir and let the real append run. Auth is ENFORCED here with a
    known proxy token so both branches are testable: wrong bearer = 401
    (the deny path fires), good bearer = forwarded (the 502 laptop path).
    """
    from fastapi.testclient import TestClient

    ledger = tmp_path / "usage-events"
    monkeypatch.setattr(modal_service, "USAGE_LEDGER_DIR", str(ledger))
    monkeypatch.setattr(modal_service, "PROXY_AUTH_ENFORCED", True)
    monkeypatch.setenv("MODAL_PROXY_TOKEN", "test-token")  # web() reads it at build
    api = _build_proxy_api()
    with TestClient(api) as client:
        yield client


def _auth() -> dict[str, str]:
    return {"authorization": "Bearer test-token"}


def test_proxy_fires_pre_then_post_with_upstream_status(svc_calls, proxy_client) -> None:
    response = proxy_client.get("/v1/models", headers=_auth())
    assert response.status_code == 502  # no engine up: upstream send fails
    assert [tag for tag, _args in svc_calls] == ["request.pre", "request.post"]
    assert svc_calls[0][1] == ("GET", "/v1/models")
    assert svc_calls[1][1] == ("GET", "/v1/models", 502)


def test_proxy_unauthorized_still_fires_post(svc_calls, tmp_path, monkeypatch) -> None:
    """A rejected request still sees pre -> post(401): the fire wraps the deny.

    proxy_token + PROXY_AUTH_ENFORCED are read when web() builds the app, so
    this test patches BOTH module constants (and the env the token comes
    from) BEFORE building its own app - the shared fixture builds enforcement
    OFF, where the 401 branch is unreachable.
    """
    monkeypatch.setattr(modal_service, "PROXY_AUTH_ENFORCED", True)
    monkeypatch.setattr(
        modal_service.os, "getenv", lambda name, default="": "tok" if name == "MODAL_PROXY_TOKEN" else default
    )
    monkeypatch.setattr(modal_service, "USAGE_LEDGER_DIR", str(tmp_path / "usage-events-401"))
    api = _build_proxy_api()
    from fastapi.testclient import TestClient

    with TestClient(api) as client:
        response = client.get("/health", headers={"authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert [tag for tag, _args in svc_calls] == ["request.pre", "request.post"]
    assert svc_calls[1][1] == ("GET", "/health", 401)


def test_raising_request_pre_does_not_break_the_proxy(svc_calls, proxy_client) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("hook blew up")

    modal_service.hooks.register("request.pre", boom)
    response = proxy_client.get("/v1/models", headers=_auth())
    assert response.status_code == 502  # host path proceeds
    errors = modal_service.hooks.last_errors("request.pre")
    assert errors and "RuntimeError: hook blew up" in errors[0]
    # the contained failure still reaches the .post fire
    assert [tag for tag, _args in svc_calls][-1] == "request.post"


def test_raising_request_post_does_not_break_the_proxy(proxy_client) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("hook blew up")

    modal_service.hooks.register("request.post", boom)
    response = proxy_client.get("/v1/models", headers=_auth())
    assert response.status_code == 502
    assert modal_service.hooks.last_errors("request.post")


class _StubUpstream:
    """A real loopback HTTP server standing in for the local engine.

    Records the JSON body of the last POST it receives, so a test can assert
    what the proxy actually forwarded (the inject seam's observable effect).
    Runs on its own thread; no library internals are patched.
    """

    def __init__(self) -> None:
        import http.server
        import threading

        captured: dict[str, object] = {"body": None}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                with contextlib.suppress(json.JSONDecodeError):
                    captured["body"] = json.loads(raw)
                payload = json.dumps({"id": "x", "choices": [], "usage": {}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:  # silence stderr noise
                return

        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.captured = captured
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TestInjectSeam:
    """inject.pre: the body-aware, MUTABLE chat seam (shadow memory's door)."""

    @pytest.fixture(autouse=True)
    def _clean_inject(self):
        """Registration/error memory is process-global: isolate per test."""
        yield
        modal_service.hooks.clear("inject.pre")

    @pytest.fixture()
    def stub_proxy(self, monkeypatch, tmp_path):
        """A proxy app whose upstream is a recording stub server."""
        from fastapi.testclient import TestClient

        stub = _StubUpstream()
        monkeypatch.setattr(modal_service, "USAGE_LEDGER_DIR", str(tmp_path / "usage-events"))
        monkeypatch.setattr(modal_service, "PROXY_AUTH_ENFORCED", True)
        monkeypatch.setenv("MODAL_PROXY_TOKEN", "test-token")
        # The intended knob (ENGINE_BASE_URL), not a library-internal patch.
        monkeypatch.setattr(modal_service, "ENGINE_BASE_URL", stub.url)
        api = _build_proxy_api()
        try:
            with TestClient(api) as client:
                yield client, stub
        finally:
            stub.stop()

    def _post_chat(self, client, body, headers=None):
        return client.post(
            "/v1/chat/completions",
            json=body,
            headers={**_auth(), **(headers or {})},
        )

    def test_handler_mutates_messages_and_body_reaches_upstream(self, stub_proxy):
        def _inject(payload, request):
            payload.setdefault("messages", []).insert(0, {"role": "system", "content": "shadow: prior turn recalled"})

        modal_service.hooks.register("inject.pre", _inject)
        client, stub = stub_proxy
        body = {"model": "solo", "messages": [{"role": "user", "content": "hi"}]}
        response = self._post_chat(client, body)
        assert response.status_code == 200
        # What the upstream actually received carries the injected message.
        sent = stub.captured["body"]
        assert sent["messages"][0] == {"role": "system", "content": "shadow: prior turn recalled"}
        assert sent["messages"][1]["role"] == "user"

    def test_inject_and_stream_options_chain(self, stub_proxy):
        def _inject(payload, request):
            payload.setdefault("messages", []).insert(0, {"role": "system", "content": "x"})

        modal_service.hooks.register("inject.pre", _inject)
        client, stub = stub_proxy
        body = {"model": "solo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        self._post_chat(client, body, headers={"accept": "text/event-stream"})
        # Both passes must land: injected message AND include_usage.
        sent = stub.captured["body"]
        assert sent["messages"][0]["role"] == "system"
        assert sent["stream_options"]["include_usage"] is True

    def test_not_fired_when_no_handler_registered(self, stub_proxy):
        # Zero overhead path: nothing registered -> body forwarded byte-identical.
        client, stub = stub_proxy
        body = {"model": "solo", "messages": [{"role": "user", "content": "hi"}]}
        self._post_chat(client, body)
        assert stub.captured["body"] == body

    def test_raising_inject_handler_does_not_break_the_proxy(self, stub_proxy):
        def boom(*_args: object) -> None:
            raise RuntimeError("inject blew up")

        modal_service.hooks.register("inject.pre", boom)
        client, stub = stub_proxy
        response = self._post_chat(client, {"model": "solo", "messages": [{"role": "user", "content": "hi"}]})
        # Contained: the request still reaches upstream (200) with the original body.
        assert response.status_code == 200
        assert stub.captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
        assert modal_service.hooks.last_errors("inject.pre")


def test_lane_boot_hooks_fire_in_order_around_a_stubbed_boot(svc_calls, tmp_path, monkeypatch):
    """Cheap direct exercise: the helper's own fire lines around a stubbed boot.

    The engine spawn is monkeypatched (subprocess.Popen + the store/heartbeat/
    measure seams) so the helper runs to its success tail in milliseconds;
    nothing touches the read-only container paths (/usage, /root, /models).
    """
    import llama_router

    class _FakeProcess:
        pid = 0

        def poll(self) -> None:
            return 0

    store = tmp_path / "ollama-store"
    (store / "blobs").mkdir(parents=True)
    blobs_dir = store / "blobs"

    def fake_store() -> str:
        return str(store)

    monkeypatch.setattr(modal_service, "_ollama_serve_store", fake_store)
    monkeypatch.setattr(llama_router, "gguf_path_for", lambda store_root, alias: blobs_dir / "sha256-fake")
    (blobs_dir / "sha256-fake").touch()
    monkeypatch.setattr(modal_service.subprocess, "Popen", lambda *a, **kw: _FakeProcess())
    monkeypatch.setattr(modal_service, "_serve_with_heartbeat", lambda *a: None)
    monkeypatch.setattr(modal_service, "_measure_router_slots", lambda *a: None)
    monkeypatch.setattr(modal_service, "_register_gpu_container", lambda: None)
    monkeypatch.setattr(modal_service, "_llama_binary", lambda: pathlib.Path("/bin/true"))
    monkeypatch.setattr(modal_service, "_require_cuda_device", lambda *a: None)
    monkeypatch.setattr(modal_service, "_llama_single_args", lambda *a: ["x"])
    resolved = {"members": [{"alias": "gemma"}], "gpu_count": 1}
    modal_service._boot_llama_single(resolved, [], 5)
    assert [tag for tag, _payload in svc_calls] == ["lane.boot.pre", "lane.boot.post"]
    assert svc_calls[0][1][0] == {"runtime": "llama-single", "profile": DEPLOYED_PROFILE}
    assert svc_calls[1][1][0] == {"runtime": "llama-single", "profile": DEPLOYED_PROFILE, "ok": True}


def test_dashboard_boot_hooks_fire_around_the_factory() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    for tag in dashboard_hooks.tags:
        dashboard_hooks.register(tag, lambda *args, _tag=tag, _calls=calls: _calls.append((_tag, args)))
    try:
        api = build_dashboard_api()
        assert isinstance(api.title, str) and api.title.endswith("dashboard")
    finally:
        dashboard_hooks.clear()
    assert [tag for tag, _args in calls] == ["dashboard.boot.pre", "dashboard.boot.post"]
    assert calls[1][1][0] == {"profile": "", "ok": True}


def test_unknown_tag_is_refused_by_both_instances() -> None:
    with pytest.raises(ValueError, match="unknown hook tag"):
        modal_service.hooks.register("lane.boot.pre.pre", lambda payload: None)
    with pytest.raises(ValueError, match="unknown hook tag"):
        dashboard_hooks.register("dashboard.boot.post.post", lambda payload: None)
