/**
 * JS shim for Remote WanVideo Lora Select node.
 */
import { app } from "../../scripts/app.js";
import {
  getActiveLorasFromNode,
  updateConnectedTriggerWords,
  chainCallback,
  mergeLoras,
} from "/extensions/ComfyUI-Lora-Manager/utils.js";
import { addLorasWidget } from "/extensions/ComfyUI-Lora-Manager/loras_widget.js";
import { applyLoraValuesToText, debounce } from "/extensions/ComfyUI-Lora-Manager/lora_syntax_utils.js";

app.registerExtension({
  name: "LoraManager.WanVideoLoraSelectRemote",

  async beforeRegisterNodeDef(nodeType, nodeData, app) {
    if (nodeType.comfyClass === "WanVideo Lora Select (Remote, LoraManager)") {
      chainCallback(nodeType.prototype, "onNodeCreated", async function () {
        this.serialize_widgets = true;

        this.addInput("prev_lora", "WANVIDLORA", { shape: 7 });
        this.addInput("blocks", "SELECTEDBLOCKS", { shape: 7 });

        let isUpdating = false;
        let isSyncingInput = false;

        // text widget is at index 2 (after low_mem_load, merge_loras)
        const inputWidget = this.widgets[2];
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

        const result = addLorasWidget(this, "loras", {}, (value) => {
          if (isUpdating) return;
          isUpdating = true;
          try {
            const activeLoraNames = new Set();
            value.forEach((lora) => { if (lora.active) activeLoraNames.add(lora.name); });
            updateConnectedTriggerWords(this, activeLoraNames);
          } finally {
            isUpdating = false;
          }
          scheduleInputSync(value);
        });

        this.lorasWidget = result.widget;

        inputWidget.callback = (value) => {
          if (isUpdating) return;
          isUpdating = true;
          try {
            const currentLoras = this.lorasWidget?.value || [];
            const mergedLoras = mergeLoras(value, currentLoras);
            if (this.lorasWidget) this.lorasWidget.value = mergedLoras;
            const activeLoraNames = getActiveLorasFromNode(this);
            updateConnectedTriggerWords(this, activeLoraNames);
          } finally {
            isUpdating = false;
          }
        };
      });
    }
  },

  async loadedGraphNode(node) {
    if (node.comfyClass === "WanVideo Lora Select (Remote, LoraManager)") {
      let existingLoras = [];
      if (node.widgets_values && node.widgets_values.length > 0) {
        // 0=low_mem_load, 1=merge_loras, 2=text, 3=loras
        existingLoras = node.widgets_values[3] || [];
      }
      const inputWidget = node.inputWidget || node.widgets[2];
      const mergedLoras = mergeLoras(inputWidget.value, existingLoras);
      node.lorasWidget.value = mergedLoras;
    }
  },
});
