import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

import {
  buildExternalLinks,
  cleanLoraName,
  extractLoraNames,
  getSelectedGraphNodes,
  loraSearchTerm,
  matchModelItems,
  normalizeLoraIdentifier,
  normalizeUsageTips,
} from "./lora_manager_sidebar_utils.js";

const TAB_ID = "lm-remote-lora-info";
const COMMAND_ID = "LMRemote.OpenLoraInfo";
const AUTO_OPEN_SETTING = "LMRemote.LoraInfo.AutoOpen";
const STYLE_ID = "lm-remote-lora-info-style";
const NODE_SELECTION_HOOK = Symbol.for("lmRemote.loraInfo.nodeSelectionHook");
const CANVAS_SELECTION_HOOK = Symbol.for("lmRemote.loraInfo.canvasSelectionHook");

let sidebarRoot = null;
let selectedNode = null;
let selectedNames = [];
let activeName = "";
let selectionSignature = "";
let lookupState = { status: "idle" };
let lookupGeneration = 0;
let lookupController = null;
let monitorTimer = null;

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = String(text);
  return element;
}

function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const link = document.createElement("link");
  link.id = STYLE_ID;
  link.rel = "stylesheet";
  link.href = new URL("./lora_manager_sidebar.css", import.meta.url).href;
  document.head.appendChild(link);
}

function selectedNodeLabel() {
  if (!selectedNode) return "";
  return (
    selectedNode.title ||
    selectedNode.comfyClass ||
    selectedNode.type ||
    `Node ${selectedNode.id ?? ""}`
  );
}

function makeExternalLink(label, url, extraClass = "") {
  const link = createElement(
    "a",
    `lmri-button lmri-link ${extraClass}`.trim(),
    label
  );
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function managerSearchUrl(name) {
  return `/loras?search=${encodeURIComponent(loraSearchTerm(name) || name)}`;
}

function safePreviewUrl(value) {
  if (!value) return "";
  try {
    const parsed = new URL(String(value), window.location.origin);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.href;
    }
  } catch {
    return "";
  }
  return "";
}

function toDisplayList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }
  if (value && typeof value === "object") {
    return Object.entries(value)
      .filter(([, enabled]) => Boolean(enabled))
      .map(([name]) => name);
  }
  return [];
}

function appendPills(container, values, className = "") {
  const unique = Array.from(new Set(values.filter(Boolean)));
  if (!unique.length) return;
  const pills = createElement("div", `lmri-pills ${className}`.trim());
  unique.forEach((value) => {
    pills.appendChild(createElement("span", "lmri-pill", value));
  });
  container.appendChild(pills);
}

function appendMetaRow(container, label, value) {
  if (value == null || value === "") return;
  const row = createElement("div", "lmri-meta-row");
  row.append(
    createElement("span", "lmri-meta-label", label),
    createElement("span", "lmri-meta-value", value)
  );
  container.appendChild(row);
}

function formatFileSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function appendSearchActions(container, query, model = null) {
  const links = buildExternalLinks(query, model);
  const actions = createElement("div", "lmri-actions");
  actions.append(
    makeExternalLink("Civitai", links.civitai, "lmri-civitai"),
    makeExternalLink("Civitai Red", links.civitaiRed, "lmri-civitai-red"),
    makeExternalLink("CivArchive", links.civArchive, "lmri-archive")
  );
  container.appendChild(actions);
}

function renderEmpty(content) {
  const empty = createElement("div", "lmri-empty");
  empty.append(
    createElement("i", "pi pi-info-circle lmri-empty-icon"),
    createElement("h3", "", "Select a LoRA loader"),
    createElement(
      "p",
      "",
      "Select any node with a LoRA name, LoRA stack, or <lora:name:strength> value."
    )
  );
  content.appendChild(empty);
}

function renderLoading(content) {
  const loading = createElement("div", "lmri-state");
  loading.append(
    createElement("i", "pi pi-spin pi-spinner"),
    createElement("span", "", `Looking up ${activeName}…`)
  );
  content.appendChild(loading);
}

