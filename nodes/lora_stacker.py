"""Remote LoRA Stacker — fetch metadata from the remote LoRA Manager."""
from __future__ import annotations

import logging
import os

from .remote_utils import get_lora_info_remote
from .utils import FlexibleOptionalInputType, any_type, extract_lora_name, get_loras_list

logger = logging.getLogger(__name__)


class LoraStackerRemoteLM:
    NAME = "Lora Stacker (Remote, LoraManager)"
    CATEGORY = "Lora Manager/stackers"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("AUTOCOMPLETE_TEXT_LORAS", {
                    "placeholder": "Search LoRAs to add...",
                    "tooltip": "Format: <lora:lora_name:strength> separated by spaces or punctuation",
                }),
            },
            "optional": FlexibleOptionalInputType(any_type),
        }

    RETURN_TYPES = ("LORA_STACK", "STRING", "STRING")
    RETURN_NAMES = ("LORA_STACK", "trigger_words", "active_loras")
    FUNCTION = "stack_loras"

    def stack_loras(self, text, **kwargs):
        stack = []
        active_loras = []
        all_trigger_words = []

        lora_stack = kwargs.get("lora_stack", None)
        if lora_stack:
            stack.extend(lora_stack)
            for lora_path, _, _ in lora_stack:
                lora_name = extract_lora_name(lora_path)
                _, trigger_words = get_lora_info_remote(lora_name)
                all_trigger_words.extend(trigger_words)

        loras_list = get_loras_list(kwargs)
        for lora in loras_list:
            if not lora.get("active", False):
                continue

            lora_name = lora["name"]
            model_strength = float(lora["strength"])
            clip_strength = float(lora.get("clipStrength", model_strength))

            lora_path, trigger_words = get_lora_info_remote(lora_name)

            stack.append((lora_path.replace("/", os.sep), model_strength, clip_strength))
            active_loras.append((lora_name, model_strength, clip_strength))
            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        formatted_loras = []
        for name, model_strength, clip_strength in active_loras:
            if abs(model_strength - clip_strength) > 0.001:
                formatted_loras.append(f"<lora:{name}:{str(model_strength).strip()}:{str(clip_strength).strip()}>")
            else:
                formatted_loras.append(f"<lora:{name}:{str(model_strength).strip()}>")

        active_loras_text = " ".join(formatted_loras)
        return (stack, trigger_words_text, active_loras_text)
