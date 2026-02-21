"""Remote WanVideo LoRA nodes — fetch metadata from the remote LoRA Manager."""
from __future__ import annotations

import logging

import folder_paths  # type: ignore

from .remote_utils import get_lora_info_remote
from .utils import FlexibleOptionalInputType, any_type, get_loras_list

logger = logging.getLogger(__name__)


class WanVideoLoraSelectRemoteLM:
    NAME = "WanVideo Lora Select (Remote, LoraManager)"
    CATEGORY = "Lora Manager/stackers"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "low_mem_load": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Load LORA models with less VRAM usage, slower loading.",
                }),
                "merge_loras": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Merge LoRAs into the model.",
                }),
                "text": ("AUTOCOMPLETE_TEXT_LORAS", {
                    "placeholder": "Search LoRAs to add...",
                    "tooltip": "Format: <lora:lora_name:strength>",
                }),
            },
            "optional": FlexibleOptionalInputType(any_type),
        }

    RETURN_TYPES = ("WANVIDLORA", "STRING", "STRING")
    RETURN_NAMES = ("lora", "trigger_words", "active_loras")
    FUNCTION = "process_loras"

    def process_loras(self, text, low_mem_load=False, merge_loras=True, **kwargs):
        loras_list = []
        all_trigger_words = []
        active_loras = []

        prev_lora = kwargs.get("prev_lora", None)
        if prev_lora is not None:
            loras_list.extend(prev_lora)

        if not merge_loras:
            low_mem_load = False

        blocks = kwargs.get("blocks", {})
        selected_blocks = blocks.get("selected_blocks", {})
        layer_filter = blocks.get("layer_filter", "")

        loras_from_widget = get_loras_list(kwargs)
        for lora in loras_from_widget:
            if not lora.get("active", False):
                continue

            lora_name = lora["name"]
            model_strength = float(lora["strength"])
            clip_strength = float(lora.get("clipStrength", model_strength))

            lora_path, trigger_words = get_lora_info_remote(lora_name)

            lora_item = {
                "path": folder_paths.get_full_path("loras", lora_path),
                "strength": model_strength,
                "name": lora_path.split(".")[0],
                "blocks": selected_blocks,
                "layer_filter": layer_filter,
                "low_mem_load": low_mem_load,
                "merge_loras": merge_loras,
            }

            loras_list.append(lora_item)
            active_loras.append((lora_name, model_strength, clip_strength))
            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        formatted_loras = []
        for name, ms, cs in active_loras:
            if abs(ms - cs) > 0.001:
                formatted_loras.append(f"<lora:{name}:{str(ms).strip()}:{str(cs).strip()}>")
            else:
                formatted_loras.append(f"<lora:{name}:{str(ms).strip()}>")

        active_loras_text = " ".join(formatted_loras)
        return (loras_list, trigger_words_text, active_loras_text)


class WanVideoLoraTextSelectRemoteLM:
    NAME = "WanVideo Lora Select From Text (Remote, LoraManager)"
    CATEGORY = "Lora Manager/stackers"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "low_mem_load": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Load LORA models with less VRAM usage, slower loading.",
                }),
                "merge_lora": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Merge LoRAs into the model.",
                }),
                "lora_syntax": ("STRING", {
                    "multiline": True,
                    "forceInput": True,
                    "tooltip": "Connect a TEXT output for LoRA syntax: <lora:name:strength>",
                }),
            },
            "optional": {
                "prev_lora": ("WANVIDLORA",),
                "blocks": ("BLOCKS",),
            },
        }

    RETURN_TYPES = ("WANVIDLORA", "STRING", "STRING")
    RETURN_NAMES = ("lora", "trigger_words", "active_loras")
    FUNCTION = "process_loras_from_syntax"

    def process_loras_from_syntax(self, lora_syntax, low_mem_load=False, merge_lora=True, **kwargs):
        blocks = kwargs.get("blocks", {})
        selected_blocks = blocks.get("selected_blocks", {})
        layer_filter = blocks.get("layer_filter", "")

        loras_list = []
        all_trigger_words = []
        active_loras = []

        prev_lora = kwargs.get("prev_lora", None)
        if prev_lora is not None:
            loras_list.extend(prev_lora)

        if not merge_lora:
            low_mem_load = False

        parts = lora_syntax.split("<lora:")
        for part in parts[1:]:
            end_index = part.find(">")
            if end_index == -1:
                continue

            content = part[:end_index]
            lora_parts = content.split(":")

            lora_name_raw = ""
            model_strength = 1.0
            clip_strength = 1.0

            if len(lora_parts) == 2:
                lora_name_raw = lora_parts[0].strip()
                try:
                    model_strength = float(lora_parts[1])
                    clip_strength = model_strength
                except (ValueError, IndexError):
                    logger.warning("Invalid strength for LoRA '%s'. Skipping.", lora_name_raw)
                    continue
            elif len(lora_parts) >= 3:
                lora_name_raw = lora_parts[0].strip()
                try:
                    model_strength = float(lora_parts[1])
                    clip_strength = float(lora_parts[2])
                except (ValueError, IndexError):
                    logger.warning("Invalid strengths for LoRA '%s'. Skipping.", lora_name_raw)
                    continue
            else:
                continue

            lora_path, trigger_words = get_lora_info_remote(lora_name_raw)

            lora_item = {
                "path": folder_paths.get_full_path("loras", lora_path),
                "strength": model_strength,
                "name": lora_path.split(".")[0],
                "blocks": selected_blocks,
                "layer_filter": layer_filter,
                "low_mem_load": low_mem_load,
                "merge_loras": merge_lora,
            }

            loras_list.append(lora_item)
            active_loras.append((lora_name_raw, model_strength, clip_strength))
            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        formatted_loras = []
        for name, ms, cs in active_loras:
            if abs(ms - cs) > 0.001:
                formatted_loras.append(f"<lora:{name}:{str(ms).strip()}:{str(cs).strip()}>")
            else:
                formatted_loras.append(f"<lora:{name}:{str(ms).strip()}>")

        active_loras_text = " ".join(formatted_loras)
        return (loras_list, trigger_words_text, active_loras_text)
