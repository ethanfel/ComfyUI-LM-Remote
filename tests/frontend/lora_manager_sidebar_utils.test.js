import test from "node:test";
import assert from "node:assert/strict";

import {
  buildExternalLinks,
  closeActiveSidebarTab,
  extractLoraNames,
  getActiveSidebarTabId,
  getSelectedGraphNodes,
  isVideoMedia,
  matchModelItems,
  mergeCivitaiMetadata,
  normalizeLoraIdentifier,
  normalizeMediaSettings,
  normalizeSharedMedia,
  normalizeUsageTips,
} from "../../web/comfyui/lora_manager_sidebar_utils.js";

test("closes only the active LoRA sidebar across ComfyUI API shapes", () => {
  const managed = {
    activeSidebarTabId: "lm-remote-lora-info",
    setActiveSidebarTab(id) {
      this.activeSidebarTabId = id;
    },
  };
  assert.equal(
    closeActiveSidebarTab(managed, "lm-remote-lora-info"),
    true
  );
  assert.equal(getActiveSidebarTabId(managed), null);

  const activeRef = { value: "lm-remote-lora-info" };
  const refManager = { sidebarTab: { activeSidebarTabId: activeRef } };
  assert.equal(
    closeActiveSidebarTab(refManager, "lm-remote-lora-info"),
    true
  );
  assert.equal(activeRef.value, null);

  let otherTabToggleCount = 0;
  const otherTabManager = {
    sidebarTab: {
      activeSidebarTabId: "node-library",
      toggleSidebarTab() {
        otherTabToggleCount += 1;
      },
    },
  };
  assert.equal(
    closeActiveSidebarTab(otherTabManager, "lm-remote-lora-info"),
    false
  );
  assert.equal(otherTabToggleCount, 0);
});

test("falls back to a guarded toggle for readonly sidebar state", () => {
  let activeId = "lm-remote-lora-info";
  let toggleCount = 0;
  const sidebarTab = {
    toggleSidebarTab(tabId) {
      toggleCount += 1;
      activeId = activeId === tabId ? null : tabId;
    },
  };
  Object.defineProperty(sidebarTab, "activeSidebarTabId", {
    get: () => activeId,
  });

  assert.equal(
    closeActiveSidebarTab({ sidebarTab }, "lm-remote-lora-info"),
    true
  );
  assert.equal(activeId, null);
  assert.equal(toggleCount, 1);
});

test("normalizes loader paths and weight extensions", () => {
  assert.equal(
    normalizeLoraIdentifier("Styles\\Portrait.safetensors"),
    "styles/portrait"
  );
});

test("formats Manager usage presets and hides empty JSON", () => {
  assert.deepEqual(normalizeUsageTips("{}"), []);
  assert.deepEqual(
    normalizeUsageTips('{"strength_min":0.7,"clipStrength":1}'),
    [
      { label: "Strength min", value: "0.7" },
      { label: "Clip Strength", value: "1" },
    ]
  );
  assert.deepEqual(normalizeUsageTips("Use at low strength"), [
    { label: "Note", value: "Use at low strength" },
  ]);
});

test("detects direct and encoded video preview URLs", () => {
  assert.equal(
    isVideoMedia(
      "/api/lm/previews?path=%2Fmodels%2Floras%2Fexample.MP4"
    ),
    true
  );
  assert.equal(isVideoMedia("https://example.com/demo.WEBM?download=1"), true);
  assert.equal(isVideoMedia("https://example.com/media/42", "video"), true);
  assert.equal(isVideoMedia("https://example.com/still.png"), false);
  assert.doesNotThrow(() => isVideoMedia("/preview?path=%E0%A4%A"));
});

test("merges full Civitai metadata without losing resolve fields", () => {
  assert.deepEqual(
    mergeCivitaiMetadata(
      { id: 7, modelId: 9, trainedWords: ["portrait"] },
      { description: "Full details", images: [{ id: 1 }] }
    ),
    {
      id: 7,
      modelId: 9,
      trainedWords: ["portrait"],
      description: "Full details",
      images: [{ id: 1 }],
    }
  );
});

