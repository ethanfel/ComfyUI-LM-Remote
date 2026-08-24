import { api } from "../../scripts/api.js";

import {
  buildConfigDraft,
  buildConnectionDraft,
  didEffectiveRemoteUrlChange,
  isConfigWritable,
  mappingsToRows,
} from "./remote_config_utils.js";

// Explicit /api paths work with both current and legacy ComfyUI apiURL helpers.
const CONFIG_ENDPOINT = "/api/lm-remote/config";
const TEST_ENDPOINT = "/api/lm-remote/test-connection";

let activeDialog = null;
let pageEffectiveRemoteUrl;

function element(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text != null) value.textContent = String(text);
  return value;
}

function labeledInput(labelText, input) {
  const label = element("label", "lmrc-field");
  label.append(element("span", "lmrc-label", labelText), input);
  return label;
}

function parseError(response, payload) {
  const error = new Error(
    payload?.error || `Request failed with HTTP ${response.status}.`
  );
  error.field = payload?.field || "";
  error.status = response.status;
  error.payload = payload;
  return error;
}

async function requestJson(path, options = {}) {
  const response = await api.fetchApi(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // The HTTP status below still gives the user a useful failure.
  }
  if (!response.ok || payload?.success === false) {
    throw parseError(response, payload);
  }
  return payload;
}

