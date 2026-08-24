const WEIGHT_EXTENSION = /\.(?:safetensors|ckpt|pt|pth|bin)$/i;
const LORA_SYNTAX = /<lora:([^:>]+)(?::[^>]*)?>/gi;
const VIDEO_EXTENSION = /\.(?:mp4|webm|mov|m4v)$/i;
const NSFW_LEVELS = {
  pg: 1,
  pg13: 2,
  r: 4,
  x: 8,
  xxx: 16,
  blocked: 32,
};
const DISABLED_VALUES = new Set([
  "",
  "none",
  "null",
  "disabled",
  "select a lora",
  "select lora",
]);

export function cleanLoraName(value) {
  if (typeof value !== "string") return "";

  const trimmed = value.trim().replace(/^["']|["']$/g, "");
  const exactSyntax = /^<lora:([^:>]+)(?::[^>]*)?>$/i.exec(trimmed);
  const name = (exactSyntax?.[1] || trimmed)
    .replace(/\\/g, "/")
    .replace(/\/{2,}/g, "/")
    .replace(/^\.\//, "")
    .trim();

  if (DISABLED_VALUES.has(name.toLowerCase())) return "";
  return name;
}

export function normalizeLoraIdentifier(value) {
  return cleanLoraName(value)
    .replace(WEIGHT_EXTENSION, "")
    .replace(/^\/+|\/+$/g, "")
    .toLowerCase();
}

export function loraSearchTerm(value) {
  const clean = cleanLoraName(value);
  const basename = clean.replace(/\\/g, "/").split("/").pop() || clean;
  return basename.replace(WEIGHT_EXTENSION, "").trim();
}

function formatUsageTipLabel(value) {
  return String(value)
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}

function formatUsageTipValue(value) {
  if (value == null) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function normalizeUsageTips(value) {
  let parsed = value;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text || text === "{}" || text === "[]" || text === "null") return [];
    try {
      parsed = JSON.parse(text);
    } catch {
      return [{ label: "Note", value: text }];
    }
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return Object.entries(parsed)
    .map(([key, entry]) => ({
      label: formatUsageTipLabel(key),
      value: formatUsageTipValue(entry),
    }))
    .filter((entry) => entry.value !== "");
}

function decodeMediaPath(value) {
  let decoded = String(value || "");
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const next = decodeURIComponent(decoded);
      if (next === decoded) break;
      decoded = next;
    } catch {
      break;
    }
  }
  return decoded;
}

function mediaPath(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  try {
    const parsed = new URL(text, "http://localhost/");
    return decodeMediaPath(parsed.searchParams.get("path") || parsed.pathname)
      .split(/[?#]/, 1)[0]
      .toLowerCase();
  } catch {
    return decodeMediaPath(text).split(/[?#]/, 1)[0].toLowerCase();
  }
}

export function isVideoMedia(value, declaredType = "") {
  const type = String(declaredType || "").trim().toLowerCase();
  if (type === "video" || type.startsWith("video/")) return true;
  return VIDEO_EXTENSION.test(mediaPath(value));
}

function safeMediaReference(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  try {
    const parsed = new URL(value.trim(), "http://localhost/");
    return !parsed.username &&
      !parsed.password &&
      (parsed.protocol === "http:" || parsed.protocol === "https:")
      ? value.trim()
      : "";
  } catch {
    return "";
  }
}

function stringValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function firstString(...values) {
  for (const value of values) {
    const text = stringValue(value);
    if (text) return text;
  }
  return "";
}

function objectValue(value) {
  let parsed = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      return {};
    }
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed
    : {};
}

function generationMetadata(item) {
  const direct = objectValue(item?.meta);
  return { ...direct, ...objectValue(direct.meta) };
}

function normalizeNsfwLevel(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 0 ? value : null;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  const normalized = value.trim().toLowerCase().replace(/[\s_-]+/g, "");
  if (normalized === "unknown") return null;
  if (normalized in NSFW_LEVELS) return NSFW_LEVELS[normalized];
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

function optimizeMediaUrl(value, mediaType) {
  const safe = safeMediaReference(value);
  if (!safe) return "";
  try {
    const parsed = new URL(safe, "http://localhost/");
    if (
      parsed.hostname !== "civitai.com" &&
      parsed.hostname.endsWith(".civitai.com") &&
      parsed.pathname.includes("/original=true")
    ) {
      const replacement =
        mediaType === "video"
          ? "/transcode=true,width=450,optimized=true"
          : "/width=450,optimized=true";
      parsed.pathname = parsed.pathname.replace("/original=true", replacement);
      return parsed.href;
    }
  } catch {
    return safe;
  }
  return safe;
}

function mediaIdentity(item, url) {
  const id = item?.civitai_image_id ?? item?.id;
  return id != null && String(id).trim()
    ? `id:${String(id).trim()}`
    : `url:${url}`;
}

function localExampleFile(item, index, exampleFiles) {
  if (!Array.isArray(exampleFiles) || !exampleFiles.length) return null;
  if (typeof item?.id === "string" && item.id) {
    const prefix = `custom_${item.id}`;
    return exampleFiles.find((file) =>
      String(file?.name || "").startsWith(prefix)
    );
  }
  return exampleFiles.find((file) => {
    const match = /(?:^|\/)image_(\d+)\./i.exec(String(file?.name || ""));
    return match && Number(match[1]) === index;
  });
}

function normalizeMediaItem(item, source, localFile = null) {
  if (!item || typeof item !== "object" || item.downloadFailed) return null;
  const meta = generationMetadata(item);
  const rawUrl = firstString(
    localFile?.path,
    item.preview_url,
    item.image_url,
    item.url
  );
  const mediaType = isVideoMedia(
    rawUrl,
    localFile?.is_video ? "video" : item.media_type || item.type
  )
    ? "video"
    : "image";
  const url = optimizeMediaUrl(rawUrl, mediaType);
  if (!url) return null;

  const prompt = firstString(item.prompt, meta.prompt);
  const negativePrompt = firstString(
    item.negative_prompt,
    item.negativePrompt,
    meta.negative_prompt,
    meta.negativePrompt
  );
  const sizeLabel = firstString(item.size, meta.Size, meta.size);
  const sizeMatch = /^(\d+)\s*[x×]\s*(\d+)$/i.exec(sizeLabel);
  const width = Number(item.width || sizeMatch?.[1]);
  const height = Number(item.height || sizeMatch?.[2]);
  const reactionCount = [
    item.like_count,
    item.heart_count,
    item.laugh_count,
    item.comment_count,
  ].reduce((sum, value) => {
    const count = Number(value);
    return sum + (Number.isFinite(count) && count > 0 ? count : 0);
  }, 0);

  return {
    id: mediaIdentity(item, url),
    source,
    url,
    thumbnailUrl: safeMediaReference(item.thumbnail_url),
    mediaType,
    prompt,
    negativePrompt,
    username: firstString(item.username, item.creator?.username),
    width: Number.isFinite(width) && width > 0 ? width : null,
    height: Number.isFinite(height) && height > 0 ? height : null,
    steps: item.steps ?? meta.steps ?? meta.Steps ?? null,
    sampler: item.sampler ?? meta.sampler ?? meta.Sampler ?? "",
    cfgScale:
      item.cfg_scale ?? meta.cfg_scale ?? meta.cfgScale ?? meta.CFG ?? null,
    seed: item.seed ?? meta.seed ?? meta.Seed ?? null,
    denoise: item.denoise ?? meta.denoise ?? null,
    clipSkip: meta.clip_skip ?? meta.clipSkip ?? null,
    modelName: firstString(item.model_name, meta.Model, meta.model),
    baseModel: firstString(
      item.base_model,
      item.baseModel,
      meta.base_model,
      meta.baseModel
    ),
    nsfwLevel: normalizeNsfwLevel(
      item.nsfwLevel ?? item.nsfw_level ?? item.nsfw
    ),
    reactionCount,
    civitaiImageId: item.civitai_image_id ?? item.id ?? null,
  };
}

function missingMediaValue(value) {
  return value == null || value === "";
}

function mergeMediaItem(primary, supplement) {
  const merged = { ...primary };
  const fillFields = [
    "thumbnailUrl",
    "prompt",
    "negativePrompt",
    "username",
    "width",
    "height",
    "steps",
    "sampler",
    "cfgScale",
    "seed",
    "denoise",
    "clipSkip",
    "modelName",
    "baseModel",
    "civitaiImageId",
  ];
  for (const field of fillFields) {
    if (missingMediaValue(merged[field]) && !missingMediaValue(supplement[field])) {
      merged[field] = supplement[field];
    }
  }
  if (!String(merged.url).startsWith("/") && String(supplement.url).startsWith("/")) {
    merged.url = supplement.url;
  }
  if (supplement.mediaType === "video") merged.mediaType = "video";
  const levels = [merged.nsfwLevel, supplement.nsfwLevel].filter(
    (value) => typeof value === "number" && Number.isFinite(value)
  );
  merged.nsfwLevel = levels.length ? Math.max(...levels) : null;
  merged.reactionCount = Math.max(
    Number(merged.reactionCount) || 0,
    Number(supplement.reactionCount) || 0
  );
  return merged;
}

export function mergeCivitaiMetadata(summary, details) {
  const merged = { ...objectValue(summary) };
  for (const [key, value] of Object.entries(objectValue(details))) {
    if (value !== undefined) merged[key] = value;
  }
  return merged;
}

export function normalizeSharedMedia(
  communityImages,
  civitaiMetadata,
  exampleFiles = []
) {
  const civitai = objectValue(civitaiMetadata);
  const regularImages = Array.isArray(civitai.images) ? civitai.images : [];
  const customImages = Array.isArray(civitai.customImages)
    ? civitai.customImages
    : [];
  const candidates = [
    ...(Array.isArray(communityImages)
      ? communityImages.map((item) => [item, "Community creation", null])
      : []),
    ...regularImages.map((item, index) => [
      item,
      "Civitai example",
      localExampleFile(item, index, exampleFiles),
    ]),
    ...customImages.map((item, index) => [
      item,
      "Custom example",
      localExampleFile(item, regularImages.length + index, exampleFiles),
    ]),
  ];

  const indexes = new Map();
  const media = [];
  for (const [item, source, localFile] of candidates) {
    const normalized = normalizeMediaItem(item, source, localFile);
    if (!normalized) continue;
    const existingIndex = indexes.get(normalized.id);
    if (existingIndex != null) {
      media[existingIndex] = mergeMediaItem(media[existingIndex], normalized);
      continue;
    }
    indexes.set(normalized.id, media.length);
    media.push(normalized);
  }
  return media;
}

export function normalizeMediaSettings(value) {
  const settings = objectValue(value);
  return {
    blurMatureContent: settings.blur_mature_content !== false,
    matureBlurLevel:
      normalizeNsfwLevel(settings.mature_blur_level) ?? NSFW_LEVELS.r,
    showOnlySfw: settings.show_only_sfw === true,
  };
}

function unwrapSidebarValue(value) {
  return value && typeof value === "object" && "value" in value
    ? value.value
    : value;
}

export function getActiveSidebarTabId(manager) {
  if (!manager) return null;
  const sidebar = manager.sidebarTab || manager;
  return unwrapSidebarValue(
    sidebar?.activeSidebarTabId ?? manager.activeSidebarTabId
  );
}

export function closeActiveSidebarTab(manager, tabId) {
  if (!manager || getActiveSidebarTabId(manager) !== tabId) return false;
  const sidebar = manager.sidebarTab || manager;

  if (typeof manager.setActiveSidebarTab === "function") {
    try {
      manager.setActiveSidebarTab(null);
      if (getActiveSidebarTabId(manager) !== tabId) return true;
    } catch {
      // Older frontend wrappers may reject null.
    }
  }

  if (
    sidebar &&
    (typeof sidebar === "object" || typeof sidebar === "function") &&
    "activeSidebarTabId" in sidebar
  ) {
    try {
      const current = sidebar.activeSidebarTabId;
      if (current && typeof current === "object" && "value" in current) {
        current.value = null;
      } else {
        sidebar.activeSidebarTabId = null;
      }
      if (getActiveSidebarTabId(manager) !== tabId) return true;
    } catch {
      // Some frontend versions expose a readonly store property.
    }
  }

  const toggleTargets = sidebar === manager ? [sidebar] : [sidebar, manager];
  for (const target of toggleTargets) {
    if (typeof target?.toggleSidebarTab !== "function") continue;
    try {
      target.toggleSidebarTab(tabId);
      if (getActiveSidebarTabId(manager) !== tabId) return true;
    } catch {
      // Try the remaining compatibility paths.
    }
  }

  if (typeof manager.command?.execute === "function") {
    manager.command.execute(`Workspace.ToggleSidebarTab.${tabId}`);
    return true;
  }
  return false;
}

export function extractLoraSyntax(value) {
  if (typeof value !== "string") return [];
  const names = [];
  LORA_SYNTAX.lastIndex = 0;
  for (const match of value.matchAll(LORA_SYNTAX)) {
    const name = cleanLoraName(match[1]);
    if (name) names.push(name);
  }
  return names;
}

function isEnabledEntry(entry) {
  if (!entry || typeof entry !== "object") return true;
  return entry.active !== false && entry.enabled !== false && entry.on !== false;
}

function collectStructuredNames(value, output, allowObjectKeys = false) {
  if (typeof value === "string") {
    const syntaxNames = extractLoraSyntax(value);
    if (syntaxNames.length) {
      output.push(...syntaxNames);
    } else {
      const name = cleanLoraName(value);
      if (name) output.push(name);
    }
    return;
  }

  if (Array.isArray(value)) {
    value.forEach((entry) => collectStructuredNames(entry, output, true));
    return;
  }

  if (!value || typeof value !== "object" || !isEnabledEntry(value)) return;

  const namedValue =
    value.name ??
    value.lora_name ??
    value.loraName ??
    value.lora ??
    value.path ??
    value.file;
  if (typeof namedValue === "string") {
    collectStructuredNames(namedValue, output);
    return;
  }

  const nested = value.loras ?? value.items ?? value.values;
  if (Array.isArray(nested) || (nested && typeof nested === "object")) {
    collectStructuredNames(nested, output, true);
  }

  if (!allowObjectKeys) return;
  for (const [key, entry] of Object.entries(value)) {
    if (WEIGHT_EXTENSION.test(key) && entry !== false && entry !== 0) {
      collectStructuredNames(key, output);
    }
    if (!entry || typeof entry !== "object" || !isEnabledEntry(entry)) continue;
    const entryName =
      entry.name ??
      entry.lora_name ??
      entry.loraName ??
      entry.lora ??
      entry.path ??
      entry.file;
    if (typeof entryName === "string") {
      collectStructuredNames(entryName, output);
    } else if (WEIGHT_EXTENSION.test(key)) {
      collectStructuredNames(key, output);
    }
  }
}

function normalizeWidgetName(name) {
  return String(name || "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
}

function loraSlotIndex(name) {
  const normalized = normalizeWidgetName(name);
  const patterns = [
    /^lora_?(\d+)(?:_(?:name|path|file|text))?$/,
    /^lora_(?:name|path|file)(?:_text)?_?(\d+)$/,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(normalized);
    if (match) return String(Number(match[1]));
  }
  return null;
}

function isLoraSelectorName(name) {
  const normalized = normalizeWidgetName(name);
  return (
    /^(?:lora|loras|lora_name|lora_path|lora_file)$/.test(normalized) ||
    loraSlotIndex(normalized) !== null ||
    /^lora.*_(?:name|path|file)$/.test(normalized)
  );
}

function isFalseLike(value) {
  return (
    value === false ||
    value === 0 ||
    ["0", "false", "off", "disabled", "no"].includes(
      String(value || "").trim().toLowerCase()
    )
  );
}

function isLoraSlotEnabled(widgetName, widgetsByName) {
  const slot = loraSlotIndex(widgetName);
  if (slot === null) return true;
  const companionNames = [
    `enabled_${slot}`,
    `enable_${slot}`,
    `lora_enabled_${slot}`,
    `lora_${slot}_enabled`,
  ];
  for (const name of companionNames) {
    if (widgetsByName.has(name)) {
      return !isFalseLike(widgetsByName.get(name)?.value);
    }
  }
  return true;
}

function shouldReadLoraSlot(widgetName, widgetsByName) {
  const slot = loraSlotIndex(widgetName);
  if (slot === null) return true;

  const count = Number(widgetsByName.get("lora_count")?.value);
  if (Number.isFinite(count) && Number(slot) > count) return false;

  const normalized = normalizeWidgetName(widgetName);
  const inputMode = String(widgetsByName.get("input_mode")?.value || "")
    .trim()
    .toLowerCase();
  const isTextSelector = normalized === `lora_name_text_${slot}`;
  const isDropdownSelector = normalized === `lora_name_${slot}`;

  if (inputMode === "text" && isDropdownSelector) {
    return !widgetsByName.has(`lora_name_text_${slot}`);
  }
  if (inputMode && inputMode !== "text" && isTextSelector) {
    return !widgetsByName.has(`lora_name_${slot}`);
  }
  return true;
}

export function extractLoraNames(node) {
  if (!node || typeof node !== "object") return [];

  const output = [];
  const descriptor = [
    node.comfyClass,
    node.type,
    node.title,
    node.constructor?.comfyClass,
  ]
    .filter(Boolean)
    .join(" ");
  const isLoraNode = /lora/i.test(descriptor);

  const hasManagerWidget = node.lorasWidget?.value != null;
  if (hasManagerWidget) {
    collectStructuredNames(node.lorasWidget.value, output, true);
  }

  const widgets = node.widgets || [];
  const widgetsByName = new Map(
    widgets.map((widget) => [normalizeWidgetName(widget?.name), widget])
  );

  for (const widget of hasManagerWidget ? [] : widgets) {
    const widgetName = String(widget?.name || "");
    const value = widget?.value;
    const slotEnabled =
      shouldReadLoraSlot(widgetName, widgetsByName) &&
      isLoraSlotEnabled(widgetName, widgetsByName);
    const syntaxNames = slotEnabled ? extractLoraSyntax(value) : [];
    if (syntaxNames.length) output.push(...syntaxNames);

    if (isLoraSelectorName(widgetName)) {
      if (slotEnabled) collectStructuredNames(value, output, true);
    } else if (
      isLoraNode &&
      /^(?:text|lora_syntax|lora_code)$/i.test(widgetName)
    ) {
      output.push(...syntaxNames);
    } else if (
      isLoraNode &&
      typeof value === "string" &&
      WEIGHT_EXTENSION.test(cleanLoraName(value))
    ) {
      collectStructuredNames(value, output);
    }
  }

  const seen = new Set();
  return output.filter((value) => {
    const key = normalizeLoraIdentifier(value);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function getSelectedGraphNodes(canvas) {
  if (!canvas) return [];

  const selectedItems = canvas.selectedItems;
  if (selectedItems && typeof selectedItems.values === "function") {
    return Array.from(selectedItems.values()).filter(
      (item) => item && (item.widgets || item.comfyClass || item.type)
    );
  }

  return Object.values(canvas.selected_nodes || {}).filter(Boolean);
}

function aliasesForModel(model) {
  const fileName = cleanLoraName(model?.file_name || "");
  const modelName = cleanLoraName(model?.model_name || "");
  const folder = String(model?.folder || "")
    .replace(/\\/g, "/")
    .replace(/^\/+|\/+$/g, "");
  const relativePath = folder && fileName ? `${folder}/${fileName}` : fileName;
  const filePath = cleanLoraName(model?.file_path || "");

  return {
    fileName: normalizeLoraIdentifier(fileName),
    modelName: normalizeLoraIdentifier(modelName),
    relativePath: normalizeLoraIdentifier(relativePath),
    filePath: normalizeLoraIdentifier(filePath),
  };
}

function matchScore(query, model) {
  const normalized = normalizeLoraIdentifier(query);
  if (!normalized) return 0;

  const basename = normalized.split("/").pop();
  const hasPath = normalized.includes("/");
  const aliases = aliasesForModel(model);

  if (hasPath) {
    if (aliases.relativePath === normalized) return 100;
    if (
      aliases.filePath === normalized ||
      aliases.filePath.endsWith(`/${normalized}`)
    ) {
      return 95;
    }
    return 0;
  }

  if (aliases.fileName.split("/").pop() === basename) return 90;
  if (aliases.modelName === normalized) return 85;
  return 0;
}

export function matchModelItems(query, items) {
  let bestScore = 0;
  let candidates = [];

  for (const item of Array.isArray(items) ? items : []) {
    const score = matchScore(query, item);
    if (!score) continue;
    if (score > bestScore) {
      bestScore = score;
      candidates = [item];
    } else if (score === bestScore) {
      candidates.push(item);
    }
  }

  return {
    found: candidates.length === 1,
    ambiguous: candidates.length > 1,
    model: candidates.length === 1 ? candidates[0] : null,
    candidates,
  };
}

function exactCivitaiUrl(host, model) {
  const modelId = model?.civitai?.modelId;
  const versionId = model?.civitai?.id;
  if (!modelId) return null;
  const version = versionId
    ? `?modelVersionId=${encodeURIComponent(String(versionId))}`
    : "";
  return `https://${host}/models/${encodeURIComponent(String(modelId))}${version}`;
}

export function buildExternalLinks(query, model = null) {
  const term =
    loraSearchTerm(model?.model_name || query) || loraSearchTerm(query);
  const encodedTerm = encodeURIComponent(term);
  const archiveTerm = String(model?.sha256 || term);

  return {
    civitai:
      exactCivitaiUrl("civitai.com", model) ||
      `https://civitai.com/search/models?query=${encodedTerm}`,
    civitaiRed:
      exactCivitaiUrl("civitai.red", model) ||
      `https://civitai.red/search/models?query=${encodedTerm}`,
    civArchive: `https://civarchive.com/search?q=${encodeURIComponent(archiveTerm)}`,
  };
}
