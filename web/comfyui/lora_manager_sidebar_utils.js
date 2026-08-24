const WEIGHT_EXTENSION = /\.(?:safetensors|ckpt|pt|pth|bin)$/i;
const LORA_SYNTAX = /<lora:([^:>]+)(?::[^>]*)?>/gi;
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
