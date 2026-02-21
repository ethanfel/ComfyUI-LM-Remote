"""
Reverse-proxy middleware that forwards LoRA Manager requests to the remote instance.

Registered as an aiohttp middleware on PromptServer.instance.app.  It intercepts
requests matching known LoRA Manager URL prefixes and proxies them to the remote
Docker instance.  Non-matching requests fall through to the regular ComfyUI router.

Routes that use ``PromptServer.instance.send_sync()`` are explicitly excluded
from proxying so the local original LoRA Manager handler can broadcast events
to the local ComfyUI frontend.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web, WSMsgType

from .config import remote_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL prefixes that should be forwarded to the remote LoRA Manager
# ---------------------------------------------------------------------------
_PROXY_PREFIXES = (
    "/api/lm/",
    "/loras_static/",
    "/locales/",
    "/example_images_static/",
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

# Routes that call send_sync on the remote side — these are NOT proxied.
# Instead they fall through to the local original LoRA Manager handler,
# which broadcasts events to the local ComfyUI frontend.  The remote
# handler would broadcast to its own (empty) frontend, which is useless.
#
# These routes:
#   /api/lm/loras/get_trigger_words  -> trigger_word_update event
#   /api/lm/update-lora-code         -> lora_code_update event
#   /api/lm/update-node-widget       -> lm_widget_update event
#   /api/lm/register-nodes           -> lora_registry_refresh event
_SEND_SYNC_SKIP_ROUTES = {
    "/api/lm/loras/get_trigger_words",
    "/api/lm/update-lora-code",
    "/api/lm/update-node-widget",
    "/api/lm/register-nodes",
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

    # Routes that use send_sync must NOT be proxied — let the local
    # original LoRA Manager handle them so events reach the local browser.
    if path in _SEND_SYNC_SKIP_ROUTES:
        return await handler(request)

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
