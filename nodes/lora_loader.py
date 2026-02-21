"""Remote LoRA Loader nodes — fetch metadata from the remote LoRA Manager."""
from __future__ import annotations

import logging
import re

from nodes import LoraLoader  # type: ignore

from .remote_utils import get_lora_info_remote
from .utils import FlexibleOptionalInputType, any_type, extract_lora_name, get_loras_list, nunchaku_load_lora

logger = logging.getLogger(__name__)


class LoraLoaderRemoteLM:
    NAME = "Lora Loader (Remote, LoraManager)"
    CATEGORY = "Lora Manager/loaders"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "text": ("AUTOCOMPLETE_TEXT_LORAS", {
                    "placeholder": "Search LoRAs to add...",
                    "tooltip": "Format: <lora:lora_name:strength> separated by spaces or punctuation",
                }),
            },
            "optional": FlexibleOptionalInputType(any_type),
        }

    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING")
    RETURN_NAMES = ("MODEL", "CLIP", "trigger_words", "loaded_loras")
    FUNCTION = "load_loras"

    def load_loras(self, model, text, **kwargs):
        loaded_loras = []
        all_trigger_words = []

        clip = kwargs.get("clip", None)
        lora_stack = kwargs.get("lora_stack", None)

        is_nunchaku_model = False
        try:
            model_wrapper = model.model.diffusion_model
            if model_wrapper.__class__.__name__ == "ComfyFluxWrapper":
                is_nunchaku_model = True
                logger.info("Detected Nunchaku Flux model")
        except (AttributeError, TypeError):
            pass

        # Process lora_stack
        if lora_stack:
            for lora_path, model_strength, clip_strength in lora_stack:
                if is_nunchaku_model:
                    model = nunchaku_load_lora(model, lora_path, model_strength)
                else:
                    model, clip = LoraLoader().load_lora(model, clip, lora_path, model_strength, clip_strength)

                lora_name = extract_lora_name(lora_path)
                _, trigger_words = get_lora_info_remote(lora_name)
                all_trigger_words.extend(trigger_words)
                if not is_nunchaku_model and abs(model_strength - clip_strength) > 0.001:
                    loaded_loras.append(f"{lora_name}: {model_strength},{clip_strength}")
                else:
                    loaded_loras.append(f"{lora_name}: {model_strength}")

        # Process loras from widget
        loras_list = get_loras_list(kwargs)
        for lora in loras_list:
            if not lora.get("active", False):
                continue

            lora_name = lora["name"]
            model_strength = float(lora["strength"])
            clip_strength = float(lora.get("clipStrength", model_strength))

            lora_path, trigger_words = get_lora_info_remote(lora_name)

            if is_nunchaku_model:
                model = nunchaku_load_lora(model, lora_path, model_strength)
            else:
                model, clip = LoraLoader().load_lora(model, clip, lora_path, model_strength, clip_strength)

            if not is_nunchaku_model and abs(model_strength - clip_strength) > 0.001:
                loaded_loras.append(f"{lora_name}: {model_strength},{clip_strength}")
            else:
                loaded_loras.append(f"{lora_name}: {model_strength}")

            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        formatted_loras = []
        for item in loaded_loras:
            parts = item.split(":")
            lora_name = parts[0]
            strength_parts = parts[1].strip().split(",")
            if len(strength_parts) > 1:
                formatted_loras.append(f"<lora:{lora_name}:{strength_parts[0].strip()}:{strength_parts[1].strip()}>")
            else:
                formatted_loras.append(f"<lora:{lora_name}:{strength_parts[0].strip()}>")

        formatted_loras_text = " ".join(formatted_loras)
        return (model, clip, trigger_words_text, formatted_loras_text)


class LoraTextLoaderRemoteLM:
    NAME = "LoRA Text Loader (Remote, LoraManager)"
    CATEGORY = "Lora Manager/loaders"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "lora_syntax": ("STRING", {
                    "forceInput": True,
                    "tooltip": "Format: <lora:lora_name:strength> separated by spaces or punctuation",
                }),
            },
            "optional": {
                "clip": ("CLIP",),
                "lora_stack": ("LORA_STACK",),
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING")
    RETURN_NAMES = ("MODEL", "CLIP", "trigger_words", "loaded_loras")
    FUNCTION = "load_loras_from_text"

    def parse_lora_syntax(self, text):
        pattern = r"<lora:([^:>]+):([^:>]+)(?::([^:>]+))?>"
        matches = re.findall(pattern, text, re.IGNORECASE)
        loras = []
        for match in matches:
            loras.append({
                "name": match[0],
                "model_strength": float(match[1]),
                "clip_strength": float(match[2]) if match[2] else float(match[1]),
            })
        return loras

    def load_loras_from_text(self, model, lora_syntax, clip=None, lora_stack=None):
        loaded_loras = []
        all_trigger_words = []

        is_nunchaku_model = False
        try:
            model_wrapper = model.model.diffusion_model
            if model_wrapper.__class__.__name__ == "ComfyFluxWrapper":
                is_nunchaku_model = True
                logger.info("Detected Nunchaku Flux model")
        except (AttributeError, TypeError):
            pass

        if lora_stack:
            for lora_path, model_strength, clip_strength in lora_stack:
                if is_nunchaku_model:
                    model = nunchaku_load_lora(model, lora_path, model_strength)
                else:
                    model, clip = LoraLoader().load_lora(model, clip, lora_path, model_strength, clip_strength)

                lora_name = extract_lora_name(lora_path)
                _, trigger_words = get_lora_info_remote(lora_name)
                all_trigger_words.extend(trigger_words)
                if not is_nunchaku_model and abs(model_strength - clip_strength) > 0.001:
                    loaded_loras.append(f"{lora_name}: {model_strength},{clip_strength}")
                else:
                    loaded_loras.append(f"{lora_name}: {model_strength}")

        parsed_loras = self.parse_lora_syntax(lora_syntax)
        for lora in parsed_loras:
            lora_name = lora["name"]
            model_strength = lora["model_strength"]
            clip_strength = lora["clip_strength"]

            lora_path, trigger_words = get_lora_info_remote(lora_name)

            if is_nunchaku_model:
                model = nunchaku_load_lora(model, lora_path, model_strength)
            else:
                model, clip = LoraLoader().load_lora(model, clip, lora_path, model_strength, clip_strength)

            if not is_nunchaku_model and abs(model_strength - clip_strength) > 0.001:
                loaded_loras.append(f"{lora_name}: {model_strength},{clip_strength}")
            else:
                loaded_loras.append(f"{lora_name}: {model_strength}")

            all_trigger_words.extend(trigger_words)

        trigger_words_text = ",, ".join(all_trigger_words) if all_trigger_words else ""

        formatted_loras = []
        for item in loaded_loras:
            parts = item.split(":")
            lora_name = parts[0].strip()
            strength_parts = parts[1].strip().split(",")
            if len(strength_parts) > 1:
                formatted_loras.append(f"<lora:{lora_name}:{strength_parts[0].strip()}:{strength_parts[1].strip()}>")
            else:
                formatted_loras.append(f"<lora:{lora_name}:{strength_parts[0].strip()}>")

        formatted_loras_text = " ".join(formatted_loras)
        return (model, clip, trigger_words_text, formatted_loras_text)