test("normalizes community and Civitai media with associated prompts", () => {
  const media = normalizeSharedMedia(
    [
      {
        civitai_image_id: 10,
        preview_url:
          "/api/lm/previews?path=%2Fexamples%2Fcommunity-video.mp4",
        media_type: "video",
        prompt: "community prompt",
        negative_prompt: "community negative",
        username: "artist",
        steps: 20,
        cfg_scale: 4.5,
        width: 1280,
        height: 720,
      },
    ],
    {
      images: [
        {
          id: 10,
          url: "https://example.com/duplicate.mp4",
          type: "video",
          nsfwLevel: 16,
          meta: { prompt: "duplicate prompt" },
        },
        {
          id: 11,
          url: "https://example.com/still.webp",
          meta: {
            meta: {
              prompt: "example prompt",
              negativePrompt: "example negative",
              sampler: "Euler",
              seed: 12,
              Size: "640x480",
              Model: "Example checkpoint",
              clipSkip: 2,
            },
          },
        },
      ],
      customImages: [
        { id: 12, url: "javascript:alert(1)", meta: { prompt: "unsafe" } },
        {
          id: 14,
          url: "https://user:password@example.com/private.webp",
          meta: { prompt: "credential leak" },
        },
        {
          id: 13,
          url: "https://example.com/failed.webp",
          downloadFailed: true,
        },
        {
          id: "local-one",
          url: "",
          meta: { prompt: "local custom prompt" },
        },
      ],
    },
    [
      {
        name: "custom_local-one.webm",
        path: "/example_images_static/abc/custom_local-one.webm",
        is_video: true,
      },
    ]
  );

  assert.equal(media.length, 3);
  assert.deepEqual(
    {
      source: media[0].source,
      mediaType: media[0].mediaType,
      prompt: media[0].prompt,
      negativePrompt: media[0].negativePrompt,
      username: media[0].username,
      steps: media[0].steps,
      cfgScale: media[0].cfgScale,
    },
    {
      source: "Community creation",
      mediaType: "video",
      prompt: "community prompt",
      negativePrompt: "community negative",
      username: "artist",
      steps: 20,
      cfgScale: 4.5,
    }
  );
  assert.equal(media[0].url.includes("/api/lm/previews"), true);
  assert.equal(media[0].nsfwLevel, 16);
  assert.equal(media[1].prompt, "example prompt");
  assert.equal(media[1].negativePrompt, "example negative");
  assert.equal(media[1].sampler, "Euler");
  assert.equal(media[1].seed, 12);
  assert.equal(media[1].width, 640);
  assert.equal(media[1].height, 480);
  assert.equal(media[1].modelName, "Example checkpoint");
  assert.equal(media[1].clipSkip, 2);
  assert.equal(media[2].url, "/example_images_static/abc/custom_local-one.webm");
  assert.equal(media[2].mediaType, "video");
  assert.equal(media[2].prompt, "local custom prompt");
});

test("optimizes Civitai media and normalizes mature-content settings", () => {
  const media = normalizeSharedMedia([], {
    images: [
      {
        id: 1,
        url: "https://image.civitai.com/example/original=true/video.mp4",
        type: "video",
      },
    ],
  });
  assert.equal(media.length, 1);
  assert.match(media[0].url, /transcode=true,width=450,optimized=true/);

  assert.deepEqual(
    normalizeMediaSettings({
      blur_mature_content: false,
      mature_blur_level: "XXX",
      show_only_sfw: true,
    }),
    {
      blurMatureContent: false,
      matureBlurLevel: 16,
      showOnlySfw: true,
    }
  );
});

test("extracts stock and numbered LoRA loader widgets", () => {
  const node = {
    comfyClass: "Power Lora Loader",
    widgets: [
      { name: "lora_name", value: "styles/portrait.safetensors" },
      { name: "lora_01", value: "characters/alice.safetensors" },
      { name: "strength_model", value: 0.8 },
    ],
  };

  assert.deepEqual(extractLoraNames(node), [
    "styles/portrait.safetensors",
    "characters/alice.safetensors",
  ]);
});

test("treats active Manager entries as authoritative over synchronized text", () => {
  const node = {
    comfyClass: "Lora Loader (Remote, LoraManager)",
    lorasWidget: {
      value: [
        { name: "one", active: true },
        { name: "two", active: false },
      ],
    },
    widgets: [{ name: "text", value: "<lora:one:1> <lora:two:0.7>" }],
  };

  assert.deepEqual(extractLoraNames(node), ["one"]);
});

