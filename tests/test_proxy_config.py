from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web


@pytest.fixture(scope="module")
def modules():
    root = Path(__file__).resolve().parents[1]
    package_name = "lm_remote_proxy_test_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package

    loaded = {}
    for name in ("config", "remote_client", "proxy"):
        full_name = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(full_name, root / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        loaded[name] = module

    yield types.SimpleNamespace(**loaded)
    for name in ("proxy", "remote_client", "config"):
        sys.modules.pop(f"{package_name}.{name}", None)
    sys.modules.pop(package_name, None)


class DummyContent:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FragmentedContent:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)

    async def read(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        self.chunks.insert(0, chunk[size:])
        return chunk[:size]


class DummyRequest:
    def __init__(
        self,
        method="GET",
        path="/",
        payload=None,
        headers=None,
        *,
        raw_body: bytes | None = None,
        content_length: int | None | object = ...,
    ):
        self.method = method
        self.path = path
        self._payload = payload
        self.headers = headers or {}
        self.query_string = ""
        self.can_read_body = payload is not None
        self.content_type = (
            "application/json" if payload is not None else "application/octet-stream"
        )
        self._body = (
            raw_body
            if raw_body is not None
            else json.dumps(payload).encode("utf-8")
            if payload is not None
            else b""
        )
        self.content = DummyContent(self._body)
        if content_length is ...:
            self.content_length = len(self._body) if self._body else None
        else:
            self.content_length = content_length
        if raw_body is not None:
            self.content_type = "application/json"

    async def json(self):
        return self._payload

    async def read(self):
        return self._body


def response_json(response: web.Response) -> dict:
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture
def isolated_proxy(modules, tmp_path, monkeypatch):
    config = modules.config.RemoteConfig(
        tmp_path / "user" / "config.json",
        tmp_path / "missing-legacy.json",
        environ={},
    )
    monkeypatch.setattr(modules.proxy, "remote_config", config)
    monkeypatch.setattr(modules.remote_client, "remote_config", config)
    modules.proxy.RemoteLoraClient._instance = None
    modules.proxy._proxy_sessions = {}
    modules.proxy._proxy_session_lock = None
    modules.proxy._active_proxy_websockets = set()
    modules.proxy._active_proxy_websockets_lock = None
    return modules.proxy, config


@pytest.mark.asyncio
async def test_config_endpoint_saves_and_hot_enables(isolated_proxy):
    proxy, config = isolated_proxy
    get_response = await proxy._handle_config(DummyRequest())
    initial = response_json(get_response)
    assert initial["configured"]["remote_url"] == ""

    put_response = await proxy._handle_config(
        DummyRequest(
            "PUT",
            proxy._CONFIG_ROUTE,
            {
                "revision": initial["revision"],
                "config": {
                    "remote_url": "http://manager.local:8188/",
                    "timeout": 40,
                    "path_mappings": {"/remote": "/local"},
                },
            },
        )
    )
    payload = response_json(put_response)
    assert put_response.status == 200
    assert payload["effective"]["remote_url"] == "http://manager.local:8188"
    assert payload["restart_required"] is False
    assert config.is_configured


@pytest.mark.asyncio
async def test_middleware_falls_through_when_disabled_then_uses_new_url(
    isolated_proxy, monkeypatch
):
    proxy, config = isolated_proxy

    async def local_handler(request):
        return web.Response(text="local")

    disabled_response = await proxy.lm_remote_proxy_middleware(
        DummyRequest(path="/loras"), local_handler
    )
    assert disabled_response.text == "local"

    state = config.as_dict()
    config.save(
        {"remote_url": "http://manager.local", "timeout": 30, "path_mappings": {}},
        expected_revision=state["revision"],
    )
    captured = {}

    async def fake_proxy_http(request, snapshot):
        captured["url"] = snapshot.remote_url
        return web.Response(text="remote")

    monkeypatch.setattr(proxy, "_proxy_http", fake_proxy_http)
    enabled_response = await proxy.lm_remote_proxy_middleware(
        DummyRequest(path="/loras"), local_handler
    )
    assert enabled_response.text == "remote"
    assert captured["url"] == "http://manager.local"


@pytest.mark.asyncio
async def test_config_endpoint_rejects_changes_to_environment_managed_field(
    modules, tmp_path, monkeypatch
):
    config = modules.config.RemoteConfig(
        tmp_path / "user.json",
        tmp_path / "legacy.json",
        environ={"LM_REMOTE_URL": "http://managed.local"},
    )
    monkeypatch.setattr(modules.proxy, "remote_config", config)
    initial = config.as_dict()
    response = await modules.proxy._handle_config(
        DummyRequest(
            "PUT",
            modules.proxy._CONFIG_ROUTE,
            {
                "revision": initial["revision"],
                "config": {
                    "remote_url": "http://changed.local",
                    "timeout": 30,
                    "path_mappings": {},
                },
            },
        )
    )
    payload = response_json(response)
    assert response.status == 409
    assert payload["field"] == "remote_url"
    assert "LM_REMOTE_URL" in payload["error"]


@pytest.mark.asyncio
async def test_conflict_reload_rotates_runtime_generation(isolated_proxy, monkeypatch):
    proxy, config = isolated_proxy
    initial = config.as_dict()
    config._config_file.parent.mkdir(parents=True, exist_ok=True)
    config._config_file.write_text(
        json.dumps(
            {
                "remote_url": "http://external.local",
                "timeout": 20,
                "path_mappings": {},
            }
        ),
        encoding="utf-8",
    )
    retired = []
    rotated = []

    async def fake_retire(generation):
        retired.append(generation)

    async def fake_rotate(generation):
        rotated.append(generation)

    monkeypatch.setattr(proxy, "_retire_proxy_sessions", fake_retire)
    monkeypatch.setattr(proxy, "_rotate_active_websockets", fake_rotate)
    response = await proxy._handle_config(
        DummyRequest(
            "PUT",
            proxy._CONFIG_ROUTE,
            {
                "revision": initial["revision"],
                "config": {
                    "remote_url": "http://browser.local",
                    "timeout": 30,
                    "path_mappings": {},
                },
            },
        )
    )
    payload = response_json(response)

    assert response.status == 409
    assert payload["latest"]["effective"]["remote_url"] == "http://external.local"
    assert retired == [config.generation]
    assert rotated == [config.generation]


@pytest.mark.asyncio
async def test_connection_test_uses_unsaved_draft(isolated_proxy, monkeypatch):
    proxy, _ = isolated_proxy
    captured = {}

    async def fake_test(remote_url, timeout):
        captured.update(remote_url=remote_url, timeout=timeout)
        return 17

    monkeypatch.setattr(proxy, "_perform_connection_test", fake_test)
    response = await proxy._handle_test_connection(
        DummyRequest(
            "POST",
            proxy._TEST_CONNECTION_ROUTE,
            {"remote_url": "http://draft.local:8188/", "timeout": 8},
        )
    )
    payload = response_json(response)
    assert response.status == 200
    assert payload["latency_ms"] == 17
    assert captured == {"remote_url": "http://draft.local:8188", "timeout": 8}


@pytest.mark.asyncio
async def test_connection_test_rejects_empty_url(isolated_proxy):
    proxy, _ = isolated_proxy
    response = await proxy._handle_test_connection(
        DummyRequest(
            "POST",
            proxy._TEST_CONNECTION_ROUTE,
            {"remote_url": "", "timeout": 30},
        )
    )
    assert response.status == 400
    assert response_json(response)["field"] == "remote_url"


@pytest.mark.asyncio
async def test_connection_response_reader_accepts_fragmented_json(isolated_proxy):
    proxy, _ = isolated_proxy
    response = types.SimpleNamespace(
        content=FragmentedContent([b'{"sta', b'tus":"', b'ok"}'])
    )

    assert await proxy._read_small_json(response) == {"status": "ok"}


@pytest.mark.asyncio
async def test_connection_response_reader_enforces_hard_limit(isolated_proxy):
    proxy, _ = isolated_proxy
    response = types.SimpleNamespace(
        content=FragmentedContent([b" " * proxy._MAX_TEST_RESPONSE, b" "])
    )

    with pytest.raises(proxy._ConnectionTestError, match="unexpectedly large"):
        await proxy._read_small_json(response)


@pytest.mark.asyncio
async def test_proxy_loop_header_is_rejected(isolated_proxy):
    proxy, config = isolated_proxy
    state = config.as_dict()
    config.save(
        {"remote_url": "http://manager.local", "timeout": 30, "path_mappings": {}},
        expected_revision=state["revision"],
    )

    async def local_handler(request):
        return web.Response(text="local")

    response = await proxy.lm_remote_proxy_middleware(
        DummyRequest(
            path="/api/lm/health-check",
            headers={proxy._PROXY_HOP_HEADER: "1"},
        ),
        local_handler,
    )
    assert response.status == 508


def test_register_proxy_is_available_while_unconfigured(isolated_proxy):
    proxy, config = isolated_proxy
    assert not config.is_configured
    app = web.Application()
    proxy.register_proxy(app)
    assert proxy.lm_remote_proxy_middleware in app.middlewares
    assert proxy._cleanup_proxy_session in app.on_shutdown


def test_register_proxy_keeps_existing_security_middleware_first(isolated_proxy):
    proxy, _ = isolated_proxy

    @web.middleware
    async def security_guard(request, handler):
        return await handler(request)

    app = web.Application(middlewares=[security_guard])
    proxy.register_proxy(app)

    assert list(app.middlewares) == [security_guard, proxy.lm_remote_proxy_middleware]


@pytest.mark.asyncio
async def test_chunked_config_body_is_hard_limited(isolated_proxy):
    proxy, _ = isolated_proxy
    response = await proxy._handle_config(
        DummyRequest(
            "PUT",
            proxy._CONFIG_ROUTE,
            raw_body=b" " * (proxy._MAX_CONFIG_BODY + 1),
            content_length=None,
        )
    )

    assert response.status == 413


@pytest.mark.asyncio
async def test_remote_client_fetches_all_pages_beyond_server_cap(
    isolated_proxy, modules, monkeypatch
):
    _, config = isolated_proxy
    client = modules.remote_client.RemoteLoraClient()
    calls = []

    async def fake_get_json(path, params=None, *, snapshot=None):
        page = int(params["page"])
        calls.append((page, int(params["page_size"]), snapshot.generation))
        start = (page - 1) * 100
        count = 100 if page == 1 else 55
        return {
            "items": [
                {"file_name": f"model-{index}"} for index in range(start, start + count)
            ],
            "total_pages": 2,
        }

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    items = await client._get_lora_list_cached(snapshot=config.snapshot)

    assert len(items) == 155
    assert items[-1]["file_name"] == "model-154"
    assert [call[:2] for call in calls] == [(1, 100), (2, 100)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_path"),
    [
        ("_get_lora_list_cached", "/api/lm/loras/list"),
        ("_get_checkpoint_list_cached", "/api/lm/checkpoints/list"),
    ],
)
async def test_successful_empty_listing_is_cached(
    isolated_proxy, modules, monkeypatch, method_name, expected_path
):
    _, config = isolated_proxy
    client = modules.remote_client.RemoteLoraClient()
    calls = []

    async def fake_get_all_pages(path, *, snapshot):
        calls.append((path, snapshot.generation))
        return []

    monkeypatch.setattr(client, "_get_all_pages", fake_get_all_pages)
    cached_method = getattr(client, method_name)

    assert await cached_method(snapshot=config.snapshot) == []
    assert await cached_method(snapshot=config.snapshot) == []
    assert calls == [(expected_path, config.generation)]


@pytest.mark.asyncio
async def test_stale_lookup_never_returns_another_generations_cache(
    isolated_proxy, modules, monkeypatch
):
    _, config = isolated_proxy
    client = modules.remote_client.RemoteLoraClient()
    client._lora_cache = [{"file_name": "new-generation"}]
    client._lora_cache_generation = config.generation
    old_snapshot = modules.config.ConfigSnapshot(
        config.generation - 1,
        "http://old.local",
        30,
        (("/remote", "/old-local"),),
    )

    async def fail_fetch(path, *, snapshot):
        raise OSError("old server unavailable")

    monkeypatch.setattr(client, "_get_all_pages", fail_fetch)

    assert await client._get_lora_list_cached(snapshot=old_snapshot) == []


@pytest.mark.asyncio
async def test_lora_info_maps_with_the_fetch_generation(
    isolated_proxy, modules, monkeypatch
):
    _, config = isolated_proxy
    initial = config.as_dict()
    config.save(
        {
            "remote_url": "http://one.local",
            "timeout": 30,
            "path_mappings": {"/remote": "/local-one"},
        },
        expected_revision=initial["revision"],
    )
    client = modules.remote_client.RemoteLoraClient()

    async def fake_list(*, snapshot=None):
        latest = config.as_dict()
        config.save(
            {
                "remote_url": "http://two.local",
                "timeout": 30,
                "path_mappings": {"/remote": "/local-two"},
            },
            expected_revision=latest["revision"],
        )
        return [
            {
                "file_name": "portrait",
                "file_path": "/remote/portrait.safetensors",
                "folder": "",
                "civitai": {},
            }
        ]

    monkeypatch.setattr(client, "_get_lora_list_cached", fake_list)
    monkeypatch.setattr(client, "_relative_lora_path", lambda path, folder: path)

    relative, _ = await client.get_lora_info("portrait")

    assert relative == "/local-one/portrait.safetensors"


class FakeSession:
    def __init__(self, *args, **kwargs):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_session_does_not_retain_remote_cookies(
    isolated_proxy, monkeypatch
):
    proxy, config = isolated_proxy
    captured = {}

    def create_session(*args, **kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(proxy.aiohttp, "ClientSession", create_session)
    lease = proxy._proxy_session_lease(config.snapshot)
    await lease.__aenter__()
    await lease.__aexit__(None, None, None)

    assert isinstance(captured["cookie_jar"], proxy.aiohttp.DummyCookieJar)
    await proxy._close_all_proxy_sessions()


@pytest.mark.asyncio
async def test_proxy_session_rotation_waits_for_inflight_request(
    isolated_proxy, monkeypatch
):
    proxy, config = isolated_proxy
    created = []

    def create_session(*args, **kwargs):
        session = FakeSession()
        created.append(session)
        return session

    monkeypatch.setattr(proxy.aiohttp, "ClientSession", create_session)
    old_snapshot = config.snapshot
    old_lease = proxy._proxy_session_lease(old_snapshot)
    old_session = await old_lease.__aenter__()

    state = config.as_dict()
    new_snapshot = config.save(
        {"remote_url": "http://new.local", "timeout": 30, "path_mappings": {}},
        expected_revision=state["revision"],
    )
    await proxy._retire_proxy_sessions(new_snapshot.generation)
    assert old_session.closed is False

    new_lease = proxy._proxy_session_lease(new_snapshot)
    new_session = await new_lease.__aenter__()
    assert new_session is not old_session
    await new_lease.__aexit__(None, None, None)
    assert new_session.closed is False

    await old_lease.__aexit__(None, None, None)
    assert old_session.closed is True
    assert new_session.closed is False

    await proxy._close_all_proxy_sessions()
    assert new_session.closed is True


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.close_code = None

    async def close(self, *, code=None, message=None):
        self.closed = True
        self.close_code = code


@pytest.mark.asyncio
async def test_websocket_rotation_closes_only_retired_generation(isolated_proxy):
    proxy, config = isolated_proxy
    current_generation = config.generation
    old_bridge = proxy._ActiveWebSocket(
        current_generation - 1, FakeWebSocket(), FakeSession()
    )
    current_bridge = proxy._ActiveWebSocket(
        current_generation, FakeWebSocket(), FakeSession()
    )
    proxy._active_proxy_websockets.update({old_bridge, current_bridge})

    await proxy._rotate_active_websockets(current_generation)

    assert old_bridge.local_ws.closed is True
    assert old_bridge.local_ws.close_code == 1012
    assert old_bridge.session.closed is True
    assert current_bridge.local_ws.closed is False
    assert proxy._active_proxy_websockets == {current_bridge}

    await proxy._rotate_active_websockets(None)
    assert current_bridge.local_ws.closed is True
    assert current_bridge.session.closed is True


def test_mapped_local_path_is_resolved_against_comfy_lora_roots(
    modules, tmp_path, monkeypatch
):
    root = tmp_path / "models" / "loras"
    fake_folder_paths = types.SimpleNamespace(
        get_folder_paths=lambda model_type: [str(root)]
    )
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)
    relative = modules.remote_client.RemoteLoraClient._relative_lora_path(
        str(root / "styles" / "portrait.safetensors"),
        "wrong-remote-folder",
    )
    assert relative == "styles/portrait.safetensors"
