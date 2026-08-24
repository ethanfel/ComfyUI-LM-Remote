"""
Reverse-proxy middleware that forwards LoRA Manager requests to the remote instance.

Registered as an aiohttp middleware on PromptServer.instance.app.  It intercepts
requests matching known LoRA Manager URL prefixes and proxies them to the remote
Docker instance.  Non-matching requests fall through to the regular ComfyUI router.

Routes that use ``send_sync`` are handled locally so that events are broadcast
to the local ComfyUI frontend (the remote instance has no connected browsers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from aiohttp import web, WSMsgType

from .config import (
    ConfigConflictError,
    ConfigSnapshot,
    ConfigValidationError,
    remote_config,
)
from .remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)

_CONFIG_ROUTE = "/api/lm-remote/config"
_TEST_CONNECTION_ROUTE = "/api/lm-remote/test-connection"
_PROXY_HOP_HEADER = "X-LM-Remote-Proxy"
_MAX_CONFIG_BODY = 64 * 1024
_MAX_TEST_RESPONSE = 64 * 1024

# ---------------------------------------------------------------------------
# URL prefixes that should be forwarded to the remote LoRA Manager
# ---------------------------------------------------------------------------
_PROXY_PREFIXES = (
    "/api/lm/",
    "/loras_static/",
    "/locales/",
    "/example_images_static/",
    "/extensions/ComfyUI-Lora-Manager/",
)

# Page routes served by the standalone LoRA Manager web UI
_PROXY_PAGE_ROUTES = {
    "/loras",
    "/checkpoints",
    "/embeddings",
    "/loras/recipes",
    "/community",
    "/statistics",
}

# WebSocket endpoints to proxy
_WS_ROUTES = {
    "/ws/fetch-progress",
    "/ws/download-progress",
    "/ws/init-progress",
}

# ---------------------------------------------------------------------------
# Local handlers for routes that need send_sync (event broadcasting)
# ---------------------------------------------------------------------------
# These routes are NOT proxied.  They are handled locally so that events
# reach the local ComfyUI frontend via PromptServer.send_sync().


def _get_prompt_server():
    """Lazily import PromptServer to avoid circular imports at module level."""
    from server import PromptServer  # type: ignore

    return PromptServer.instance


def _parse_node_id(entry):
    """Parse a node ID entry that can be int, string, or dict.

    Returns (parsed_id, graph_id_or_None).
    """
    node_identifier = entry
    graph_identifier = None
    if isinstance(entry, dict):
        node_identifier = entry.get("node_id")
        graph_identifier = entry.get("graph_id")

    try:
        parsed_id = int(node_identifier)
    except (TypeError, ValueError):
        parsed_id = node_identifier

    return parsed_id, graph_identifier


async def _handle_get_trigger_words(request: web.Request) -> web.Response:
    """Fetch trigger words from remote and broadcast via send_sync."""
    try:
        data = await request.json()
        lora_names = data.get("lora_names", [])
        node_ids = data.get("node_ids", [])

        client = RemoteLoraClient.get_instance()
        server = _get_prompt_server()

        # Collect trigger words for ALL loras into a single combined list,
        # then broadcast the same combined text to ALL node_ids.
        all_trigger_words = []
        for lora_name in lora_names:
            _, trigger_words = await client.get_lora_info(lora_name)
            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        for entry in node_ids:
            parsed_id, graph_id = _parse_node_id(entry)
            payload = {"id": parsed_id, "message": trigger_words_text}
            if graph_id is not None:
                payload["graph_id"] = str(graph_id)
            server.send_sync("trigger_word_update", payload)

        return web.json_response({"success": True})
    except Exception as exc:
        logger.error("[LM-Remote] Error getting trigger words: %s", exc)
        return web.json_response({"success": False, "error": str(exc)}, status=500)


async def _handle_update_lora_code(request: web.Request) -> web.Response:
    """Parse lora code update and broadcast via send_sync."""
    data = await request.json()
    node_ids = data.get("node_ids")
    lora_code = data.get("lora_code", "")
    mode = data.get("mode", "append")

    server = _get_prompt_server()

    if node_ids is None:
        # Broadcast to all nodes
        server.send_sync(
            "lora_code_update",
            {"id": -1, "lora_code": lora_code, "mode": mode},
        )
    else:
        for entry in node_ids:
            parsed_id, graph_id = _parse_node_id(entry)
            payload = {"id": parsed_id, "lora_code": lora_code, "mode": mode}
            if graph_id is not None:
                payload["graph_id"] = str(graph_id)
            server.send_sync("lora_code_update", payload)

    return web.json_response({"success": True})


async def _handle_update_node_widget(request: web.Request) -> web.Response:
    """Parse widget update and broadcast via send_sync."""
    data = await request.json()
    widget_name = data.get("widget_name")
    value = data.get("value")
    node_ids = data.get("node_ids")

    if not widget_name or value is None or not node_ids:
        return web.json_response(
            {"error": "widget_name, value, and node_ids are required"},
            status=400,
        )

    server = _get_prompt_server()

    for entry in node_ids:
        parsed_id, graph_id = _parse_node_id(entry)
        payload = {"id": parsed_id, "widget_name": widget_name, "value": value}
        if graph_id is not None:
            payload["graph_id"] = str(graph_id)
        server.send_sync("lm_widget_update", payload)

    return web.json_response({"success": True})


async def _handle_register_nodes(request: web.Request) -> web.Response:
    """No-op handler — node registration is not needed in remote mode."""
    return web.json_response({"success": True, "message": "No-op in remote mode"})


# Dispatch table for send_sync routes
_SEND_SYNC_HANDLERS = {
    "/api/lm/loras/get_trigger_words": _handle_get_trigger_words,
    "/api/lm/update-lora-code": _handle_update_lora_code,
    "/api/lm/update-node-widget": _handle_update_node_widget,
    "/api/lm/register-nodes": _handle_register_nodes,
}


def _config_response() -> web.Response:
    return web.json_response({"success": True, **remote_config.as_dict()})


class _RequestBodyTooLarge(ValueError):
    pass


async def _read_limited_json(request: web.Request) -> object:
    """Read JSON without ever buffering more than the configuration limit."""
    if request.content_length is not None and request.content_length > _MAX_CONFIG_BODY:
        raise _RequestBodyTooLarge

    body = bytearray()
    while True:
        remaining = _MAX_CONFIG_BODY + 1 - len(body)
        chunk = await request.content.read(min(8192, remaining))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > _MAX_CONFIG_BODY:
            raise _RequestBodyTooLarge
    return json.loads(body)


async def _activate_runtime_generation() -> None:
    """Invalidate and rotate all resources tied to the previous snapshot."""
    RemoteLoraClient.get_instance().invalidate_caches()
    await _retire_proxy_sessions(remote_config.generation)
    await _rotate_active_websockets(remote_config.generation)


async def _handle_config(request: web.Request) -> web.Response:
    """Read or atomically replace LM Remote's server-side configuration."""
    if request.method == "GET":
        return _config_response()
    if request.method != "PUT":
        return web.json_response(
            {"success": False, "error": "Method not allowed."},
            status=405,
            headers={"Allow": "GET, PUT"},
        )
    if request.content_type != "application/json":
        return web.json_response(
            {"success": False, "error": "Content-Type must be application/json."},
            status=415,
        )

    try:
        payload = await _read_limited_json(request)
    except (_RequestBodyTooLarge, web.HTTPRequestEntityTooLarge):
        return web.json_response(
            {"success": False, "error": "Configuration request is too large."},
            status=413,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(
            {"success": False, "error": "Request body is not valid JSON."}, status=400
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"success": False, "error": "Request body must be an object."}, status=400
        )
    unknown = set(payload) - {"revision", "config"}
    if unknown:
        return web.json_response(
            {
                "success": False,
                "error": f"Unknown request field(s): {', '.join(sorted(unknown))}",
            },
            status=400,
        )

    revision = payload.get("revision")
    proposed = payload.get("config")
    if not isinstance(revision, str) or not revision:
        return web.json_response(
            {
                "success": False,
                "field": "revision",
                "error": "Reload configuration before saving.",
            },
            status=400,
        )
    if not isinstance(proposed, dict):
        return web.json_response(
            {
                "success": False,
                "field": "config",
                "error": "Configuration must be an object.",
            },
            status=400,
        )

    current = remote_config.as_dict()
    configured = current["configured"]
    overrides = current["overrides"]
    for field in ("remote_url", "timeout"):
        if overrides.get(field) and proposed.get(field) != configured.get(field):
            variable = overrides[field]
            return web.json_response(
                {
                    "success": False,
                    "field": field,
                    "error": f"{field.replace('_', ' ').title()} is managed by {variable}.",
                },
                status=409,
            )

    try:
        remote_config.save(proposed, expected_revision=revision)
        await _activate_runtime_generation()
    except ConfigValidationError as exc:
        return web.json_response(
            {"success": False, "field": exc.field, "error": str(exc)}, status=400
        )
    except ConfigConflictError as exc:
        remote_config.reload()
        await _activate_runtime_generation()
        return web.json_response(
            {"success": False, "error": str(exc), "latest": remote_config.as_dict()},
            status=409,
        )
    except OSError:
        logger.exception("[LM-Remote] Failed to persist configuration")
        return web.json_response(
            {"success": False, "error": "Could not save LM Remote configuration."},
            status=500,
        )

    logger.info(
        "[LM-Remote] Configuration applied without restart (generation %s)",
        remote_config.generation,
    )
    return _config_response()


