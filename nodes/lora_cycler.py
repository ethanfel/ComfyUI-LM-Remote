"""Remote LoRA Cycler — uses remote API instead of local ServiceRegistry."""
from __future__ import annotations

import logging
import os

from .remote_utils import get_lora_info_remote
from ..remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)


class LoraCyclerRemoteLM:
    """Node that sequentially cycles through LoRAs from a pool (remote)."""

    NAME = "Lora Cycler (Remote, LoraManager)"
    CATEGORY = "Lora Manager/randomizer"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "cycler_config": ("CYCLER_CONFIG", {}),
            },
            "optional": {
                "pool_config": ("POOL_CONFIG", {}),
            },
        }

    RETURN_TYPES = ("LORA_STACK",)
    RETURN_NAMES = ("LORA_STACK",)
    FUNCTION = "cycle"
    OUTPUT_NODE = False

    async def cycle(self, cycler_config, pool_config=None):
        current_index = cycler_config.get("current_index", 1)
        model_strength = float(cycler_config.get("model_strength", 1.0))
        clip_strength = float(cycler_config.get("clip_strength", 1.0))
        execution_index = cycler_config.get("execution_index")

        client = RemoteLoraClient.get_instance()
        lora_list = await client.get_cycler_list(
            pool_config=pool_config, sort_by="filename"
        )

        total_count = len(lora_list)

        if total_count == 0:
            logger.warning("[LoraCyclerRemoteLM] No LoRAs available in pool")
            return {
                "result": ([],),
                "ui": {
                    "current_index": [1],
                    "next_index": [1],
                    "total_count": [0],
                    "current_lora_name": [""],
                    "current_lora_filename": [""],
                    "error": ["No LoRAs available in pool"],
                },
            }

        actual_index = execution_index if execution_index is not None else current_index
        clamped_index = max(1, min(actual_index, total_count))

        current_lora = lora_list[clamped_index - 1]

        lora_path, _ = get_lora_info_remote(current_lora["file_name"])
        if not lora_path:
            logger.warning("[LoraCyclerRemoteLM] Could not find path for LoRA: %s", current_lora["file_name"])
            lora_stack = []
        else:
            lora_path = lora_path.replace("/", os.sep)
            lora_stack = [(lora_path, model_strength, clip_strength)]

        next_index = clamped_index + 1
        if next_index > total_count:
            next_index = 1

        next_lora = lora_list[next_index - 1]

        return {
            "result": (lora_stack,),
            "ui": {
                "current_index": [clamped_index],
                "next_index": [next_index],
                "total_count": [total_count],
                "current_lora_name": [current_lora["file_name"]],
                "current_lora_filename": [current_lora["file_name"]],
                "next_lora_name": [next_lora["file_name"]],
                "next_lora_filename": [next_lora["file_name"]],
            },
        }
