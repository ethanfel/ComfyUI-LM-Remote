/**
 * JS shim for Remote Lora Stacker node.
 */
import { app } from "../../scripts/app.js";
import {
  getActiveLorasFromNode,
  updateConnectedTriggerWords,
  updateDownstreamLoaders,
  chainCallback,
  mergeLoras,
} from "/extensions/ComfyUI-Lora-Manager/utils.js";
import { addLorasWidget } from "/extensions/ComfyUI-Lora-Manager/loras_widget.js";
import { applyLoraValuesToText, debounce } from "/extensions/ComfyUI-Lora-Manager/lora_syntax_utils.js";
import { applySelectionHighlight } from "/extensions/ComfyUI-Lora-Manager/trigger_word_highlight.js";

app.registerExtension({
  name: "LoraManager.LoraStackerRemote",

  async beforeRegisterNodeDef(nodeType, nodeData, app) {
    if (nodeType.comfyClass === "Lora Stacker (Remote, LoraManager)") {
      chainCallback(nodeType.prototype, "onNodeCreated", async function () {
        this.serialize_widgets = true;
        this.addInput("lora_stack", "LORA_STACK", { shape: 7 });

        let isUpdating = false;
        let isSyncingInput = false;

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

        const result = addLorasWidget(
          this, "loras",
          { onSelectionChange: (selection) => applySelectionHighlight(this, selection) },
          (value) => {
            if (isUpdating) return;
            isUpdating = true;
            try {
              const isNodeActive = this.mode === undefined || this.mode === 0 || this.mode === 3;
              const activeLoraNames = new Set();
              if (isNodeActive) {
                value.forEach((lora) => { if (lora.active) activeLoraNames.add(lora.name); });
              }
              updateConnectedTriggerWords(this, activeLoraNames);
              updateDownstreamLoaders(this);
            } finally {
              isUpdating = false;
            }
            scheduleInputSync(value);
          }
        );

        this.lorasWidget = result.widget;

        inputWidget.callback = (value) => {
          if (isUpdating) return;
          isUpdating = true;
          try {
            const currentLoras = this.lorasWidget?.value || [];
            const mergedLoras = mergeLoras(value, currentLoras);
            if (this.lorasWidget) this.lorasWidget.value = mergedLoras;
            const isNodeActive = this.mode === undefined || this.mode === 0 || this.mode === 3;
            const activeLoraNames = isNodeActive ? getActiveLorasFromNode(this) : new Set();
            updateConnectedTriggerWords(this, activeLoraNames);
            updateDownstreamLoaders(this);
          } finally {
            isUpdating = false;
          }
        };
      });
    }
  },

  async loadedGraphNode(node) {
    if (node.comfyClass === "Lora Stacker (Remote, LoraManager)") {
      let existingLoras = [];
      if (node.widgets_values && node.widgets_values.length > 0) {
        existingLoras = node.widgets_values[1] || [];
      }
      const inputWidget = node.inputWidget || node.widgets[0];
      const mergedLoras = mergeLoras(inputWidget.value, existingLoras);
      node.lorasWidget.value = mergedLoras;
    }
  },
});
