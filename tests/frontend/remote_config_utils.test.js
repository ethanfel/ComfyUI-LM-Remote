import assert from "node:assert/strict";
import test from "node:test";

import {
  RemoteConfigFormError,
  buildConfigDraft,
  buildConnectionDraft,
  didEffectiveRemoteUrlChange,
  isConfigWritable,
  mappingsToRows,
} from "../../web/comfyui/remote_config_utils.js";

test("buildConfigDraft normalizes form values and skips blank mappings", () => {
  assert.deepEqual(
    buildConfigDraft({
      remoteUrl: "  http://manager.local:8188/  ",
      timeout: "45",
      mappingRows: [
        { remote: "/data/loras/", local: "/mnt/loras" },
        { remote: "", local: "" },
      ],
    }),
    {
      remote_url: "http://manager.local:8188",
      timeout: 45,
      path_mappings: { "/data/loras": "/mnt/loras" },
    }
  );
});

test("buildConfigDraft allows an empty URL to disable remote mode", () => {
  const draft = buildConfigDraft({
    remoteUrl: "",
    timeout: 30,
    mappingRows: [],
  });
  assert.equal(draft.remote_url, "");
});

test("buildConfigDraft rejects unsafe URLs and invalid mappings", () => {
  assert.throws(
    () =>
      buildConfigDraft({
        remoteUrl: "ftp://manager.local",
        timeout: 30,
        mappingRows: [],
      }),
    (error) =>
      error instanceof RemoteConfigFormError && error.field === "remote_url"
  );
  assert.throws(
    () =>
      buildConfigDraft({
        remoteUrl: "http://manager.local",
        timeout: 30,
        mappingRows: [{ remote: "/remote", local: "" }],
      }),
    (error) =>
      error instanceof RemoteConfigFormError && error.field === "path_mappings"
  );
  assert.throws(
    () =>
      buildConfigDraft({
        remoteUrl: "http://manager.local",
        timeout: 301,
        mappingRows: [],
      }),
    (error) =>
      error instanceof RemoteConfigFormError && error.field === "timeout"
  );
});

test("buildConfigDraft rejects normalized duplicate path prefixes", () => {
  assert.throws(
    () =>
      buildConfigDraft({
        remoteUrl: "http://manager.local",
        timeout: 30,
        mappingRows: [
          { remote: "/data/loras", local: "/mnt/a" },
          { remote: "\\data\\loras\\", local: "/mnt/b" },
        ],
      }),
    /Duplicate remote path prefix/
  );
});

test("buildConfigDraft preserves __proto__ as a path mapping key", () => {
  const draft = buildConfigDraft({
    remoteUrl: "http://manager.local",
    timeout: 30,
    mappingRows: [{ remote: "__proto__", local: "/mnt/prototype" }],
  });

  assert.equal(Object.hasOwn(draft.path_mappings, "__proto__"), true);
  assert.equal(draft.path_mappings.__proto__, "/mnt/prototype");
  assert.equal(
    JSON.parse(JSON.stringify(draft.path_mappings)).__proto__,
    "/mnt/prototype"
  );
});

test("connection tests use effective environment-managed values", () => {
  assert.deepEqual(
    buildConnectionDraft({
      remoteUrl: "http://stored.local:8188",
      timeout: "30",
      effective: {
        remote_url: "https://managed.local:443",
        timeout: 12,
      },
      overrides: {
        remote_url: "LM_REMOTE_URL",
        timeout: "LM_REMOTE_TIMEOUT",
      },
    }),
    { remote_url: "https://managed.local:443", timeout: 12 }
  );
});

test("mappingsToRows creates editable row values", () => {
  assert.deepEqual(mappingsToRows({ "/remote": "/local" }), [
    { remote: "/remote", local: "/local" },
  ]);
});

test("didEffectiveRemoteUrlChange only tracks the effective URL", () => {
  assert.equal(
    didEffectiveRemoteUrlChange(
      { effective: { remote_url: "http://old:8188", timeout: 30 } },
      { effective: { remote_url: "http://new:8188", timeout: 30 } }
    ),
    true
  );
  assert.equal(
    didEffectiveRemoteUrlChange(
      { effective: { remote_url: "http://same:8188", timeout: 30 } },
      { effective: { remote_url: "http://same:8188", timeout: 60 } }
    ),
    false
  );
});

test("isConfigWritable only disables saving for an explicit false value", () => {
  assert.equal(isConfigWritable({ storage: { writable: false } }), false);
  assert.equal(isConfigWritable({ storage: { writable: true } }), true);
  assert.equal(isConfigWritable({ storage: {} }), true);
  assert.equal(isConfigWritable(null), true);
});
