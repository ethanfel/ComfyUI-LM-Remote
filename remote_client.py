"""HTTP client for the remote LoRA Manager instance."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .config import remote_config

logger = logging.getLogger(__name__)

# Cache TTL in seconds — how long before we re-fetch the full LoRA list
_CACHE_TTL = 60


class RemoteLoraClient:
    """Singleton HTTP client that talks to the remote LoRA Manager.

    Uses the actual LoRA Manager REST API endpoints:
    - ``GET /api/lm/loras/list?page_size=9999``  — paginated LoRA list
    - ``GET /api/lm/loras/get-trigger-words?name=X`` — trigger words
    - ``POST /api/lm/loras/random-sample``  — random LoRA selection
    - ``POST /api/lm/loras/cycler-list``  — sorted LoRA list for cycler

    A short-lived in-memory cache avoids redundant calls to the list endpoint
    during a single workflow execution (which may resolve many LoRAs at once).
    """

    _instance: RemoteLoraClient | None = None
    _session: aiohttp.ClientSession | None = None

    def __init__(self):
        self._lora_cache: list[dict] = []
        self._lora_cache_ts: float = 0
        self._checkpoint_cache: list[dict] = []
        self._checkpoint_cache_ts: float = 0

    @classmethod
    def get_instance(cls) -> RemoteLoraClient:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=remote_config.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        url = f"{remote_config.remote_url}{path}"
        session = await self._get_session()
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post_json(self, path: str, json_body: dict | None = None) -> Any:
        url = f"{remote_config.remote_url}{path}"
        session = await self._get_session()
        async with session.post(url, json=json_body) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------
    # Cached list helpers
    # ------------------------------------------------------------------

    async def _get_lora_list_cached(self) -> list[dict]:
        """Return the full LoRA list, using a short-lived cache."""
        now = time.monotonic()
        if self._lora_cache and (now - self._lora_cache_ts) < _CACHE_TTL:
            return self._lora_cache

        try:
            data = await self._get_json(
                "/api/lm/loras/list", params={"page_size": "9999"}
            )
            self._lora_cache = data.get("items", [])
            self._lora_cache_ts = now
        except Exception as exc:
            logger.warning("[LM-Remote] Failed to fetch LoRA list: %s", exc)
            # Return stale cache on error, or empty list
        return self._lora_cache

    async def _get_checkpoint_list_cached(self) -> list[dict]:
        """Return the full checkpoint list, using a short-lived cache."""
        now = time.monotonic()
        if self._checkpoint_cache and (now - self._checkpoint_cache_ts) < _CACHE_TTL:
            return self._checkpoint_cache

        try:
            data = await self._get_json(
                "/api/lm/checkpoints/list", params={"page_size": "9999"}
            )
            self._checkpoint_cache = data.get("items", [])
            self._checkpoint_cache_ts = now
        except Exception as exc:
            logger.warning("[LM-Remote] Failed to fetch checkpoint list: %s", exc)
        return self._checkpoint_cache

    def _find_item_by_name(self, items: list[dict], name: str) -> dict | None:
        """Find an item in a list by file_name."""
        for item in items:
            if item.get("file_name") == name:
                return item
        return None

    # ------------------------------------------------------------------
    # LoRA metadata
    # ------------------------------------------------------------------

    async def get_lora_info(self, lora_name: str) -> tuple[str, list[str]]:
        """Return (relative_path, trigger_words) for a LoRA by display name.

        Uses the cached ``/api/lm/loras/list`` data.  Falls back to the
        per-LoRA ``get-trigger-words`` endpoint if the list lookup fails.
        """
        import posixpath

        try:
            items = await self._get_lora_list_cached()
            item = self._find_item_by_name(items, lora_name)

            if item:
                file_path = item.get("file_path", "")
                file_path = remote_config.map_path(file_path)

                # file_path is the absolute path (forward-slashed) from
                # the remote.  We need a relative path that the local
                # folder_paths.get_full_path("loras", ...) can resolve.
                #
                # The ``folder`` field gives the subfolder within the
                # model root (e.g. "anime" or "anime/characters").
                # The basename of file_path has the extension.
                #
                # Example: file_path="/mnt/loras/anime/test.safetensors"
                #          folder="anime"
                #          -> basename="test.safetensors"
                #          -> relative="anime/test.safetensors"
                folder = item.get("folder", "")
                basename = posixpath.basename(file_path)  # "test.safetensors"

                if folder:
                    relative = f"{folder}/{basename}"
                else:
                    relative = basename

                civitai = item.get("civitai") or {}
                trigger_words = civitai.get("trainedWords", []) if civitai else []
                return relative, trigger_words

            # Fallback: try the specific trigger-words endpoint
            tw_data = await self._get_json(
                "/api/lm/loras/get-trigger-words",
                params={"name": lora_name},
            )
            trigger_words = tw_data.get("trigger_words", [])
            return lora_name, trigger_words

        except Exception as exc:
            logger.warning("[LM-Remote] get_lora_info(%s) failed: %s", lora_name, exc)
        return lora_name, []

    async def get_lora_hash(self, lora_name: str) -> str | None:
        """Return the SHA-256 hash for a LoRA by display name."""
        try:
            items = await self._get_lora_list_cached()
            item = self._find_item_by_name(items, lora_name)
            if item:
                return item.get("sha256") or item.get("hash")
        except Exception as exc:
            logger.warning("[LM-Remote] get_lora_hash(%s) failed: %s", lora_name, exc)
        return None

    async def get_checkpoint_hash(self, checkpoint_name: str) -> str | None:
        """Return the SHA-256 hash for a checkpoint by display name."""
        try:
            items = await self._get_checkpoint_list_cached()
            item = self._find_item_by_name(items, checkpoint_name)
            if item:
                return item.get("sha256") or item.get("hash")
        except Exception as exc:
            logger.warning("[LM-Remote] get_checkpoint_hash(%s) failed: %s", checkpoint_name, exc)
        return None

    async def get_random_loras(self, **kwargs) -> list[dict]:
        """Ask the remote to generate random LoRAs (for Randomizer node)."""
        try:
            result = await self._post_json("/api/lm/loras/random-sample", json_body=kwargs)
            return result if isinstance(result, list) else result.get("loras", [])
        except Exception as exc:
            logger.warning("[LM-Remote] get_random_loras failed: %s", exc)
            return []

    async def get_cycler_list(self, **kwargs) -> list[dict]:
        """Ask the remote for a sorted LoRA list (for Cycler node)."""
        try:
            result = await self._post_json("/api/lm/loras/cycler-list", json_body=kwargs)
            return result if isinstance(result, list) else result.get("loras", [])
        except Exception as exc:
            logger.warning("[LM-Remote] get_cycler_list failed: %s", exc)
            return []
