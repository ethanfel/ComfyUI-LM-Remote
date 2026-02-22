/**
 * Bootstrap loader for LoRA Manager Vue widget bundle.
 *
 * When the original ComfyUI-Lora-Manager package is NOT installed locally,
 * the Vue widget types (AUTOCOMPLETE_TEXT_LORAS, LORAS, LORA_POOL_CONFIG,
 * RANDOMIZER_CONFIG, CYCLER_CONFIG) would never be registered and nodes
 * wouldn't render.
 *
 * This script loads the Vue widget bundle from the remote instance via the
 * proxy at /extensions/ComfyUI-Lora-Manager/vue-widgets/.  If the original
 * package IS installed, the bundle is already loaded and we skip the import.
 */
import { app } from "../../scripts/app.js";

const alreadyLoaded = app.extensions?.some(
    ext => ext.name === "LoraManager.VueWidgets"
);

if (!alreadyLoaded) {
    try {
        await import("/extensions/ComfyUI-Lora-Manager/vue-widgets/lora-manager-widgets.js");
    } catch (err) {
        console.warn("[LM-Remote] Failed to load Vue widget bundle:", err);
    }
}
