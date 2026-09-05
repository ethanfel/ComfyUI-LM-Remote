# ComfyUI-LM-Remote

Remote-aware [LoRA Manager](https://github.com/willmiao/ComfyUI-Lora-Manager) nodes for ComfyUI. Fetches metadata (trigger words, hashes, model info) from a remote LoRA Manager instance via HTTP, while loading LoRA files from local NFS/SMB-mounted paths.

## Why?

When ComfyUI runs on a GPU workstation and LoRA Manager runs in Docker on a NAS (e.g., Unraid), the original LoRA Manager nodes can't access the remote metadata database. This package bridges that gap:

- **Proxy middleware** transparently forwards the LoRA Manager web UI and API to the remote instance
- **Remote nodes** fetch metadata via HTTP instead of local SQLite lookups
- **Local file loading** is unchanged -- LoRA files are loaded from shared storage (NFS/SMB)

```
ComfyUI Workstation                        NAS (Docker)
+--------------------------+              +------------------------+
| ComfyUI                  |              | LoRA Manager           |
|  +- ComfyUI-LM-Remote   |   HTTP API   |  +- SQLite metadata DB |
|  |  (this package)       |<------------>|  +- CivitAI sync       |
|  +- /mnt/loras/ (NFS)   |              |  +- Port 8188          |
+--------------------------+              +------------------------+
         |                                           |
         +------- Shared NFS/SMB storage ------------+
```

## Prerequisites

- A running LoRA Manager instance accessible over the network (e.g., in Docker)
- Shared storage so both machines see the same LoRA files at compatible paths

> **Note:** The original [ComfyUI-Lora-Manager](https://github.com/willmiao/ComfyUI-Lora-Manager) package is **not required**. Widget JS files and Vue widget types are served from the remote instance via the proxy. You may still install it alongside if you want the original (non-remote) nodes available too.

## Installation

Clone into your ComfyUI `custom_nodes/` directory:

```bash
cd /path/to/ComfyUI/custom_nodes/
git clone https://github.com/ethanfel/ComfyUI-LM-Remote.git
```

## Configuration

Open the configuration panel from any of these places:

- Click the gear in the **LoRA Info** sidebar
- Open **Settings**, select **LM Remote**, then click **Configure LM Remote**
- Run **Configure LM Remote** from the command palette

Enter the remote URL, adjust the timeout if needed, and click **Test connection**. **Save** validates the settings and applies them to new requests immediately; the ComfyUI process does not need to restart. If the remote URL changes, the panel offers a page reload so Manager assets and live progress reconnect to the new server. Save workflow edits before using it.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `remote_url` | string | `""` | URL of the remote LoRA Manager instance |
| `timeout` | int | `30` | HTTP request timeout in seconds |
| `path_mappings` | object | `{}` | Remote-to-local path prefix mapping (see below) |

Configuration is stored under `<ComfyUI user directory>/ComfyUI-LM-Remote/config.json`, outside the custom-node checkout. An existing package-level `config.json` is loaded as a legacy fallback and is left untouched. The first save through the panel migrates its values into ComfyUI user data.

The panel is the normal way to manage this file; manual editing is not required. For a headless or automated deployment, start from [`config.example.json`](config.example.json), place the copy outside the custom-node checkout, and point `LM_REMOTE_CONFIG` to it.

### Environment Variable Overrides

Environment variables take priority over the stored configuration without rewriting it. Overridden fields are shown as managed in the panel.

| Variable | Purpose |
|----------|---------|
| `LM_REMOTE_URL` | Overrides `remote_url`; the field is shown as managed in the panel |
| `LM_REMOTE_TIMEOUT` | Overrides `timeout`; the field is shown as managed in the panel |
| `LM_REMOTE_CONFIG` | Uses an explicit configuration file instead of the ComfyUI user-data path |

### Path Mappings

If the remote instance and local ComfyUI see LoRA files at different absolute paths, use `path_mappings` to translate:

```json
{
    "path_mappings": {
        "/data/models/loras": "/mnt/nas/models/loras"
    }
}
```

This maps the remote path prefix `/data/models/loras` to the local `/mnt/nas/models/loras`. Usually not needed if both machines use the same NFS mount point.

## Nodes

All nodes appear under the **Lora Manager** category in the ComfyUI node menu, with "(Remote, LoraManager)" in the name.

| Node | Description |
|------|-------------|
| **Lora Loader (Remote)** | Load LoRAs with trigger words from remote metadata. Supports Nunchaku Flux models. |
| **LoRA Text Loader (Remote)** | Load LoRAs from `<lora:name:strength>` text syntax. |
| **Lora Stacker (Remote)** | Stack multiple LoRAs into a LORA_STACK for downstream loaders. |
| **Lora Randomizer (Remote)** | Randomly sample LoRAs from the remote pool with configurable count and strength ranges. |
| **Lora Cycler (Remote)** | Sequentially cycle through LoRAs from the remote pool. |
| **Lora Pool (Remote)** | Configure pool filters (base model, tags, folders, favorites) for Randomizer/Cycler. |
| **Save Image (Remote)** | Save images with embedded generation metadata, using remote hash lookups for LoRA and checkpoint hashes. |
| **WanVideo Lora Select (Remote)** | Select LoRAs for WanVideo with block-level control. |
| **WanVideo Lora Select From Text (Remote)** | Select WanVideo LoRAs from text syntax. |

## LoRA Info Sidebar

Selecting a LoRA loader opens the **LoRA Info** sidebar and follows the node's current selection. It supports the stock ComfyUI loader, LM Remote nodes, and third-party loaders that expose standard `lora_name`, numbered LoRA, stack, or `<lora:name:strength>` values.

- If the selected LoRA is indexed by the remote LoRA Manager, the sidebar shows its image or video preview, file details, base model, trigger words, tags, usage tips, and direct model links.
- Cached community creations and Civitai examples appear with video controls, their shared prompt and negative prompt, generation settings, attribution, navigation, and one-click prompt copying.
- Community image fetches and per-model refreshes can run beyond the normal connection timeout; progress updates and cancellation remain available while they finish.
- If a node contains multiple active LoRAs, use the selector at the top of the sidebar to switch cards.
- LoRA Info closes automatically after the selected LoRA loader is cleared, but stays open while switching directly between loaders.
- If no Manager card exists, the sidebar offers name searches on LoRA Manager, Civitai, Civitai Red, and CivArchive.
- Duplicate filenames are not guessed: the sidebar asks you to choose the matching Manager path.

ComfyUI does not currently expose an extension API for adding custom tabs to the built-in Properties panel, so this feature uses its supported custom-sidebar API. It follows ComfyUI's configured sidebar location, including a right-side layout like Templates.

Auto-open is enabled by default. Disable it under **Settings > LM Remote > LoRA Info > Auto-open** if you prefer to open **LoRA Info** manually from the sidebar or command palette.

The gear beside the refresh button opens LM Remote connection settings without leaving the sidebar.

## How It Works

### Reverse Proxy

An aiohttp middleware is registered at startup that intercepts requests to LoRA Manager endpoints and forwards them to the remote instance:

**Proxied routes:**
- `/api/lm/*` -- all REST API endpoints (except send_sync routes below)
- `/extensions/ComfyUI-Lora-Manager/*` -- widget JS files and Vue widget bundle
- `/loras_static/*`, `/locales/*`, `/example_images_static/*` -- static assets
- `/loras`, `/checkpoints`, `/embeddings`, `/loras/recipes`, `/community`, `/statistics` -- web UI pages
- `/ws/fetch-progress`, `/ws/download-progress`, `/ws/init-progress` -- WebSocket connections

**Handled locally** (events broadcast to local browser via `send_sync`):
- `/api/lm/loras/get_trigger_words` -- fetches trigger words from remote, broadcasts `trigger_word_update`
- `/api/lm/update-lora-code` -- broadcasts `lora_code_update`
- `/api/lm/update-node-widget` -- broadcasts `lm_widget_update`
- `/api/lm/register-nodes` -- no-op in remote mode

### Remote Metadata

Nodes use `RemoteLoraClient` to fetch metadata from the remote LoRA Manager API. A 60-second in-memory cache avoids redundant API calls during workflow execution. The client queries:

- `GET /api/lm/loras/list` -- full LoRA list (cached)
- `GET /api/lm/loras/get-trigger-words` -- trigger words fallback
- `POST /api/lm/loras/random-sample` -- random sampling
- `POST /api/lm/loras/cycler-list` -- cycler ordering
- `GET /api/lm/checkpoints/list` -- checkpoint hashes (cached)

### Local File Loading

After fetching the relative path from the remote metadata, LoRA files are loaded locally via `folder_paths.get_full_path("loras", relative_path)`. No files are transferred over the network.

## Verification

After installation and configuration:

1. Restart ComfyUI once after installing the custom node
2. Open **Configure LM Remote**, enter the URL, and run **Test connection**
3. Save the configuration; no ComfyUI process restart is required
4. If prompted after a remote URL change, save workflow edits and reload the browser page
5. Open the LoRA Manager web UI -- it should load through the remote proxy
6. Add a stock or remote LoRA loader and click the node -- **LoRA Info** should open
7. Select a LoRA -- its Manager card (or external search links) should appear and remote trigger words should populate where supported
8. Run the workflow -- the LoRA loads from local shared storage

## License

Same license as [ComfyUI-Lora-Manager](https://github.com/willmiao/ComfyUI-Lora-Manager).
