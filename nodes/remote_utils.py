"""Remote replacement for ``py/utils/utils.py:get_lora_info()``.

Same signature: ``get_lora_info_remote(lora_name) -> (relative_path, trigger_words)``
but fetches data from the remote LoRA Manager HTTP API instead of the local
ServiceRegistry / SQLite cache.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from ..remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)


def get_lora_info_remote(lora_name: str) -> tuple[str, list[str]]:
    """Synchronous wrapper that calls the remote API for LoRA metadata.

    Uses the same sync-from-async bridge pattern as the original
    ``get_lora_info()`` to be a drop-in replacement in node ``FUNCTION`` methods.
    """
    async def _fetch():
        client = RemoteLoraClient.get_instance()
        return await client.get_lora_info(lora_name)

    try:
        asyncio.get_running_loop()
        # Already inside an event loop — run in a separate thread.
        def _run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_fetch())
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(_run_in_thread)
            return future.result()
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(_fetch())
