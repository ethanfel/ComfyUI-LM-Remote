"""
ComfyUI-LM-Remote — Remote LoRA Manager integration for ComfyUI.

Provides:
1. A reverse-proxy middleware that forwards all LoRA Manager API/UI/WS
   requests to a remote Docker instance (including JS widget files).
2. Remote-aware node classes that fetch metadata via HTTP instead of the
   local ServiceRegistry, while still loading LoRA files from local
   NFS/SMB-mounted paths.
3. Local send_sync handlers so widget events (trigger words, lora code
   updates) are broadcast to the local ComfyUI frontend.

Does NOT require the original ComfyUI-Lora-Manager package to be installed.
Widget JS files and Vue widget types are served from the remote instance.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Import node classes ────────────────────────────────────────────────
from .nodes.lora_loader import LoraLoaderRemoteLM, LoraTextLoaderRemoteLM
from .nodes.lora_stacker import LoraStackerRemoteLM
from .nodes.lora_randomizer import LoraRandomizerRemoteLM
from .nodes.lora_cycler import LoraCyclerRemoteLM
from .nodes.lora_pool import LoraPoolRemoteLM
from .nodes.save_image import SaveImageRemoteLM
from .nodes.wanvideo import WanVideoLoraSelectRemoteLM, WanVideoLoraTextSelectRemoteLM

# ── NODE_CLASS_MAPPINGS (how ComfyUI discovers nodes) ──────────────────
NODE_CLASS_MAPPINGS = {
    LoraLoaderRemoteLM.NAME: LoraLoaderRemoteLM,
    LoraTextLoaderRemoteLM.NAME: LoraTextLoaderRemoteLM,
    LoraStackerRemoteLM.NAME: LoraStackerRemoteLM,
    LoraRandomizerRemoteLM.NAME: LoraRandomizerRemoteLM,
    LoraCyclerRemoteLM.NAME: LoraCyclerRemoteLM,
    LoraPoolRemoteLM.NAME: LoraPoolRemoteLM,
    SaveImageRemoteLM.NAME: SaveImageRemoteLM,
    WanVideoLoraSelectRemoteLM.NAME: WanVideoLoraSelectRemoteLM,
    WanVideoLoraTextSelectRemoteLM.NAME: WanVideoLoraTextSelectRemoteLM,
}

# ── WEB_DIRECTORY tells ComfyUI where to find our JS extensions ───────
WEB_DIRECTORY = "./web/comfyui"

# ── Register proxy middleware ──────────────────────────────────────────
try:
    from server import PromptServer  # type: ignore
    from .proxy import register_proxy

    register_proxy(PromptServer.instance.app)
except Exception as exc:
    logger.warning("[LM-Remote] Could not register proxy middleware: %s", exc)

__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]
