"""Minimal utility classes/functions copied from the original LoRA Manager.

Only the pieces needed by the remote node classes are included here so that
ComfyUI-LM-Remote can function independently of the original package's Python
internals (while still requiring its JS widget files).
"""
from __future__ import annotations

import copy
import logging
import os
import sys

import folder_paths  # type: ignore

logger = logging.getLogger(__name__)


class AnyType(str):
    """A special class that is always equal in not-equal comparisons.

    Credit to pythongosssss.
    """

    def __ne__(self, __value: object) -> bool:
        return False


class FlexibleOptionalInputType(dict):
    """Allow flexible/dynamic input types on ComfyUI nodes.

    Credit to Regis Gaughan, III (rgthree).
    """

    def __init__(self, type):
        self.type = type

    def __getitem__(self, key):
        return (self.type,)

    def __contains__(self, key):
        return True


any_type = AnyType("*")


def extract_lora_name(lora_path: str) -> str:
    """``'IL\\\\aorunIllstrious.safetensors'`` -> ``'aorunIllstrious'``"""
    basename = os.path.basename(lora_path)
    return os.path.splitext(basename)[0]


def get_loras_list(kwargs: dict) -> list:
    """Extract loras list from either old or new kwargs format."""
    if "loras" not in kwargs:
        return []
    loras_data = kwargs["loras"]
    if isinstance(loras_data, dict) and "__value__" in loras_data:
        return loras_data["__value__"]
    elif isinstance(loras_data, list):
        return loras_data
    else:
        logger.warning("Unexpected loras format: %s", type(loras_data))
        return []


# ---------------------------------------------------------------------------
# Nunchaku LoRA helpers (copied verbatim from original)
# ---------------------------------------------------------------------------

def load_state_dict_in_safetensors(path, device="cpu", filter_prefix=""):
    import safetensors.torch

    state_dict = {}
    with safetensors.torch.safe_open(path, framework="pt", device=device) as f:
        for k in f.keys():
            if filter_prefix and not k.startswith(filter_prefix):
                continue
            state_dict[k.removeprefix(filter_prefix)] = f.get_tensor(k)
    return state_dict


def to_diffusers(input_lora):
    import torch
    from diffusers.utils.state_dict_utils import convert_unet_state_dict_to_peft
    from diffusers.loaders import FluxLoraLoaderMixin

    if isinstance(input_lora, str):
        tensors = load_state_dict_in_safetensors(input_lora, device="cpu")
    else:
        tensors = {k: v for k, v in input_lora.items()}

    for k, v in tensors.items():
        if v.dtype not in [torch.float64, torch.float32, torch.bfloat16, torch.float16]:
            tensors[k] = v.to(torch.bfloat16)

    new_tensors = FluxLoraLoaderMixin.lora_state_dict(tensors)
    new_tensors = convert_unet_state_dict_to_peft(new_tensors)
    return new_tensors


def nunchaku_load_lora(model, lora_name, lora_strength):
    lora_path = lora_name if os.path.isfile(lora_name) else folder_paths.get_full_path("loras", lora_name)
    if not lora_path or not os.path.isfile(lora_path):
        logger.warning("Skipping LoRA '%s' because it could not be found", lora_name)
        return model

    model_wrapper = model.model.diffusion_model

    module_name = model_wrapper.__class__.__module__
    module = sys.modules.get(module_name)
    copy_with_ctx = getattr(module, "copy_with_ctx", None)

    if copy_with_ctx is not None:
        ret_model_wrapper, ret_model = copy_with_ctx(model_wrapper)
        ret_model_wrapper.loras = [*model_wrapper.loras, (lora_path, lora_strength)]
    else:
        logger.warning(
            "Please upgrade ComfyUI-nunchaku to 1.1.0 or above for better LoRA support. "
            "Falling back to legacy loading logic."
        )
        transformer = model_wrapper.model
        model_wrapper.model = None
        ret_model = copy.deepcopy(model)
        ret_model_wrapper = ret_model.model.diffusion_model
        model_wrapper.model = transformer
        ret_model_wrapper.model = transformer
        ret_model_wrapper.loras.append((lora_path, lora_strength))

    sd = to_diffusers(lora_path)

    if "transformer.x_embedder.lora_A.weight" in sd:
        new_in_channels = sd["transformer.x_embedder.lora_A.weight"].shape[1]
        assert new_in_channels % 4 == 0
        new_in_channels = new_in_channels // 4
        old_in_channels = ret_model.model.model_config.unet_config["in_channels"]
        if old_in_channels < new_in_channels:
            ret_model.model.model_config.unet_config["in_channels"] = new_in_channels

    return ret_model