function renderMissing(content) {
  const panel = createElement("section", "lmri-notice");
  panel.append(
    createElement("h3", "", "No LoRA Manager card found"),
    createElement(
      "p",
      "",
      `“${activeName}” is selected, but it is not indexed by the remote LoRA Manager.`
    )
  );
  panel.appendChild(
    makeExternalLink(
      "Search LoRA Manager",
      managerSearchUrl(activeName),
      "lmri-manager"
    )
  );
  appendSearchActions(panel, activeName);
  content.appendChild(panel);
}

function renderError(content) {
  const panel = createElement("section", "lmri-notice lmri-error");
  panel.append(
    createElement("h3", "", "LoRA Manager unavailable"),
    createElement(
      "p",
      "",
      lookupState.message || "The remote Manager did not answer this lookup."
    )
  );
  const retry = createElement("button", "lmri-button", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => lookupActiveName());
  panel.appendChild(retry);
  appendSearchActions(panel, activeName);
  content.appendChild(panel);
}

function useResolvedCandidate(model) {
  lookupGeneration += 1;
  lookupController?.abort();
  lookupState = { status: "found", model };
  renderSidebar();
}

function renderAmbiguous(content) {
  const panel = createElement("section", "lmri-notice");
  panel.append(
    createElement("h3", "", "Choose the matching LoRA"),
    createElement(
      "p",
      "",
      "More than one Manager card has this filename. Pick the folder used by the node."
    )
  );

  const candidates = createElement("div", "lmri-candidates");
  (lookupState.candidates || []).forEach((model) => {
    const button = createElement("button", "lmri-candidate");
    button.type = "button";
    const title = model.model_name || model.file_name || "Unnamed LoRA";
    const path = [model.folder, model.file_name].filter(Boolean).join("/");
    button.append(
      createElement("strong", "", title),
      createElement("span", "", path || model.file_path || "")
    );
    button.addEventListener("click", () => useResolvedCandidate(model));
    candidates.appendChild(button);
  });
  panel.appendChild(candidates);
  appendSearchActions(panel, activeName);
  content.appendChild(panel);
}

function renderModelCard(content, model) {
  const card = createElement("article", "lmri-card");
  const previewUrl = safePreviewUrl(model.preview_url);
  if (previewUrl) {
    const preview = createElement("div", "lmri-preview");
    const image = document.createElement("img");
    image.src = previewUrl;
    image.alt = `Preview for ${model.model_name || activeName}`;
    image.loading = "lazy";
    image.addEventListener("error", () => preview.remove());
    preview.appendChild(image);
    card.appendChild(preview);
  }

  const body = createElement("div", "lmri-card-body");
  const heading = createElement("div", "lmri-card-heading");
  const titleGroup = createElement("div", "");
  titleGroup.append(
    createElement("h2", "", model.model_name || model.file_name || activeName),
    createElement(
      "p",
      "lmri-file-name",
      [model.folder, model.file_name].filter(Boolean).join("/") ||
        model.file_path ||
        activeName
    )
  );
  heading.appendChild(titleGroup);

  const flags = createElement("div", "lmri-flags");
  if (model.favorite) flags.appendChild(createElement("span", "", "★"));
  if (model.update_available) {
    flags.appendChild(createElement("span", "lmri-update", "Update"));
  }
  if (flags.childNodes.length) heading.appendChild(flags);
  body.appendChild(heading);

  const metadata = createElement("div", "lmri-metadata");
  appendMetaRow(metadata, "Base model", model.base_model);
  appendMetaRow(metadata, "Type", model.sub_type);
  appendMetaRow(metadata, "Version", model.civitai?.name);
  appendMetaRow(metadata, "Size", formatFileSize(model.file_size));
  appendMetaRow(
    metadata,
    "Used",
    Number.isFinite(Number(model.usage_count))
      ? `${Number(model.usage_count)} times`
      : ""
  );
  if (model.sha256) {
    const hash = String(model.sha256);
    appendMetaRow(metadata, "SHA256", hash.length > 16 ? `${hash.slice(0, 16)}…` : hash);
    metadata.lastElementChild?.querySelector(".lmri-meta-value")?.setAttribute(
      "title",
      hash
    );
  }
  body.appendChild(metadata);

  const trainedWords = toDisplayList(model.civitai?.trainedWords);
  if (trainedWords.length) {
    body.appendChild(createElement("h3", "lmri-section-title", "Trigger words"));
    appendPills(body, trainedWords, "lmri-triggers");
  }

  const tags = [
    ...toDisplayList(model.tags),
    ...toDisplayList(model.auto_tags),
  ];
  if (tags.length) {
    body.appendChild(createElement("h3", "lmri-section-title", "Tags"));
    appendPills(body, tags);
  }

  const usageTips = normalizeUsageTips(model.usage_tips);
  if (usageTips.length) {
    const section = createElement("section", "lmri-copy");
    section.appendChild(createElement("h3", "", "Usage tips"));
    const values = createElement("div", "lmri-metadata lmri-usage-tips");
    usageTips.forEach((tip) => appendMetaRow(values, tip.label, tip.value));
    section.appendChild(values);
    body.appendChild(section);
  }
  if (model.notes) {
    const section = createElement("section", "lmri-copy");
    section.append(
      createElement("h3", "", "Manager notes"),
      createElement("p", "", model.notes)
    );
    body.appendChild(section);
  }

  const actions = createElement("div", "lmri-actions lmri-primary-actions");
  actions.appendChild(
    makeExternalLink(
      "Open Manager card",
      managerSearchUrl(model.file_name || activeName),
      "lmri-manager"
    )
  );
  body.appendChild(actions);
  appendSearchActions(body, activeName, model);
  card.appendChild(body);
  content.appendChild(card);
}

