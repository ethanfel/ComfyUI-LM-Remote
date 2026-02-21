/**
 * JS shim for Remote Lora Loader / Remote Lora Text Loader nodes.
 *
 * Re-uses all widget infrastructure from the original ComfyUI-Lora-Manager;
 * the only difference is matching on the remote node NAMEs.
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
  collectActiveLorasFromChain,
  updateConnectedTriggerWords,
  chainCallback,
  mergeLoras,
  getAllGraphNodes,
  getNodeFromGraph,
} from "/extensions/ComfyUI-Lora-Manager/utils.js";
import { addLorasWidget } from "/extensions/ComfyUI-Lora-Manager/loras_widget.js";
import { applyLoraValuesToText, debounce } from "/extensions/ComfyUI-Lora-Manager/lora_syntax_utils.js";
import { applySelectionHighlight } from "/extensions/ComfyUI-Lora-Manager/trigger_word_highlight.js";

app.registerExtension({
  name: "LoraManager.LoraLoaderRemote",

  setup() {
    api.addEventListener("lora_code_update", (event) => {
      this.handleLoraCodeUpdate(event.detail || {});
    });
  },

  handleLoraCodeUpdate(message) {
    const nodeId = message?.node_id ?? message?.id;
    const graphId = message?.graph_id;
    const loraCode = message?.lora_code ?? "";
    const mode = message?.mode ?? "append";
    const numericNodeId = typeof nodeId === "string" ? Number(nodeId) : nodeId;

    if (numericNodeId === -1) {
      const loraLoaderNodes = getAllGraphNodes(app.graph)
        .map(({ node }) => node)
        .filter((node) => node?.comfyClass === "Lora Loader (Remote, LoraManager)");

      if (loraLoaderNodes.length > 0) {
        loraLoaderNodes.forEach((node) => {
          this.updateNodeLoraCode(node, loraCode, mode);
        });
      }
      return;
    }

    const node = getNodeFromGraph(graphId, numericNodeId);
    if (
      !node ||
      (node.comfyClass !== "Lora Loader (Remote, LoraManager)" &&
        node.comfyClass !== "Lora Stacker (Remote, LoraManager)" &&
        node.comfyClass !== "WanVideo Lora Select (Remote, LoraManager)")
    ) {
      return;
    }
    this.updateNodeLoraCode(node, loraCode, mode);
  },

  updateNodeLoraCode(node, loraCode, mode) {
    const inputWidget = node.inputWidget;
    if (!inputWidget) return;

    const currentValue = inputWidget.value || "";
    if (mode === "replace") {
      inputWidget.value = loraCode;
    } else {
      inputWidget.value = currentValue.trim()
        ? `${currentValue.trim()} ${loraCode}`
        : loraCode;
    }

    if (typeof inputWidget.callback === "function") {
      inputWidget.callback(inputWidget.value);
    }
  },

  async beforeRegisterNodeDef(nodeType, nodeData, app) {
    if (nodeType.comfyClass === "Lora Loader (Remote, LoraManager)") {
      chainCallback(nodeType.prototype, "onNodeCreated", function () {
        this.serialize_widgets = true;

        this.addInput("clip", "CLIP", { shape: 7 });
        this.addInput("lora_stack", "LORA_STACK", { shape: 7 });

        let isUpdating = false;
        let isSyncingInput = false;

        const self = this;
        let _mode = this.mode;
        Object.defineProperty(this, "mode", {
          get() { return _mode; },
          set(value) {
            const oldValue = _mode;
            _mode = value;
            if (self.onModeChange) self.onModeChange(value, oldValue);
          },
        });

        this.onModeChange = function (newMode) {
          const allActiveLoraNames = collectActiveLorasFromChain(self);
          updateConnectedTriggerWords(self, allActiveLoraNames);
        };

        const inputWidget = this.widgets[0];
        this.inputWidget = inputWidget;

        const scheduleInputSync = debounce((lorasValue) => {
          if (isSyncingInput) return;
          isSyncingInput = true;
          isUpdating = true;
          try {
            const nextText = applyLoraValuesToText(inputWidget.value, lorasValue);
            if (inputWidget.value !== nextText) inputWidget.value = nextText;
          } finally {
            isUpdating = false;
            isSyncingInput = false;
          }
        });

        this.lorasWidget = addLorasWidget(
          this, "loras",
          { onSelectionChange: (selection) => applySelectionHighlight(this, selection) },
          (value) => {
            if (isUpdating) return;
            isUpdating = true;
            try {
              const allActiveLoraNames = collectActiveLorasFromChain(this);
              updateConnectedTriggerWords(this, allActiveLoraNames);
            } finally {
              isUpdating = false;
            }
            scheduleInputSync(value);
          }
        ).widget;

        inputWidget.callback = (value) => {
          if (isUpdating) return;
          isUpdating = true;
          try {
            const currentLoras = this.lorasWidget.value || [];
            const mergedLoras = mergeLoras(value, currentLoras);
            this.lorasWidget.value = mergedLoras;
            const allActiveLoraNames = collectActiveLorasFromChain(this);
            updateConnectedTriggerWords(this, allActiveLoraNames);
          } finally {
            isUpdating = false;
          }
        };
      });
    }
  },

  async loadedGraphNode(node) {
    if (node.comfyClass === "Lora Loader (Remote, LoraManager)") {
      let existingLoras = [];
      if (node.widgets_values && node.widgets_values.length > 0) {
        existingLoras = node.widgets_values[1] || [];
      }
      const mergedLoras = mergeLoras(node.widgets[0].value, existingLoras);
      node.lorasWidget.value = mergedLoras;
    }
  },
});
