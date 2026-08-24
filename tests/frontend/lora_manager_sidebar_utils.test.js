import test from "node:test";
import assert from "node:assert/strict";

import {
  buildExternalLinks,
  extractLoraNames,
  getSelectedGraphNodes,
  matchModelItems,
  normalizeLoraIdentifier,
  normalizeUsageTips,
} from "../../web/comfyui/lora_manager_sidebar_utils.js";

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
