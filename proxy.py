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
import logging

import aiohttp
from aiohttp import web, WSMsgType

from .config import remote_config
from .remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)

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
        return web.json_response(
            {"success": False, "error": str(exc)}, status=500
        )


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

# Shared HTTP session for proxied requests (connection pooling)
_proxy_session: aiohttp.ClientSession | None = None


async def _get_proxy_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp session for HTTP proxy requests."""
    global _proxy_session
    if _proxy_session is None or _proxy_session.closed:
        timeout = aiohttp.ClientTimeout(total=remote_config.timeout)
        _proxy_session = aiohttp.ClientSession(timeout=timeout)
    return _proxy_session


def _should_proxy(path: str) -> bool:
    """Return True if *path* should be proxied to the remote instance."""
    if any(path.startswith(p) for p in _PROXY_PREFIXES):
        return True
    if path in _PROXY_PAGE_ROUTES or path.rstrip("/") in _PROXY_PAGE_ROUTES:
        return True
    return False


def _is_ws_route(path: str) -> bool:
    return path in _WS_ROUTES


async def _proxy_ws(request: web.Request) -> web.WebSocketResponse:
    """Proxy a WebSocket connection to the remote LoRA Manager."""
    remote_url = remote_config.remote_url.replace("http://", "ws://").replace("https://", "wss://")
    remote_ws_url = f"{remote_url}{request.path}"
    if request.query_string:
        remote_ws_url += f"?{request.query_string}"

    local_ws = web.WebSocketResponse()
    await local_ws.prepare(request)

    timeout = aiohttp.ClientTimeout(total=None)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        async with session.ws_connect(remote_ws_url) as remote_ws:

            async def forward_local_to_remote():
                async for msg in local_ws:
                    if msg.type == WSMsgType.TEXT:
                        await remote_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await remote_ws.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                        return

            async def forward_remote_to_local():
                async for msg in remote_ws:
                    if msg.type == WSMsgType.TEXT:
                        await local_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await local_ws.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
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
        logger.warning("[LM-Remote] WebSocket proxy error for %s: %s", request.path, exc)
    finally:
        await session.close()

    return local_ws


async def _proxy_http(request: web.Request) -> web.Response:
    """Forward an HTTP request to the remote LoRA Manager and return its response."""
    remote_url = f"{remote_config.remote_url}{request.path}"
    if request.query_string:
        remote_url += f"?{request.query_string}"

    # Read the request body (if any)
    body = await request.read() if request.can_read_body else None

    # Filter hop-by-hop headers
    headers = {}
    skip = {"host", "transfer-encoding", "connection", "keep-alive", "upgrade"}
    for k, v in request.headers.items():
        if k.lower() not in skip:
            headers[k] = v

    session = await _get_proxy_session()
    try:
        async with session.request(
            method=request.method,
            url=remote_url,
            headers=headers,
            data=body,
        ) as resp:
            resp_body = await resp.read()
            resp_headers = {}
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    resp_headers[k] = v
            return web.Response(
                status=resp.status,
                body=resp_body,
                headers=resp_headers,
            )
    except Exception as exc:
        logger.error("[LM-Remote] Proxy error for %s %s: %s", request.method, request.path, exc)
        return web.json_response(
            {"error": f"Remote LoRA Manager unavailable: {exc}"},
            status=502,
        )


# ---------------------------------------------------------------------------
# Middleware factory
# ---------------------------------------------------------------------------

@web.middleware
async def lm_remote_proxy_middleware(request: web.Request, handler):
    """aiohttp middleware that intercepts LoRA Manager requests."""
    if not remote_config.is_configured:
        return await handler(request)

    path = request.path

    # Routes that need send_sync are handled locally so events reach
    # the local browser (the remote instance has no connected browsers).
    local_handler = _SEND_SYNC_HANDLERS.get(path)
    if local_handler is not None:
        return await local_handler(request)

    # WebSocket routes
    if _is_ws_route(path):
        return await _proxy_ws(request)

    # Regular proxy routes
    if _should_proxy(path):
        return await _proxy_http(request)

    # Not a LoRA Manager route — fall through
    return await handler(request)


async def _cleanup_proxy_session(app) -> None:
    """Shutdown hook to close the shared proxy session."""
    global _proxy_session
    if _proxy_session and not _proxy_session.closed:
        await _proxy_session.close()
        _proxy_session = None


def register_proxy(app) -> None:
    """Insert the proxy middleware into the aiohttp app."""
    if not remote_config.is_configured:
        logger.warning("[LM-Remote] No remote_url configured — proxy disabled")
        return

    # Insert at position 0 so we run before the original LoRA Manager routes
    app.middlewares.insert(0, lm_remote_proxy_middleware)
    app.on_shutdown.append(_cleanup_proxy_session)
    logger.info("[LM-Remote] Proxy routes registered -> %s", remote_config.remote_url)
