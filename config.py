"""Configuration for ComfyUI-LM-Remote."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _PACKAGE_DIR / "config.json"


class RemoteConfig:
    """Holds remote LoRA Manager connection settings."""

    def __init__(self):
        self.remote_url: str = ""
        self.timeout: int = 30
        self.path_mappings: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self):
        # Environment variable takes priority
        env_url = os.environ.get("LM_REMOTE_URL", "")
        env_timeout = os.environ.get("LM_REMOTE_TIMEOUT", "")

        # Load config.json defaults
        if _CONFIG_FILE.exists():
            try:
                with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.remote_url = data.get("remote_url", "")
                self.timeout = int(data.get("timeout", 30))
                self.path_mappings = data.get("path_mappings", {})
            except Exception as exc:
                logger.warning("[LM-Remote] Failed to read config.json: %s", exc)

        # Env overrides
        if env_url:
            self.remote_url = env_url
        if env_timeout:
            self.timeout = int(env_timeout)

        # Strip trailing slash
        self.remote_url = self.remote_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.remote_url)

    def map_path(self, remote_path: str) -> str:
        """Apply remote->local path prefix mappings."""
        for remote_prefix, local_prefix in self.path_mappings.items():
            if remote_path.startswith(remote_prefix):
                return local_prefix + remote_path[len(remote_prefix):]
        return remote_path


remote_config = RemoteConfig()
