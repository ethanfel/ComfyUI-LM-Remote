"""Remote LoRA Randomizer — uses remote API instead of local ServiceRegistry."""
from __future__ import annotations

import logging
import os

from .remote_utils import get_lora_info_remote
from .utils import extract_lora_name
from ..remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)


class LoraRandomizerRemoteLM:
    """Node that randomly selects LoRAs from a pool (remote metadata)."""

    NAME = "Lora Randomizer (Remote, LoraManager)"
    CATEGORY = "Lora Manager/randomizer"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "randomizer_config": ("RANDOMIZER_CONFIG", {}),
                "loras": ("LORAS", {}),
            },
            "optional": {
                "pool_config": ("POOL_CONFIG", {}),
            },
        }

    RETURN_TYPES = ("LORA_STACK",)
    RETURN_NAMES = ("LORA_STACK",)
    FUNCTION = "randomize"
    OUTPUT_NODE = False

    def _preprocess_loras_input(self, loras):
        if isinstance(loras, dict) and "__value__" in loras:
            return loras["__value__"]
        return loras

    async def randomize(self, randomizer_config, loras, pool_config=None):
        loras = self._preprocess_loras_input(loras)

        roll_mode = randomizer_config.get("roll_mode", "always")
        execution_seed = randomizer_config.get("execution_seed", None)
        next_seed = randomizer_config.get("next_seed", None)

        if roll_mode == "fixed":
            ui_loras = loras
            execution_loras = loras
        else:
            client = RemoteLoraClient.get_instance()

            # Build common kwargs for remote API
            api_kwargs = self._build_api_kwargs(randomizer_config, loras, pool_config)

            if execution_seed is not None:
                exec_kwargs = {**api_kwargs, "seed": execution_seed}
                execution_loras = await client.get_random_loras(**exec_kwargs)
                if not execution_loras:
                    execution_loras = loras
            else:
                execution_loras = loras

            ui_kwargs = {**api_kwargs, "seed": next_seed}
            ui_loras = await client.get_random_loras(**ui_kwargs)
            if not ui_loras:
                ui_loras = loras

        execution_stack = self._build_execution_stack_from_input(execution_loras)

        return {
            "result": (execution_stack,),
            "ui": {"loras": ui_loras, "last_used": execution_loras},
        }

    def _build_api_kwargs(self, randomizer_config, input_loras, pool_config):
        locked_loras = [l for l in input_loras if l.get("locked", False)]
        return {
            "count": int(randomizer_config.get("count_fixed", 5)),
            "count_mode": randomizer_config.get("count_mode", "range"),
            "count_min": int(randomizer_config.get("count_min", 3)),
            "count_max": int(randomizer_config.get("count_max", 7)),
            "model_strength_min": float(randomizer_config.get("model_strength_min", 0.0)),
            "model_strength_max": float(randomizer_config.get("model_strength_max", 1.0)),
            "use_same_clip_strength": randomizer_config.get("use_same_clip_strength", True),
            "clip_strength_min": float(randomizer_config.get("clip_strength_min", 0.0)),
            "clip_strength_max": float(randomizer_config.get("clip_strength_max", 1.0)),
            "use_recommended_strength": randomizer_config.get("use_recommended_strength", False),
            "recommended_strength_scale_min": float(randomizer_config.get("recommended_strength_scale_min", 0.5)),
            "recommended_strength_scale_max": float(randomizer_config.get("recommended_strength_scale_max", 1.0)),
            "locked_loras": locked_loras,
            "pool_config": pool_config,
        }

    def _build_execution_stack_from_input(self, loras):
        lora_stack = []
        for lora in loras:
            if not lora.get("active", False):
                continue

            lora_path, _ = get_lora_info_remote(lora["name"])
            if not lora_path:
                logger.warning("[LoraRandomizerRemoteLM] Could not find path for LoRA: %s", lora["name"])
                continue

            lora_path = lora_path.replace("/", os.sep)
            model_strength = float(lora.get("strength", 1.0))
            clip_strength = float(lora.get("clipStrength", model_strength))
            lora_stack.append((lora_path, model_strength, clip_strength))

        return lora_stack