function trapFocus(event, panel) {
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    panel.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((item) => item.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function openRemoteConfigDialog({ onSaved } = {}) {
  if (activeDialog) {
    activeDialog.panel.focus();
    return activeDialog;
  }

  const opener = document.activeElement;

  const overlay = element("div", "lmrc-overlay");
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "lmrc-dialog-title");
  const panel = element("section", "lmrc-dialog");
  panel.tabIndex = -1;
  overlay.appendChild(panel);

  const header = element("header", "lmrc-header");
  const heading = element("div");
  const title = element("h2", "", "Configure LM Remote");
  title.id = "lmrc-dialog-title";
  heading.append(
    title,
    element(
      "p",
      "",
      "Connect this ComfyUI instance to LoRA Manager. New requests use saved changes immediately."
    )
  );
  const closeButton = element("button", "lmrc-icon-button");
  closeButton.type = "button";
  closeButton.title = "Close";
  closeButton.setAttribute("aria-label", "Close configuration");
  closeButton.appendChild(element("i", "pi pi-times"));
  header.append(heading, closeButton);

  const form = element("form", "lmrc-form");
  form.noValidate = true;
  const connectionSection = element("section", "lmrc-section");
  connectionSection.appendChild(element("h3", "", "Connection"));
  const connectionGrid = element("div", "lmrc-connection-grid");

  const remoteUrlInput = element("input", "lmrc-input");
  remoteUrlInput.type = "url";
  remoteUrlInput.placeholder = "http://manager.local:8188";
  remoteUrlInput.autocomplete = "url";
  remoteUrlInput.spellcheck = false;
  const remoteField = labeledInput("Remote URL", remoteUrlInput);
  const remoteHint = element("small", "lmrc-hint");
  remoteField.appendChild(remoteHint);

  const timeoutInput = element("input", "lmrc-input");
  timeoutInput.type = "number";
  timeoutInput.min = "1";
  timeoutInput.max = "300";
  timeoutInput.step = "1";
  const timeoutField = labeledInput("Timeout (seconds)", timeoutInput);
  const timeoutHint = element("small", "lmrc-hint");
  timeoutField.appendChild(timeoutHint);
  connectionGrid.append(remoteField, timeoutField);
  connectionSection.appendChild(connectionGrid);

  const mappingSection = element("section", "lmrc-section");
  const mappingHeading = element("div", "lmrc-section-heading");
  const mappingCopy = element("div");
  mappingCopy.append(
    element("h3", "", "Path mappings"),
    element(
      "p",
      "",
      "Optional remote-to-local prefixes when both machines mount models at different paths."
    )
  );
  const addMappingButton = element("button", "lmrc-button lmrc-secondary", "Add mapping");
  addMappingButton.type = "button";
  mappingHeading.append(mappingCopy, addMappingButton);
  const mappingRows = element("div", "lmrc-mapping-rows");
  mappingSection.append(mappingHeading, mappingRows);

  const notices = element("div", "lmrc-notices");
  const status = element("div", "lmrc-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");

  const footer = element("footer", "lmrc-footer");
  const leftActions = element("div", "lmrc-footer-group");
  const testButton = element("button", "lmrc-button lmrc-secondary", "Test connection");
  testButton.type = "button";
  const reloadButton = element("button", "lmrc-button lmrc-secondary", "Reload saved");
  reloadButton.type = "button";
  leftActions.append(testButton, reloadButton);
  const rightActions = element("div", "lmrc-footer-group");
  const cancelButton = element("button", "lmrc-button lmrc-secondary", "Close");
  cancelButton.type = "button";
  const saveButton = element("button", "lmrc-button lmrc-primary", "Save");
  saveButton.type = "submit";
  rightActions.append(cancelButton, saveButton);
  footer.append(leftActions, rightActions);

  form.append(connectionSection, mappingSection, notices, status, footer);
  panel.append(header, form);
  document.body.appendChild(overlay);

  let loaded = null;
  let busy = false;
  let formLocked = false;
  let dismissLocked = false;

  function updateDisabledState() {
    const overrides = loaded?.overrides || {};
    remoteUrlInput.disabled = formLocked || Boolean(overrides.remote_url);
    timeoutInput.disabled = formLocked || Boolean(overrides.timeout);
    for (const control of mappingRows.querySelectorAll("input, button")) {
      control.disabled = formLocked;
    }
    closeButton.disabled = dismissLocked;
    cancelButton.disabled = dismissLocked;
  }

  function close() {
    if (dismissLocked) return;
    if (activeDialog?.overlay !== overlay) return;
    overlay.remove();
    activeDialog = null;
    if (opener?.isConnected && typeof opener.focus === "function") {
      try {
        opener.focus({ preventScroll: true });
      } catch {
        opener.focus();
      }
    }
  }

  function setStatus(message = "", kind = "", action = null) {
    status.replaceChildren();
    if (message) status.appendChild(element("span", "", message));
    if (action) {
      const actionButton = element(
        "button",
        "lmrc-button lmrc-secondary lmrc-status-action",
        action.label
      );
      actionButton.type = "button";
      actionButton.addEventListener("click", action.onClick);
      status.appendChild(actionButton);
    }
    status.className = `lmrc-status${kind ? ` ${kind}` : ""}`;
  }

  function setBusy(
    value,
    message = "",
    { lockForm = false, lockDismiss = false } = {}
  ) {
    busy = value;
    formLocked = Boolean(value && lockForm);
    dismissLocked = Boolean(value && lockDismiss);
    testButton.disabled = value || !loaded;
    reloadButton.disabled = value;
    saveButton.disabled = value || !loaded || !isConfigWritable(loaded);
    addMappingButton.disabled = value || !loaded;
    form.setAttribute("aria-busy", String(value));
    updateDisabledState();
    if (message) setStatus(message, "busy");
  }

  function addMappingRow(remote = "", local = "") {
    const row = element("div", "lmrc-mapping-row");
    const remoteInput = element("input", "lmrc-input");
    remoteInput.type = "text";
    remoteInput.placeholder = "/data/models/loras";
    remoteInput.value = remote;
    remoteInput.setAttribute("aria-label", "Remote path prefix");
    const arrow = element("i", "pi pi-arrow-right lmrc-mapping-arrow");
    arrow.setAttribute("aria-hidden", "true");
    const localInput = element("input", "lmrc-input");
    localInput.type = "text";
    localInput.placeholder = "/mnt/nas/models/loras";
    localInput.value = local;
    localInput.setAttribute("aria-label", "Local path prefix");
    const remove = element("button", "lmrc-icon-button");
    remove.type = "button";
    remove.title = "Remove path mapping";
    remove.setAttribute("aria-label", remove.title);
    remove.appendChild(element("i", "pi pi-trash"));
    remove.addEventListener("click", () => row.remove());
    row.append(remoteInput, arrow, localInput, remove);
    mappingRows.appendChild(row);
    updateDisabledState();
    return row;
  }

  function readMappingRows() {
    return Array.from(mappingRows.querySelectorAll(".lmrc-mapping-row")).map(
      (row) => {
        const inputs = row.querySelectorAll("input");
        return { remote: inputs[0]?.value || "", local: inputs[1]?.value || "" };
      }
    );
  }

  function renderNotices(payload) {
    notices.replaceChildren();
    const source = payload.storage?.source;
    if (source === "legacy") {
      notices.appendChild(
        element(
          "div",
          "lmrc-notice",
          "Loaded the package config. Saving migrates it to ComfyUI user data."
        )
      );
    } else if (source === "user") {
      notices.appendChild(
        element("div", "lmrc-notice", "Stored in ComfyUI user data.")
      );
    } else if (source === "explicit") {
      notices.appendChild(
        element(
          "div",
          "lmrc-notice",
          "Stored in the file selected by LM_REMOTE_CONFIG."
        )
      );
    }
    if (!isConfigWritable(payload)) {
      notices.appendChild(
        element(
          "div",
          "lmrc-notice warning",
          "Configuration storage is not writable. Fix its file or directory permissions, then use Reload saved to check again."
        )
      );
    }
    for (const warning of payload.warnings || []) {
      notices.appendChild(element("div", "lmrc-notice warning", warning));
    }
  }

  function applyPayload(payload) {
    loaded = payload;
    const configured = payload.configured || {};
    const effective = payload.effective || configured;
    const overrides = payload.overrides || {};
    remoteUrlInput.value = configured.remote_url || "";
    timeoutInput.value = String(configured.timeout ?? 30);
    remoteHint.textContent = overrides.remote_url
      ? `Managed by ${overrides.remote_url}. Effective: ${effective.remote_url}`
      : "The base URL of the standalone LoRA Manager.";
    timeoutHint.textContent = overrides.timeout
      ? `Managed by ${overrides.timeout}. Effective: ${effective.timeout} seconds.`
      : "Used for Manager API and proxy requests.";
    mappingRows.replaceChildren();
    const rows = mappingsToRows(configured.path_mappings);
    rows.forEach((row) => addMappingRow(row.remote, row.local));
    renderNotices(payload);
    setBusy(false);
  }

  async function load() {
    setBusy(true, "Loading configuration…", { lockForm: true });
    try {
      const payload = await requestJson(CONFIG_ENDPOINT);
      if (pageEffectiveRemoteUrl === undefined) {
        pageEffectiveRemoteUrl = String(payload?.effective?.remote_url || "");
      }
      applyPayload(payload);
      setStatus("");
      (form.querySelector("input:not([disabled])") || panel).focus();
    } catch (error) {
      loaded = null;
      setBusy(false);
      setStatus(error.message || "Could not load LM Remote configuration.", "error");
    }
  }

  function showFormError(error) {
    setStatus(error.message, "error");
    const target =
      error.field === "remote_url"
        ? remoteUrlInput
        : error.field === "timeout"
          ? timeoutInput
          : null;
    if (target) {
      queueMicrotask(() => {
        if (target.isConnected && !target.disabled) target.focus();
      });
    }
  }

  addMappingButton.addEventListener("click", () => {
    addMappingRow().querySelector("input")?.focus();
  });
  reloadButton.addEventListener("click", () => {
    if (!busy) load();
  });
  testButton.addEventListener("click", async () => {
    if (busy || !loaded) return;
    try {
      const draft = buildConnectionDraft({
        remoteUrl: remoteUrlInput.value,
        timeout: timeoutInput.value,
        effective: loaded.effective,
        overrides: loaded.overrides,
      });
      setBusy(true, "Testing connection…", { lockForm: true });
      const payload = await requestJson(TEST_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      setStatus(`Connected in ${payload.latency_ms} ms.`, "success");
    } catch (error) {
      showFormError(error);
    } finally {
      setBusy(false);
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !loaded || !isConfigWritable(loaded)) return;
    try {
      const config = buildConfigDraft({
        remoteUrl: remoteUrlInput.value,
        timeout: timeoutInput.value,
        mappingRows: readMappingRows(),
      });
      setBusy(true, "Saving configuration…", {
        lockForm: true,
        lockDismiss: true,
      });
      const payload = await requestJson(CONFIG_ENDPOINT, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: loaded.revision, config }),
      });
      applyPayload(payload);
      if (
        didEffectiveRemoteUrlChange(
          { effective: { remote_url: pageEffectiveRemoteUrl } },
          payload
        )
      ) {
        setStatus(
          "Saved. Reload the ComfyUI page to load Manager assets and reconnect live updates. Save workflow changes first.",
          "success",
          {
            label: "Reload ComfyUI page",
            onClick: () => window.location.reload(),
          }
        );
      } else {
        setStatus(
          "Saved and applied to new requests. No ComfyUI restart required.",
          "success"
        );
      }
      onSaved?.(payload);
    } catch (error) {
      if (error.status === 409 && error.payload?.latest) {
        setStatus(`${error.message} Use Reload saved to continue.`, "error");
      } else {
        showFormError(error);
      }
    } finally {
      setBusy(false);
    }
  });

  closeButton.addEventListener("click", close);
  cancelButton.addEventListener("click", close);
  overlay.addEventListener("mousedown", (event) => {
    if (event.target === overlay) close();
  });
  overlay.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else {
      trapFocus(event, panel);
    }
  });

  activeDialog = { overlay, panel, close };
  panel.focus();
  load();
  return activeDialog;
}
