export class RemoteConfigFormError extends Error {
  constructor(field, message) {
    super(message);
    this.name = "RemoteConfigFormError";
    this.field = field;
  }
}

function normalizeTimeout(value) {
  const text = String(value ?? "").trim();
  if (!/^\d+$/.test(text)) {
    throw new RemoteConfigFormError("timeout", "Timeout must be a whole number.");
  }
  const timeout = Number(text);
  if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > 300) {
    throw new RemoteConfigFormError(
      "timeout",
      "Timeout must be between 1 and 300 seconds."
    );
  }
  return timeout;
}

function normalizeRemoteUrl(value, allowEmpty) {
  const remoteUrl = String(value ?? "").trim();
  if (!remoteUrl) {
    if (allowEmpty) return "";
    throw new RemoteConfigFormError(
      "remote_url",
      "Enter a remote LoRA Manager URL."
    );
  }

  let parsed;
  try {
    parsed = new URL(remoteUrl);
  } catch {
    throw new RemoteConfigFormError("remote_url", "Enter a valid remote URL.");
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new RemoteConfigFormError(
      "remote_url",
      "Remote URL must use http:// or https://."
    );
  }
  if (!parsed.hostname) {
    throw new RemoteConfigFormError(
      "remote_url",
      "Remote URL must include a host name."
    );
  }
  if (parsed.username || parsed.password) {
    throw new RemoteConfigFormError(
      "remote_url",
      "Credentials are not allowed in the remote URL."
    );
  }
  if (parsed.search || parsed.hash) {
    throw new RemoteConfigFormError(
      "remote_url",
      "Remote URL cannot contain a query or fragment."
    );
  }
  return remoteUrl.replace(/\/+$/, "");
}

function buildMappings(rows) {
  const pathMappings = new Map();
  for (const row of rows || []) {
    const remote = String(row?.remote ?? "").trim();
    const local = String(row?.local ?? "").trim();
    if (!remote && !local) continue;
    if (!remote || !local) {
      throw new RemoteConfigFormError(
        "path_mappings",
        "Each path mapping needs both a remote and local path."
      );
    }
    const normalizedRemote = remote.replace(/\\/g, "/").replace(/\/+$/, "") || "/";
    if (pathMappings.has(normalizedRemote)) {
      throw new RemoteConfigFormError(
        "path_mappings",
        `Duplicate remote path prefix: ${normalizedRemote}`
      );
    }
    pathMappings.set(normalizedRemote, local);
  }
  return Object.fromEntries(pathMappings);
}

export function mappingsToRows(pathMappings) {
  if (!pathMappings || typeof pathMappings !== "object") return [];
  return Object.entries(pathMappings).map(([remote, local]) => ({
    remote: String(remote),
    local: String(local),
  }));
}

export function buildConfigDraft({ remoteUrl, timeout, mappingRows }) {
  return {
    remote_url: normalizeRemoteUrl(remoteUrl, true),
    timeout: normalizeTimeout(timeout),
    path_mappings: buildMappings(mappingRows),
  };
}

export function buildConnectionDraft({
  remoteUrl,
  timeout,
  effective,
  overrides,
}) {
  return {
    remote_url: normalizeRemoteUrl(
      overrides?.remote_url ? effective?.remote_url : remoteUrl,
      false
    ),
    timeout: normalizeTimeout(
      overrides?.timeout ? effective?.timeout : timeout
    ),
  };
}

export function didEffectiveRemoteUrlChange(previousPayload, nextPayload) {
  const previous = String(previousPayload?.effective?.remote_url || "");
  const next = String(nextPayload?.effective?.remote_url || "");
  return previous !== next;
}

export function isConfigWritable(payload) {
  return payload?.storage?.writable !== false;
}