test("extracts LoRA syntax from text loaders without a Manager widget", () => {
  const node = {
    comfyClass: "LoRA Text Loader",
    widgets: [{ name: "text", value: "<lora:one:1> <lora:three:0.7>" }],
  };

  assert.deepEqual(extractLoraNames(node), ["one", "three"]);
});

test("extracts third-party generic selectors and keyed LoRA maps", () => {
  const node = {
    type: "ThirdPartyLoraLoader",
    widgets: [
      { name: "model", value: "styles/four.safetensors" },
      {
        name: "loras",
        value: {
          "five.safetensors": 0.7,
          "disabled.safetensors": false,
        },
      },
    ],
  };

  assert.deepEqual(extractLoraNames(node), [
    "styles/four.safetensors",
    "five.safetensors",
  ]);
});

test("supports dynamic stack widget names and their enable switches", () => {
  const node = {
    type: "LoRAStackDynamic",
    widgets: [
      { name: "input_mode", value: "text" },
      { name: "lora_count", value: 2 },
      { name: "lora_name_1", value: "stale-one.safetensors" },
      { name: "lora_name_text_1", value: "one.safetensors" },
      { name: "enabled_1", value: true },
      { name: "lora_name_2", value: "stale-two.safetensors" },
      { name: "lora_name_text_2", value: "two.safetensors" },
      { name: "enabled_2", value: false },
      { name: "lora_name_text_3", value: "three.safetensors" },
      { name: "enabled_3", value: true },
    ],
  };

  assert.deepEqual(extractLoraNames(node), ["one.safetensors"]);

  node.widgets.find((widget) => widget.name === "input_mode").value = "dropdown";
  node.widgets.find((widget) => widget.name === "lora_count").value = 1;
  assert.deepEqual(extractLoraNames(node), ["stale-one.safetensors"]);
});

test("reads current selectedItems with selected_nodes fallback", () => {
  const selected = { id: 4, type: "LoraLoader", widgets: [] };
  assert.deepEqual(
    getSelectedGraphNodes({ selectedItems: new Set([selected]) }),
    [selected]
  );
  assert.deepEqual(
    getSelectedGraphNodes({ selected_nodes: { 4: selected } }),
    [selected]
  );
});

test("prefers exact relative paths and reports ambiguous basenames", () => {
  const items = [
    {
      file_name: "portrait.safetensors",
      model_name: "Portrait",
      folder: "styles",
      file_path: "/models/loras/styles/portrait.safetensors",
    },
    {
      file_name: "portrait.safetensors",
      model_name: "Portrait Alt",
      folder: "people",
      file_path: "/models/loras/people/portrait.safetensors",
    },
  ];

  const exact = matchModelItems("styles/portrait.safetensors", items);
  assert.equal(exact.found, true);
  assert.equal(exact.model.folder, "styles");

  const ambiguous = matchModelItems("portrait.safetensors", items);
  assert.equal(ambiguous.ambiguous, true);
  assert.equal(ambiguous.candidates.length, 2);

  const absolute = matchModelItems(
    "/models/loras/people/portrait.safetensors",
    items
  );
  assert.equal(absolute.found, true);
  assert.equal(absolute.model.folder, "people");
});

test("builds exact Civitai mirrors and hash-based CivArchive search", () => {
  const links = buildExternalLinks("portrait", {
    model_name: "Portrait",
    sha256: "abc123",
    civitai: { modelId: 42, id: 84 },
  });

  assert.equal(
    links.civitai,
    "https://civitai.com/models/42?modelVersionId=84"
  );
  assert.equal(
    links.civitaiRed,
    "https://civitai.red/models/42?modelVersionId=84"
  );
  assert.equal(links.civArchive, "https://civarchive.com/search?q=abc123");
});

test("builds encoded name searches when the Manager has no card", () => {
  const links = buildExternalLinks("Krea 2 portrait");
  assert.equal(
    links.civitai,
    "https://civitai.com/search/models?query=Krea%202%20portrait"
  );
  assert.equal(
    links.civitaiRed,
    "https://civitai.red/search/models?query=Krea%202%20portrait"
  );
  assert.equal(
    links.civArchive,
    "https://civarchive.com/search?q=Krea%202%20portrait"
  );
});
