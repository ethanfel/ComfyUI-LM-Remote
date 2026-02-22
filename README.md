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

Edit `config.json` in the package directory:

```json
{
    "remote_url": "http://192.168.1.3:8188",
    "timeout": 30,
    "path_mappings": {}
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `remote_url` | string | `""` | URL of the remote LoRA Manager instance |
| `timeout` | int | `30` | HTTP request timeout in seconds |
| `path_mappings` | object | `{}` | Remote-to-local path prefix mapping (see below) |

### Environment Variable Overrides

Environment variables take priority over `config.json`:

| Variable | Overrides |
|----------|-----------|
| `LM_REMOTE_URL` | `remote_url` |
| `LM_REMOTE_TIMEOUT` | `timeout` |

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

## How It Works

### Reverse Proxy

An aiohttp middleware is registered at startup that intercepts requests to LoRA Manager endpoints and forwards them to the remote instance:

**Proxied routes:**
- `/api/lm/*` -- all REST API endpoints (except send_sync routes below)
- `/extensions/ComfyUI-Lora-Manager/*` -- widget JS files and Vue widget bundle
- `/loras_static/*`, `/locales/*`, `/example_images_static/*` -- static assets
- `/loras`, `/checkpoints`, `/embeddings`, `/loras/recipes`, `/statistics` -- web UI pages
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

1. Restart ComfyUI
2. Check logs for: `[LM-Remote] Proxy routes registered -> http://192.168.1.3:8188`
3. Open ComfyUI -- the LoRA Manager web UI should load (proxied from remote)
4. Add a "Lora Loader (Remote, LoraManager)" node to a workflow
5. Select a LoRA -- trigger words should populate from remote metadata
6. Run the workflow -- the LoRA loads from local shared storage

## License

Same license as [ComfyUI-Lora-Manager](https://github.com/willmiao/ComfyUI-Lora-Manager).