function renderSidebar() {
  if (!sidebarRoot) return;
  sidebarRoot.replaceChildren();

  const header = createElement("header", "lmri-header");
  const heading = createElement("div", "");
  heading.append(
    createElement("h1", "", "LoRA Info"),
    createElement("p", "", selectedNodeLabel() || "Selected node")
  );
  const refresh = createElement("button", "lmri-icon-button");
  refresh.type = "button";
  refresh.title = "Refresh LoRA Manager card";
  refresh.setAttribute("aria-label", refresh.title);
  refresh.appendChild(createElement("i", "pi pi-refresh"));
  refresh.disabled = !activeName || lookupState.status === "loading";
  refresh.addEventListener("click", () => lookupActiveName());
  header.append(heading, refresh);
  sidebarRoot.appendChild(header);

  if (selectedNames.length > 1) {
    const selector = createElement("div", "lmri-name-selector");
    selectedNames.forEach((name) => {
      const button = createElement(
        "button",
        normalizeLoraIdentifier(name) === normalizeLoraIdentifier(activeName)
          ? "lmri-name active"
          : "lmri-name",
        loraSearchTerm(name) || name
      );
      button.type = "button";
      button.title = name;
      button.addEventListener("click", () => {
        if (normalizeLoraIdentifier(name) === normalizeLoraIdentifier(activeName)) {
          return;
        }
        activeName = name;
        lookupActiveName();
      });
      selector.appendChild(button);
    });
    sidebarRoot.appendChild(selector);
  }

  const content = createElement("main", "lmri-content");
  sidebarRoot.appendChild(content);
  if (!activeName) {
    renderEmpty(content);
  } else if (lookupState.status === "loading") {
    renderLoading(content);
  } else if (lookupState.status === "found") {
    renderModelCard(content, lookupState.model);
  } else if (lookupState.status === "ambiguous") {
    renderAmbiguous(content);
  } else if (lookupState.status === "missing") {
    renderMissing(content);
  } else if (lookupState.status === "error") {
    renderError(content);
  } else {
    renderLoading(content);
  }
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return payload.error || `Request failed with HTTP ${response.status}`;
  } catch {
    return `Request failed with HTTP ${response.status}`;
  }
}

