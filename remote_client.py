"""HTTP client for the remote LoRA Manager instance."""

from __future__ import annotations

import logging
import os
import posixpath
import threading
import time
from typing import Any

import aiohttp

from .config import ConfigSnapshot, remote_config

logger = logging.getLogger(__name__)

# Cache TTL in seconds — how long before we re-fetch the full LoRA list
_CACHE_TTL = 60
_LIST_PAGE_SIZE = 100
_MAX_LIST_PAGES = 1000


class _ConfigurationChanged(RuntimeError):
    """Raised when a multi-page read outlives its configuration snapshot."""


class RemoteLoraClient:
    """Singleton HTTP client that talks to the remote LoRA Manager.

    Uses the actual LoRA Manager REST API endpoints:
    - ``GET /api/lm/loras/list?page=N&page_size=100`` — paginated LoRA list
    - ``GET /api/lm/loras/get-trigger-words?name=X`` — trigger words
    - ``POST /api/lm/loras/random-sample``  — random LoRA selection
    - ``POST /api/lm/loras/cycler-list``  — sorted LoRA list for cycler

    A short-lived in-memory cache avoids redundant calls to the list endpoint
    during a single workflow execution (which may resolve many LoRAs at once).
    """

    _instance: RemoteLoraClient | None = None

    def __init__(self):
        self._lora_cache: list[dict] = []
        self._lora_cache_ts: float = 0
        self._lora_cache_generation: int = -1
        self._checkpoint_cache: list[dict] = []
        self._checkpoint_cache_ts: float = 0
        self._checkpoint_cache_generation: int = -1
        self._cache_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> RemoteLoraClient:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def close(self):
        """Compatibility hook; requests use loop-safe, short-lived sessions."""

    def invalidate_caches(self) -> None:
        """Forget results associated with a previous remote configuration."""
        with self._cache_lock:
            self._lora_cache = []
            self._lora_cache_ts = 0
            self._lora_cache_generation = -1
            self._checkpoint_cache = []
            self._checkpoint_cache_ts = 0
            self._checkpoint_cache_generation = -1

    # ------------------------------------------------------------------
    # Core HTTP helpers
    # ------------------------------------------------------------------

    async def _get_json(
        self,
        path: str,
        params: dict | None = None,
        *,
        snapshot: ConfigSnapshot | None = None,
    ) -> Any:
        snapshot = snapshot or remote_config.snapshot
        url = f"{snapshot.remote_url}{path}"
        timeout = aiohttp.ClientTimeout(total=snapshot.timeout)
        # Node execution can invoke this singleton from several short-lived event
        # loops. A request-scoped session avoids retaining a loop-bound session.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post_json(
        self,
        path: str,
        json_body: dict | None = None,
        *,
        snapshot: ConfigSnapshot | None = None,
    ) -> Any:
        snapshot = snapshot or remote_config.snapshot
        url = f"{snapshot.remote_url}{path}"
        timeout = aiohttp.ClientTimeout(total=snapshot.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=json_body) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _get_all_pages(
        self, path: str, *, snapshot: ConfigSnapshot
    ) -> list[dict]:
        """Fetch a complete bounded listing from an API capped at 100 rows."""
        items: list[dict] = []
        page = 1

        while page <= _MAX_LIST_PAGES:
            if remote_config.generation != snapshot.generation:
                raise _ConfigurationChanged
            data = await self._get_json(
                path,
                params={"page": str(page), "page_size": str(_LIST_PAGE_SIZE)},
                snapshot=snapshot,
            )
            if not isinstance(data, dict):
                raise ValueError("Remote listing response must be an object.")
            page_items = data.get("items", [])
            if not isinstance(page_items, list):
                raise ValueError("Remote listing items must be an array.")
            items.extend(item for item in page_items if isinstance(item, dict))

            raw_total_pages = data.get("total_pages")
            total_pages: int | None = None
            if raw_total_pages is not None:
                try:
                    total_pages = int(raw_total_pages)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Remote listing has an invalid total_pages value."
                    ) from exc
                if total_pages < 0:
                    raise ValueError("Remote listing has an invalid total_pages value.")
                if total_pages > _MAX_LIST_PAGES:
                    raise ValueError(
                        f"Remote listing exceeds the {_MAX_LIST_PAGES}-page safety limit."
                    )

            if not page_items:
                break
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(page_items) < _LIST_PAGE_SIZE:
                break
            page += 1
        else:
            raise ValueError(
                f"Remote listing exceeds the {_MAX_LIST_PAGES}-page safety limit."
            )

        if remote_config.generation != snapshot.generation:
            raise _ConfigurationChanged
        return items

    # ------------------------------------------------------------------
    # Cached list helpers
    # ------------------------------------------------------------------

    async def _get_lora_list_cached(
        self, *, snapshot: ConfigSnapshot | None = None
    ) -> list[dict]:
        """Return the full LoRA list, using a short-lived cache."""
        now = time.monotonic()
        snapshot = snapshot or remote_config.snapshot
        with self._cache_lock:
            if (
                self._lora_cache_generation == snapshot.generation
                and (now - self._lora_cache_ts) < _CACHE_TTL
            ):
                return list(self._lora_cache)

        try:
            items = await self._get_all_pages("/api/lm/loras/list", snapshot=snapshot)
            if remote_config.generation == snapshot.generation:
                with self._cache_lock:
                    self._lora_cache = list(items)
                    self._lora_cache_ts = now
                    self._lora_cache_generation = snapshot.generation
                return list(items)
        except _ConfigurationChanged:
            pass
        except Exception as exc:
            logger.warning("[LM-Remote] Failed to fetch LoRA list: %s", exc)
            # Return stale cache on error, or empty list
        with self._cache_lock:
            if self._lora_cache_generation == snapshot.generation:
                return list(self._lora_cache)
        return []

    async def _get_checkpoint_list_cached(
        self, *, snapshot: ConfigSnapshot | None = None
    ) -> list[dict]:
        """Return the full checkpoint list, using a short-lived cache."""
        now = time.monotonic()
        snapshot = snapshot or remote_config.snapshot
        with self._cache_lock:
            if (
                self._checkpoint_cache_generation == snapshot.generation
                and (now - self._checkpoint_cache_ts) < _CACHE_TTL
            ):
                return list(self._checkpoint_cache)

        try:
            items = await self._get_all_pages(
                "/api/lm/checkpoints/list", snapshot=snapshot
            )
            if remote_config.generation == snapshot.generation:
                with self._cache_lock:
                    self._checkpoint_cache = list(items)
                    self._checkpoint_cache_ts = now
                    self._checkpoint_cache_generation = snapshot.generation
                return list(items)
        except _ConfigurationChanged:
            pass
        except Exception as exc:
            logger.warning("[LM-Remote] Failed to fetch checkpoint list: %s", exc)
        with self._cache_lock:
            if self._checkpoint_cache_generation == snapshot.generation:
                return list(self._checkpoint_cache)
        return []

    def _find_item_by_name(self, items: list[dict], name: str) -> dict | None:
        """Find an item in a list by file_name."""
        for item in items:
            if item.get("file_name") == name:
                return item
        return None

    @staticmethod
    def _relative_lora_path(mapped_file_path: str, folder: str) -> str:
        """Convert a mapped absolute path to a local ComfyUI LoRA name."""
        try:
            import folder_paths  # type: ignore

            candidate = os.path.normpath(mapped_file_path)
            for root in folder_paths.get_folder_paths("loras"):
                try:
                    relative = os.path.relpath(candidate, os.path.normpath(str(root)))
                except ValueError:
                    # Windows paths on different drives cannot be relativized.
                    continue
                if relative == os.pardir or relative.startswith(f"{os.pardir}{os.sep}"):
                    continue
                return relative.replace(os.sep, "/")
        except Exception:
            # ComfyUI's folder registry is not present in lightweight tooling.
            pass

        normalized_path = mapped_file_path.replace("\\", "/")
        basename = posixpath.basename(normalized_path)
        normalized_folder = str(folder or "").replace("\\", "/").strip("/")
        return f"{normalized_folder}/{basename}" if normalized_folder else basename

    # ------------------------------------------------------------------
    # LoRA metadata
    # ------------------------------------------------------------------

    async def get_lora_info(self, lora_name: str) -> tuple[str, list[str]]:
        """Return (relative_path, trigger_words) for a LoRA by display name.

        Uses the cached ``/api/lm/loras/list`` data.  Falls back to the
        per-LoRA ``get-trigger-words`` endpoint if the list lookup fails.
        """
        try:
            snapshot = remote_config.snapshot
            items = await self._get_lora_list_cached(snapshot=snapshot)
            item = self._find_item_by_name(items, lora_name)

            if item:
                file_path = item.get("file_path", "")
                file_path = snapshot.map_path(file_path)

                folder = item.get("folder", "")
                relative = self._relative_lora_path(file_path, folder)

                civitai = item.get("civitai") or {}
                trigger_words = civitai.get("trainedWords", []) if civitai else []
                return relative, trigger_words

            # Fallback: try the specific trigger-words endpoint
            tw_data = await self._get_json(
                "/api/lm/loras/get-trigger-words",
                params={"name": lora_name},
                snapshot=snapshot,
            )
            trigger_words = tw_data.get("trigger_words", [])
            return lora_name, trigger_words

        except Exception as exc:
            logger.warning("[LM-Remote] get_lora_info(%s) failed: %s", lora_name, exc)
        return lora_name, []

    async def get_lora_hash(self, lora_name: str) -> str | None:
        """Return the SHA-256 hash for a LoRA by display name."""
        try:
            snapshot = remote_config.snapshot
            items = await self._get_lora_list_cached(snapshot=snapshot)
            item = self._find_item_by_name(items, lora_name)
            if item:
                return item.get("sha256") or item.get("hash")
        except Exception as exc:
            logger.warning("[LM-Remote] get_lora_hash(%s) failed: %s", lora_name, exc)
        return None

    async def get_checkpoint_hash(self, checkpoint_name: str) -> str | None:
        """Return the SHA-256 hash for a checkpoint by display name."""
        try:
            snapshot = remote_config.snapshot
            items = await self._get_checkpoint_list_cached(snapshot=snapshot)
            item = self._find_item_by_name(items, checkpoint_name)
            if item:
                return item.get("sha256") or item.get("hash")
        except Exception as exc:
            logger.warning(
                "[LM-Remote] get_checkpoint_hash(%s) failed: %s", checkpoint_name, exc
            )
        return None

    async def get_random_loras(self, **kwargs) -> list[dict]:
        """Ask the remote to generate random LoRAs (for Randomizer node)."""
        try:
            result = await self._post_json(
                "/api/lm/loras/random-sample", json_body=kwargs
            )
            return result if isinstance(result, list) else result.get("loras", [])
        except Exception as exc:
            logger.warning("[LM-Remote] get_random_loras failed: %s", exc)
            return []

    async def get_cycler_list(self, **kwargs) -> list[dict]:
        """Ask the remote for a sorted LoRA list (for Cycler node)."""
        try:
            result = await self._post_json(
                "/api/lm/loras/cycler-list", json_body=kwargs
            )
            return result if isinstance(result, list) else result.get("loras", [])
        except Exception as exc:
            logger.warning("[LM-Remote] get_cycler_list failed: %s", exc)
            return []
