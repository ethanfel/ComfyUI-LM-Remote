"""Remote LoRA Pool — pure pass-through, just different NAME."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LoraPoolRemoteLM:
    """LoRA Pool that passes through filter config (remote variant for NAME only)."""

    NAME = "Lora Pool (Remote, LoraManager)"
    CATEGORY = "Lora Manager/randomizer"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pool_config": ("LORA_POOL_CONFIG", {}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("POOL_CONFIG",)
    RETURN_NAMES = ("POOL_CONFIG",)
    FUNCTION = "process"
    OUTPUT_NODE = False

    def process(self, pool_config, unique_id=None):
        if not isinstance(pool_config, dict):
            logger.warning("Invalid pool_config type, using empty config")
            pool_config = self._default_config()

        if "version" not in pool_config:
            pool_config["version"] = 1

        filters = pool_config.get("filters", self._default_config()["filters"])
        logger.debug("[LoraPoolRemoteLM] Processing filters: %s", filters)
        return (filters,)

    @staticmethod
    def _default_config():
        return {
            "version": 1,
            "filters": {
                "baseModels": [],
                "tags": {"include": [], "exclude": []},
                "folders": {"include": [], "exclude": []},
                "favoritesOnly": False,
                "license": {"noCreditRequired": False, "allowSelling": False},
            },
            "preview": {"matchCount": 0, "lastUpdated": 0},
        }