async function fallbackListLookup(name, signal) {
  const term = loraSearchTerm(name);
  const params = new URLSearchParams({
    page: "1",
    page_size: "100",
    search: term,
    fuzzy_search: "true",
  });
  const response = await api.fetchApi(`/lm/loras/list?${params}`, { signal });
  if (!response.ok) throw new Error(await responseError(response));
  const payload = await response.json();
  const result = matchModelItems(name, payload.items);
  return {
    success: true,
    query: name,
    ...result,
  };
}

async function resolveManagerCard(name, signal) {
  const response = await api.fetchApi(
    `/lm/loras/resolve?name=${encodeURIComponent(name)}`,
    { signal }
  );
  if (response.status === 404) {
    return fallbackListLookup(name, signal);
  }
  if (!response.ok) throw new Error(await responseError(response));
  const result = await response.json();
  if (result?.success && !result.found && !result.ambiguous) {
    try {
      return await fallbackListLookup(name, signal);
    } catch (error) {
      if (error?.name === "AbortError") throw error;
    }
  }
  return result;
}

async function lookupActiveName() {
  const name = cleanLoraName(activeName);
  if (!name) return;

  const generation = ++lookupGeneration;
  lookupController?.abort();
  lookupController = new AbortController();
  lookupState = { status: "loading" };
  renderSidebar();

  try {
    const result = await resolveManagerCard(name, lookupController.signal);
    if (generation !== lookupGeneration) return;

    if (!result?.success) {
      throw new Error(result?.error || "The Manager lookup failed.");
    }
    if (result.found && result.model) {
      lookupState = { status: "found", model: result.model };
    } else if (result.ambiguous && result.candidates?.length) {
      lookupState = {
        status: "ambiguous",
        candidates: result.candidates,
      };
    } else {
      lookupState = { status: "missing" };
    }
  } catch (error) {
    if (error?.name === "AbortError" || generation !== lookupGeneration) return;
    lookupState = {
      status: "error",
      message: error instanceof Error ? error.message : String(error),
    };
  }
  renderSidebar();
}

function autoOpenEnabled() {
  return app.extensionManager?.setting?.get?.(AUTO_OPEN_SETTING) !== false;
}

function unwrapValue(value) {
  return value && typeof value === "object" && "value" in value
    ? value.value
    : value;
}

function activeSidebarId(manager, sidebar) {
  return unwrapValue(
    sidebar?.activeSidebarTabId ?? manager?.activeSidebarTabId
  );
}

function openSidebarTab() {
  const manager = app.extensionManager;
  if (!manager) return false;
  const sidebar = manager.sidebarTab || manager;
  if (activeSidebarId(manager, sidebar) === TAB_ID) return true;

  if (typeof manager.setActiveSidebarTab === "function") {
    manager.setActiveSidebarTab(TAB_ID);
    if (activeSidebarId(manager, sidebar) === TAB_ID) return true;
  }

  if (sidebar && "activeSidebarTabId" in sidebar) {
    try {
      const current = sidebar.activeSidebarTabId;
      if (current && typeof current === "object" && "value" in current) {
        current.value = TAB_ID;
      } else {
        sidebar.activeSidebarTabId = TAB_ID;
      }
    } catch {
      // Some frontend versions expose a readonly store property.
    }
    if (activeSidebarId(manager, sidebar) === TAB_ID) return true;
  }

  if (typeof sidebar?.toggleSidebarTab === "function") {
    sidebar.toggleSidebarTab(TAB_ID);
    return true;
  }
  if (typeof manager.toggleSidebarTab === "function") {
    manager.toggleSidebarTab(TAB_ID);
    return true;
  }
  if (typeof manager.command?.execute === "function") {
    manager.command.execute(`Workspace.ToggleSidebarTab.${TAB_ID}`);
    return true;
  }
  return false;
}