class _ConnectionTestError(RuntimeError):
    pass


async def _read_small_json(response: aiohttp.ClientResponse) -> object:
    body = bytearray()
    while True:
        remaining = _MAX_TEST_RESPONSE + 1 - len(body)
        chunk = await response.content.read(min(8192, remaining))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > _MAX_TEST_RESPONSE:
            raise _ConnectionTestError("Remote response was unexpectedly large.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ConnectionTestError("Remote response was not valid JSON.") from exc


async def _perform_connection_test(remote_url: str, timeout_seconds: int) -> int:
    """Probe a fixed Manager endpoint without changing the active configuration."""
    started = time.monotonic()
    bounded_timeout = min(timeout_seconds, 30)
    timeout = aiohttp.ClientTimeout(
        total=bounded_timeout,
        connect=min(bounded_timeout, 10),
    )
    headers = {"Accept": "application/json", _PROXY_HOP_HEADER: "probe"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        health_url = f"{remote_url}/api/lm/health-check"
        async with session.get(
            health_url, headers=headers, allow_redirects=False
        ) as response:
            if response.status == 404:
                payload = None
            elif not 200 <= response.status < 300:
                raise _ConnectionTestError(
                    f"Remote LoRA Manager returned HTTP {response.status}."
                )
            else:
                payload = await _read_small_json(response)
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    raise _ConnectionTestError(
                        "The server answered, but it is not a compatible LoRA Manager."
                    )

        # Older Manager releases may not expose health-check.
        if payload is None:
            list_url = f"{remote_url}/api/lm/loras/list?page=1&page_size=1"
            async with session.get(
                list_url, headers=headers, allow_redirects=False
            ) as response:
                if not 200 <= response.status < 300:
                    raise _ConnectionTestError(
                        f"Remote LoRA Manager returned HTTP {response.status}."
                    )
                list_payload = await _read_small_json(response)
                if not isinstance(list_payload, dict) or "items" not in list_payload:
                    raise _ConnectionTestError(
                        "The server answered, but it is not a compatible LoRA Manager."
                    )

    return max(1, round((time.monotonic() - started) * 1000))


async def _handle_test_connection(request: web.Request) -> web.Response:
    if request.method != "POST":
        return web.json_response(
            {"success": False, "error": "Method not allowed."},
            status=405,
            headers={"Allow": "POST"},
        )
    if request.content_type != "application/json":
        return web.json_response(
            {"success": False, "error": "Content-Type must be application/json."},
            status=415,
        )
    try:
        payload = await _read_limited_json(request)
        if not isinstance(payload, dict):
            raise ConfigValidationError("config", "Request body must be an object.")
        unknown = set(payload) - {"remote_url", "timeout"}
        if unknown:
            raise ConfigValidationError(
                "config", f"Unknown request field(s): {', '.join(sorted(unknown))}"
            )
        from .config import _normalize_timeout, _normalize_url

        remote_url = _normalize_url(payload.get("remote_url", ""), allow_empty=False)
        timeout_seconds = _normalize_timeout(payload.get("timeout", 30))
        latency_ms = await _perform_connection_test(remote_url, timeout_seconds)
    except (_RequestBodyTooLarge, web.HTTPRequestEntityTooLarge):
        return web.json_response(
            {"success": False, "error": "Connection test request is too large."},
            status=413,
        )
    except ConfigValidationError as exc:
        return web.json_response(
            {"success": False, "field": exc.field, "error": str(exc)}, status=400
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(
            {"success": False, "error": "Request body is not valid JSON."}, status=400
        )
    except asyncio.TimeoutError:
        return web.json_response(
            {"success": False, "error": "Connection test timed out."}, status=504
        )
    except aiohttp.ClientError:
        return web.json_response(
            {"success": False, "error": "Could not reach the remote LoRA Manager."},
            status=502,
        )
    except _ConnectionTestError as exc:
        return web.json_response({"success": False, "error": str(exc)}, status=502)

    return web.json_response(
        {
            "success": True,
            "message": "Connected to LoRA Manager.",
            "latency_ms": latency_ms,
        }
    )


# Generation-scoped HTTP sessions allow an old request to finish while a newly
# saved configuration starts using its own connection pool immediately.
@dataclass(eq=False)
class _ProxySessionState:
    session: aiohttp.ClientSession
    active_requests: int = 0
    retired: bool = False


_proxy_sessions: dict[int, _ProxySessionState] = {}
_proxy_session_lock: asyncio.Lock | None = None


def _get_proxy_session_lock() -> asyncio.Lock:
    global _proxy_session_lock
    if _proxy_session_lock is None:
        _proxy_session_lock = asyncio.Lock()
    return _proxy_session_lock


@asynccontextmanager
async def _proxy_session_lease(snapshot: ConfigSnapshot):
    """Lease the pool for one generation without closing active requests."""
    async with _get_proxy_session_lock():
        state = _proxy_sessions.get(snapshot.generation)
        if state is None or state.session.closed:
            if state is not None:
                state.retired = True
            state = _ProxySessionState(
                session=aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=snapshot.timeout),
                    cookie_jar=aiohttp.DummyCookieJar(),
                ),
                retired=snapshot.generation != remote_config.generation,
            )
            _proxy_sessions[snapshot.generation] = state
        state.active_requests += 1

    try:
        yield state.session
    finally:
        close_session: aiohttp.ClientSession | None = None
        async with _get_proxy_session_lock():
            state.active_requests -= 1
            if state.retired and state.active_requests == 0:
                if _proxy_sessions.get(snapshot.generation) is state:
                    _proxy_sessions.pop(snapshot.generation, None)
                close_session = state.session
        if close_session is not None and not close_session.closed:
            await close_session.close()


async def _retire_proxy_sessions(active_generation: int) -> None:
    """Retire old pools and close only those with no requests in flight."""
    close_sessions: list[aiohttp.ClientSession] = []
    async with _get_proxy_session_lock():
        for generation, state in list(_proxy_sessions.items()):
            if generation == active_generation:
                continue
            state.retired = True
            if state.active_requests == 0:
                _proxy_sessions.pop(generation, None)
                close_sessions.append(state.session)
    for session in close_sessions:
        if not session.closed:
            await session.close()


async def _close_all_proxy_sessions() -> None:
    """Force-close every pool during application shutdown."""
    async with _get_proxy_session_lock():
        sessions = [state.session for state in _proxy_sessions.values()]
        _proxy_sessions.clear()
    for session in sessions:
        if not session.closed:
            await session.close()


@dataclass(eq=False)
class _ActiveWebSocket:
    generation: int
    local_ws: web.WebSocketResponse
    session: aiohttp.ClientSession
    remote_ws: aiohttp.ClientWebSocketResponse | None = None
    retired: bool = False


_active_proxy_websockets: set[_ActiveWebSocket] = set()
_active_proxy_websockets_lock: asyncio.Lock | None = None


def _get_active_websockets_lock() -> asyncio.Lock:
    global _active_proxy_websockets_lock
    if _active_proxy_websockets_lock is None:
        _active_proxy_websockets_lock = asyncio.Lock()
    return _active_proxy_websockets_lock


async def _register_active_websocket(bridge: _ActiveWebSocket) -> bool:
    async with _get_active_websockets_lock():
        if bridge.generation != remote_config.generation:
            bridge.retired = True
            return False
        _active_proxy_websockets.add(bridge)
        return True


async def _unregister_active_websocket(bridge: _ActiveWebSocket) -> None:
    async with _get_active_websockets_lock():
        _active_proxy_websockets.discard(bridge)


async def _close_websocket_bridge(bridge: _ActiveWebSocket) -> None:
    bridge.retired = True
    try:
        if bridge.remote_ws is not None and not bridge.remote_ws.closed:
            await bridge.remote_ws.close(
                code=1012, message=b"LM Remote configuration changed"
            )
        if not bridge.local_ws.closed:
            await bridge.local_ws.close(
                code=1012, message=b"LM Remote configuration changed"
            )
    finally:
        if not bridge.session.closed:
            await bridge.session.close()


async def _rotate_active_websockets(active_generation: int | None) -> None:
    """Close bridges for retired generations so browsers reconnect."""
    async with _get_active_websockets_lock():
        bridges = [
            bridge
            for bridge in _active_proxy_websockets
            if active_generation is None or bridge.generation != active_generation
        ]
        for bridge in bridges:
            _active_proxy_websockets.discard(bridge)
    for bridge in bridges:
        try:
            await _close_websocket_bridge(bridge)
        except Exception:
            logger.exception("[LM-Remote] Failed to rotate a proxied WebSocket")


def _should_proxy(path: str) -> bool:
    """Return True if *path* should be proxied to the remote instance."""
    if any(path.startswith(p) for p in _PROXY_PREFIXES):
        return True
    if path in _PROXY_PAGE_ROUTES or path.rstrip("/") in _PROXY_PAGE_ROUTES:
        return True
    return False


def _is_ws_route(path: str) -> bool:
    return path in _WS_ROUTES


async def _proxy_ws(
    request: web.Request, snapshot: ConfigSnapshot
) -> web.WebSocketResponse:
    """Proxy a WebSocket connection to the remote LoRA Manager."""
    remote_url = snapshot.remote_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )
    remote_ws_url = f"{remote_url}{request.path}"
    if request.query_string:
        remote_ws_url += f"?{request.query_string}"

    local_ws = web.WebSocketResponse()
    await local_ws.prepare(request)

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=min(snapshot.timeout, 30),
    )
    session = aiohttp.ClientSession(timeout=timeout)
    bridge = _ActiveWebSocket(snapshot.generation, local_ws, session)
    if not await _register_active_websocket(bridge):
        await _close_websocket_bridge(bridge)
        return local_ws

    try:
        async with session.ws_connect(
            remote_ws_url, headers={_PROXY_HOP_HEADER: "1"}
        ) as remote_ws:
            bridge.remote_ws = remote_ws

            async def forward_local_to_remote():
                async for msg in local_ws:
                    if msg.type == WSMsgType.TEXT:
                        await remote_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await remote_ws.send_bytes(msg.data)
                    elif msg.type in (
                        WSMsgType.CLOSE,
                        WSMsgType.CLOSING,
                        WSMsgType.CLOSED,
                    ):
                        return

            async def forward_remote_to_local():
                async for msg in remote_ws:
                    if msg.type == WSMsgType.TEXT:
                        await local_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await local_ws.send_bytes(msg.data)
                    elif msg.type in (
                        WSMsgType.CLOSE,
                        WSMsgType.CLOSING,
                        WSMsgType.CLOSED,
                    ):
                        return

            # Run both directions concurrently.  When either side closes,
            # cancel the other to prevent hanging.
            task_l2r = asyncio.create_task(forward_local_to_remote())
            task_r2l = asyncio.create_task(forward_remote_to_local())
            try:
                done, pending = await asyncio.wait(
                    {task_l2r, task_r2l}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                # Ensure both sides are closed
                if not remote_ws.closed:
                    await remote_ws.close()
                if not local_ws.closed:
                    await local_ws.close()

    except Exception as exc:
        if not bridge.retired:
            logger.warning(
                "[LM-Remote] WebSocket proxy error for %s: %s", request.path, exc
            )
    finally:
        await _unregister_active_websocket(bridge)
        if not local_ws.closed:
            await local_ws.close()
        await session.close()

    return local_ws


async def _proxy_http(request: web.Request, snapshot: ConfigSnapshot) -> web.Response:
    """Forward an HTTP request to the remote LoRA Manager and return its response."""
    remote_url = f"{snapshot.remote_url}{request.path}"
    if request.query_string:
        remote_url += f"?{request.query_string}"

    # Read the request body (if any)
    body = await request.read() if request.can_read_body else None

    # Filter hop-by-hop headers
    headers = {}
    skip = {
        "host",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "authorization",
        "proxy-authorization",
        "cookie",
        "origin",
        "referer",
    }
    for k, v in request.headers.items():
        if k.lower() not in skip:
            headers[k] = v
    headers[_PROXY_HOP_HEADER] = "1"

    try:
        async with _proxy_session_lease(snapshot) as session:
            async with session.request(
                method=request.method,
                url=remote_url,
                headers=headers,
                data=body,
            ) as resp:
                resp_body = await resp.read()
                resp_headers = {}
                for k, v in resp.headers.items():
                    if k.lower() not in (
                        "transfer-encoding",
                        "content-encoding",
                        "content-length",
                        "set-cookie",
                    ):
                        resp_headers[k] = v
                return web.Response(
                    status=resp.status,
                    body=resp_body,
                    headers=resp_headers,
                )
    except Exception as exc:
        logger.error(
            "[LM-Remote] Proxy error for %s %s: %s", request.method, request.path, exc
        )
        return web.json_response(
            {"error": "Remote LoRA Manager unavailable."},
            status=502,
        )


# ---------------------------------------------------------------------------
# Middleware factory
# ---------------------------------------------------------------------------


@web.middleware
async def lm_remote_proxy_middleware(request: web.Request, handler):
    """aiohttp middleware that intercepts LoRA Manager requests."""
    path = request.path

    # Configuration remains local and available even before a remote is set.
    if path == _CONFIG_ROUTE:
        return await _handle_config(request)
    if path == _TEST_CONNECTION_ROUTE:
        return await _handle_test_connection(request)

    snapshot = remote_config.snapshot
    if not snapshot.remote_url:
        return await handler(request)

    if request.headers.get(_PROXY_HOP_HEADER) and (
        _should_proxy(path) or _is_ws_route(path)
    ):
        return web.json_response(
            {"error": "LM Remote proxy loop detected."}, status=508
        )

    # Routes that need send_sync are handled locally so events reach
    # the local browser (the remote instance has no connected browsers).
    local_handler = _SEND_SYNC_HANDLERS.get(path)
    if local_handler is not None:
        return await local_handler(request)

    # WebSocket routes
    if _is_ws_route(path):
        return await _proxy_ws(request, snapshot)

    # Regular proxy routes
    if _should_proxy(path):
        return await _proxy_http(request, snapshot)

    # Not a LoRA Manager route — fall through
    return await handler(request)


async def _cleanup_proxy_session(app) -> None:
    """Shutdown hook to close every HTTP pool and WebSocket bridge."""
    await _rotate_active_websockets(None)
    await _close_all_proxy_sessions()
    await RemoteLoraClient.get_instance().close()


def register_proxy(app) -> None:
    """Append the proxy after ComfyUI's origin and security guards."""
    if lm_remote_proxy_middleware not in app.middlewares:
        app.middlewares.append(lm_remote_proxy_middleware)
    if _cleanup_proxy_session not in app.on_shutdown:
        app.on_shutdown.append(_cleanup_proxy_session)
    if remote_config.is_configured:
        logger.info(
            "[LM-Remote] Proxy routes registered -> %s", remote_config.remote_url
        )
    else:
        logger.info("[LM-Remote] Configuration API ready; remote URL is not set yet")
