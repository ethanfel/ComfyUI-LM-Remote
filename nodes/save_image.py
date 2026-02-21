"""Remote Save Image — uses remote API for hash lookups."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re

import folder_paths  # type: ignore
import numpy as np
from PIL import Image, PngImagePlugin

from ..remote_client import RemoteLoraClient

logger = logging.getLogger(__name__)

try:
    import piexif
except ImportError:
    piexif = None


class SaveImageRemoteLM:
    NAME = "Save Image (Remote, LoraManager)"
    CATEGORY = "Lora Manager/utils"
    DESCRIPTION = "Save images with embedded generation metadata (remote hash lookup)"

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""
        self.compress_level = 4
        self.counter = 0

    pattern_format = re.compile(r"(%[^%]+%)")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {
                    "default": "ComfyUI",
                    "tooltip": "Base filename. Supports %seed%, %width%, %height%, %model%, etc.",
                }),
                "file_format": (["png", "jpeg", "webp"], {
                    "tooltip": "Image format to save as.",
                }),
            },
            "optional": {
                "lossless_webp": ("BOOLEAN", {"default": False}),
                "quality": ("INT", {"default": 100, "min": 1, "max": 100}),
                "embed_workflow": ("BOOLEAN", {"default": False}),
                "add_counter_to_filename": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "id": "UNIQUE_ID",
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "process_image"
    OUTPUT_NODE = True

    # ------------------------------------------------------------------
    # Remote hash lookups
    # ------------------------------------------------------------------

    def _run_async(self, coro):
        """Run an async coroutine from sync context."""
        try:
            asyncio.get_running_loop()

            def _in_thread():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(_in_thread).result()
        except RuntimeError:
            return asyncio.run(coro)

    def get_lora_hash(self, lora_name):
        client = RemoteLoraClient.get_instance()
        return self._run_async(client.get_lora_hash(lora_name))

    def get_checkpoint_hash(self, checkpoint_path):
        if not checkpoint_path:
            return None
        checkpoint_name = os.path.splitext(os.path.basename(checkpoint_path))[0]
        client = RemoteLoraClient.get_instance()
        return self._run_async(client.get_checkpoint_hash(checkpoint_name))

    # ------------------------------------------------------------------
    # Metadata formatting (identical to original)
    # ------------------------------------------------------------------

    def format_metadata(self, metadata_dict):
        if not metadata_dict:
            return ""

        def add_param_if_not_none(param_list, label, value):
            if value is not None:
                param_list.append(f"{label}: {value}")

        prompt = metadata_dict.get("prompt", "")
        negative_prompt = metadata_dict.get("negative_prompt", "")
        loras_text = metadata_dict.get("loras", "")
        lora_hashes = {}

        if loras_text:
            prompt_with_loras = f"{prompt}\n{loras_text}"
            lora_matches = re.findall(r"<lora:([^:]+):([^>]+)>", loras_text)
            for lora_name, _ in lora_matches:
                hash_value = self.get_lora_hash(lora_name)
                if hash_value:
                    lora_hashes[lora_name] = hash_value
        else:
            prompt_with_loras = prompt

        metadata_parts = [prompt_with_loras]

        if negative_prompt:
            metadata_parts.append(f"Negative prompt: {negative_prompt}")

        params = []

        if "steps" in metadata_dict:
            add_param_if_not_none(params, "Steps", metadata_dict.get("steps"))

        sampler_name = None
        scheduler_name = None

        if "sampler" in metadata_dict:
            sampler_mapping = {
                "euler": "Euler", "euler_ancestral": "Euler a",
                "dpm_2": "DPM2", "dpm_2_ancestral": "DPM2 a",
                "heun": "Heun", "dpm_fast": "DPM fast",
                "dpm_adaptive": "DPM adaptive", "lms": "LMS",
                "dpmpp_2s_ancestral": "DPM++ 2S a", "dpmpp_sde": "DPM++ SDE",
                "dpmpp_sde_gpu": "DPM++ SDE", "dpmpp_2m": "DPM++ 2M",
                "dpmpp_2m_sde": "DPM++ 2M SDE", "dpmpp_2m_sde_gpu": "DPM++ 2M SDE",
                "ddim": "DDIM",
            }
            sampler_name = sampler_mapping.get(metadata_dict["sampler"], metadata_dict["sampler"])

        if "scheduler" in metadata_dict:
            scheduler_mapping = {
                "normal": "Simple", "karras": "Karras",
                "exponential": "Exponential", "sgm_uniform": "SGM Uniform",
                "sgm_quadratic": "SGM Quadratic",
            }
            scheduler_name = scheduler_mapping.get(metadata_dict["scheduler"], metadata_dict["scheduler"])

        if sampler_name:
            if scheduler_name:
                params.append(f"Sampler: {sampler_name} {scheduler_name}")
            else:
                params.append(f"Sampler: {sampler_name}")

        if "guidance" in metadata_dict:
            add_param_if_not_none(params, "CFG scale", metadata_dict.get("guidance"))
        elif "cfg_scale" in metadata_dict:
            add_param_if_not_none(params, "CFG scale", metadata_dict.get("cfg_scale"))
        elif "cfg" in metadata_dict:
            add_param_if_not_none(params, "CFG scale", metadata_dict.get("cfg"))

        if "seed" in metadata_dict:
            add_param_if_not_none(params, "Seed", metadata_dict.get("seed"))

        if "size" in metadata_dict:
            add_param_if_not_none(params, "Size", metadata_dict.get("size"))

        if "checkpoint" in metadata_dict:
            checkpoint = metadata_dict.get("checkpoint")
            if checkpoint is not None:
                model_hash = self.get_checkpoint_hash(checkpoint)
                checkpoint_name = os.path.splitext(os.path.basename(checkpoint))[0]
                if model_hash:
                    params.append(f"Model hash: {model_hash[:10]}, Model: {checkpoint_name}")
                else:
                    params.append(f"Model: {checkpoint_name}")

        if lora_hashes:
            lora_hash_parts = [f"{n}: {h[:10]}" for n, h in lora_hashes.items()]
            params.append(f'Lora hashes: "{", ".join(lora_hash_parts)}"')

        metadata_parts.append(", ".join(params))
        return "\n".join(metadata_parts)

    def format_filename(self, filename, metadata_dict):
        if not metadata_dict:
            return filename

        result = re.findall(self.pattern_format, filename)
        for segment in result:
            parts = segment.replace("%", "").split(":")
            key = parts[0]

            if key == "seed" and "seed" in metadata_dict:
                filename = filename.replace(segment, str(metadata_dict.get("seed", "")))
            elif key == "width" and "size" in metadata_dict:
                size = metadata_dict.get("size", "x")
                w = size.split("x")[0] if isinstance(size, str) else size[0]
                filename = filename.replace(segment, str(w))
            elif key == "height" and "size" in metadata_dict:
                size = metadata_dict.get("size", "x")
                h = size.split("x")[1] if isinstance(size, str) else size[1]
                filename = filename.replace(segment, str(h))
            elif key == "pprompt" and "prompt" in metadata_dict:
                p = metadata_dict.get("prompt", "").replace("\n", " ")
                if len(parts) >= 2:
                    p = p[: int(parts[1])]
                filename = filename.replace(segment, p.strip())
            elif key == "nprompt" and "negative_prompt" in metadata_dict:
                p = metadata_dict.get("negative_prompt", "").replace("\n", " ")
                if len(parts) >= 2:
                    p = p[: int(parts[1])]
                filename = filename.replace(segment, p.strip())
            elif key == "model":
                model_value = metadata_dict.get("checkpoint")
                if isinstance(model_value, (bytes, os.PathLike)):
                    model_value = str(model_value)
                if not isinstance(model_value, str) or not model_value:
                    model = "model_unavailable"
                else:
                    model = os.path.splitext(os.path.basename(model_value))[0]
                if len(parts) >= 2:
                    model = model[: int(parts[1])]
                filename = filename.replace(segment, model)
            elif key == "date":
                from datetime import datetime
                now = datetime.now()
                date_table = {
                    "yyyy": f"{now.year:04d}", "yy": f"{now.year % 100:02d}",
                    "MM": f"{now.month:02d}", "dd": f"{now.day:02d}",
                    "hh": f"{now.hour:02d}", "mm": f"{now.minute:02d}",
                    "ss": f"{now.second:02d}",
                }
                if len(parts) >= 2:
                    date_format = parts[1]
                else:
                    date_format = "yyyyMMddhhmmss"
                for k, v in date_table.items():
                    date_format = date_format.replace(k, v)
                filename = filename.replace(segment, date_format)

        return filename

    # ------------------------------------------------------------------
    # Image saving
    # ------------------------------------------------------------------

    def save_images(self, images, filename_prefix, file_format, id, prompt=None,
                    extra_pnginfo=None, lossless_webp=True, quality=100,
                    embed_workflow=False, add_counter_to_filename=True):
        results = []

        # Try to get metadata from the original LoRA Manager's collector.
        # The package directory name varies across installs, so we search
        # sys.modules for any loaded module whose path ends with the
        # expected submodule.
        metadata_dict = {}
        try:
            get_metadata = None
            MetadataProcessor = None

            import sys
            for mod_name, mod in sys.modules.items():
                if mod is None:
                    continue
                if mod_name.endswith(".py.metadata_collector") and hasattr(mod, "get_metadata"):
                    get_metadata = mod.get_metadata
                if mod_name.endswith(".py.metadata_collector.metadata_processor") and hasattr(mod, "MetadataProcessor"):
                    MetadataProcessor = mod.MetadataProcessor
                if get_metadata and MetadataProcessor:
                    break

            if get_metadata and MetadataProcessor:
                raw_metadata = get_metadata()
                metadata_dict = MetadataProcessor.to_dict(raw_metadata, id)
            else:
                logger.debug("[LM-Remote] metadata_collector not found in loaded modules")
        except Exception:
            logger.debug("[LM-Remote] metadata_collector not available, saving without generation metadata")

        metadata = self.format_metadata(metadata_dict)
        filename_prefix = self.format_filename(filename_prefix, metadata_dict)

        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0]
        )

        if not os.path.exists(full_output_folder):
            os.makedirs(full_output_folder, exist_ok=True)

        for i, image in enumerate(images):
            img = 255.0 * image.cpu().numpy()
            img = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))

            base_filename = filename
            if add_counter_to_filename:
                current_counter = counter + i
                base_filename += f"_{current_counter:05}_"

            if file_format == "png":
                file = base_filename + ".png"
                save_kwargs = {"compress_level": self.compress_level}
                pnginfo = PngImagePlugin.PngInfo()
            elif file_format == "jpeg":
                file = base_filename + ".jpg"
                save_kwargs = {"quality": quality, "optimize": True}
            elif file_format == "webp":
                file = base_filename + ".webp"
                save_kwargs = {"quality": quality, "lossless": lossless_webp, "method": 0}

            file_path = os.path.join(full_output_folder, file)

            try:
                if file_format == "png":
                    if metadata:
                        pnginfo.add_text("parameters", metadata)
                    if embed_workflow and extra_pnginfo is not None:
                        pnginfo.add_text("workflow", json.dumps(extra_pnginfo["workflow"]))
                    save_kwargs["pnginfo"] = pnginfo
                    img.save(file_path, format="PNG", **save_kwargs)
                elif file_format == "jpeg" and piexif:
                    if metadata:
                        try:
                            exif_dict = {"Exif": {piexif.ExifIFD.UserComment: b"UNICODE\0" + metadata.encode("utf-16be")}}
                            save_kwargs["exif"] = piexif.dump(exif_dict)
                        except Exception as e:
                            logger.error("Error adding EXIF data: %s", e)
                    img.save(file_path, format="JPEG", **save_kwargs)
                elif file_format == "webp" and piexif:
                    try:
                        exif_dict = {}
                        if metadata:
                            exif_dict["Exif"] = {piexif.ExifIFD.UserComment: b"UNICODE\0" + metadata.encode("utf-16be")}
                        if embed_workflow and extra_pnginfo is not None:
                            exif_dict["0th"] = {piexif.ImageIFD.ImageDescription: "Workflow:" + json.dumps(extra_pnginfo["workflow"])}
                        save_kwargs["exif"] = piexif.dump(exif_dict)
                    except Exception as e:
                        logger.error("Error adding EXIF data: %s", e)
                    img.save(file_path, format="WEBP", **save_kwargs)
                else:
                    img.save(file_path)

                results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            except Exception as e:
                logger.error("Error saving image: %s", e)

        return results

    def process_image(self, images, id, filename_prefix="ComfyUI", file_format="png",
                      prompt=None, extra_pnginfo=None, lossless_webp=True, quality=100,
                      embed_workflow=False, add_counter_to_filename=True):
        os.makedirs(self.output_dir, exist_ok=True)

        if isinstance(images, (list, np.ndarray)):
            pass
        else:
            if len(images.shape) == 3:
                images = [images]
            else:
                images = [img for img in images]

        self.save_images(
            images, filename_prefix, file_format, id, prompt, extra_pnginfo,
            lossless_webp, quality, embed_workflow, add_counter_to_filename,
        )
        return (images,)