function updateSelection({ autoOpen = false, force = false } = {}) {
  const nodes = getSelectedGraphNodes(app.canvas);
  const node = nodes.length === 1 ? nodes[0] : null;
  const names = node ? extractLoraNames(node) : [];
  const signature = `${node?.id ?? ""}|${names
    .map(normalizeLoraIdentifier)
    .join("|")}`;

  if (!force && signature === selectionSignature) {
    if (autoOpen && names.length && autoOpenEnabled()) openSidebarTab();
    return;
  }

  selectionSignature = signature;
  selectedNode = node;
  selectedNames = names;
  const currentStillExists = names.some(
    (name) =>
      normalizeLoraIdentifier(name) === normalizeLoraIdentifier(activeName)
  );
  activeName = currentStillExists ? activeName : names[0] || "";

  lookupGeneration += 1;
  lookupController?.abort();
  lookupState = { status: activeName ? "loading" : "idle" };
  renderSidebar();

  if (activeName) {
    if (autoOpen && autoOpenEnabled()) openSidebarTab();
    lookupActiveName();
  }
}

function chainCanvasSelection() {
  const canvas = app.canvas;
  if (!canvas || canvas[CANVAS_SELECTION_HOOK]) return;
  canvas[CANVAS_SELECTION_HOOK] = true;

  const previous = canvas.onSelectionChange;
  canvas.onSelectionChange = function (...args) {
    const result =
      typeof previous === "function" ? previous.apply(this, args) : undefined;
    queueMicrotask(() => updateSelection({ autoOpen: true }));
    return result;
  };
}

function chainNodeSelection(nodeType) {
  const prototype = nodeType?.prototype;
  if (!prototype || prototype[NODE_SELECTION_HOOK]) return;
  prototype[NODE_SELECTION_HOOK] = true;

  const previous = prototype.onSelected;
  prototype.onSelected = function (...args) {
    const result =
      typeof previous === "function" ? previous.apply(this, args) : undefined;
    queueMicrotask(() => updateSelection({ autoOpen: true }));
    return result;
  };
}

function registerSidebarTab() {
  const manager = app.extensionManager;
  const sidebar = manager?.sidebarTab;
  const tabs =
    sidebar?.sidebarTabs?.value ??
    sidebar?.sidebarTabs ??
    manager?.getSidebarTabs?.() ??
    [];
  if (Array.isArray(tabs) && tabs.some((tab) => tab.id === TAB_ID)) return;

  const specification = {
    id: TAB_ID,
    icon: "pi pi-id-card",
    title: "LoRA Info",
    tooltip: "LoRA Manager card and Civitai links for the selected loader",
    type: "custom",
    render(container) {
      ensureStyles();
      container.style.height = "100%";
      container.style.minHeight = "0";
      sidebarRoot = createElement("div", "lmri-root");
      container.replaceChildren(sidebarRoot);
      renderSidebar();
      updateSelection({ force: true });
    },
    destroy() {
      lookupGeneration += 1;
      lookupController?.abort();
      sidebarRoot?.remove();
      sidebarRoot = null;
      lookupState = { status: "idle" };
    },
  };

  if (typeof manager?.registerSidebarTab === "function") {
    manager.registerSidebarTab(specification);
  } else if (typeof sidebar?.registerSidebarTab === "function") {
    sidebar.registerSidebarTab(specification);
  } else {
    console.error(
      "[LM-Remote] This ComfyUI frontend does not support custom sidebar tabs."
    );
  }
}

app.registerExtension({
  name: "LoraManager.RemoteLoraInfoSidebar",
  settings: [
    {
      id: AUTO_OPEN_SETTING,
      name: "Open LoRA Info when selecting a LoRA loader",
      type: "boolean",
      defaultValue: true,
      category: ["LM Remote", "LoRA Info", "Auto-open"],
    },
  ],
  commands: [
    {
      id: COMMAND_ID,
      label: "Open LoRA Info",
      icon: "pi pi-id-card",
      function: () => {
        updateSelection({ force: true });
        openSidebarTab();
      },
    },
  ],
  getSelectionToolboxCommands(selectedItem) {
    return extractLoraNames(selectedItem).length ? [COMMAND_ID] : [];
  },
  beforeRegisterNodeDef(nodeType) {
    chainNodeSelection(nodeType);
  },
  setup() {
    ensureStyles();
    registerSidebarTab();
    chainCanvasSelection();
    updateSelection();
    if (monitorTimer == null) {
      monitorTimer = window.setInterval(() => {
        chainCanvasSelection();
        updateSelection();
      }, 500);
    }
  },
});
