const nameOrder = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
const sourceOrder = { custom: 0, modified: 1, default: 2 };

export function sortedPresetEntries(presets, order = "grouped") {
  return presets.map((preset, index) => ({ preset, index })).sort((left, right) => {
    if (order === "grouped") {
      const group = (sourceOrder[left.preset.source] ?? 0) - (sourceOrder[right.preset.source] ?? 0);
      if (group) return group;
    }
    const names = nameOrder.compare(left.preset.name || "", right.preset.name || "");
    return (order === "name_desc" ? -names : names) || left.index - right.index;
  });
}

export function capturePresetDrafts(presets, container) {
  container.querySelectorAll(".preset-row[data-preset-index]").forEach((row) => {
    const preset = presets[Number(row.dataset.presetIndex)];
    if (!preset) return;
    preset.name = row.querySelector('[data-preset="name"]').value;
    preset.prompt = row.querySelector('[data-preset="prompt"]').value;
    const channel = row.querySelector('[data-preset="channel_id"]');
    if (channel) preset.channel_id = channel.value;
  });
}

export function presetChannelOptions(channels = [], selected = "") {
  const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const options = [`<option value="" ${selected ? "" : "selected"}>跟随会话 / 默认渠道</option>`];
  if (selected && !channels.some((channel) => channel.id === selected)) {
    options.push(`<option value="${escape(selected)}" selected>不可用：${escape(selected)}（请更换或解除）</option>`);
  }
  channels.forEach((channel) => {
    const enabled = channel.enabled !== false;
    options.push(`<option value="${escape(channel.id)}" ${channel.id === selected ? "selected" : ""} ${!enabled && channel.id !== selected ? "disabled" : ""}>${escape(channel.name || channel.id)}${channel.name ? ` (${escape(channel.id)})` : ""}${enabled ? "" : "（已禁用）"}</option>`);
  });
  return options.join("");
}

export function validateNewPreset(rawName, rawPrompt, presets = []) {
  const name = String(rawName || "").trim();
  const prompt = String(rawPrompt || "").trim();
  const reject = (message, field) => { const error = new Error(message); error.field = field; throw error; };
  if (!name) reject("请填写预设名称。", "name");
  if (/[\x00-\x1f\x7f:：]/.test(name) || [...name].length > 48) {
    reject("预设名称不能包含冒号或换行，且最多 48 个字符。", "name");
  }
  if (presets.some((preset) => String(preset.name || "").trim() === name)) reject(`预设名称已存在：${name}`, "name");
  if (!prompt) reject("请填写提示词。", "prompt");
  if ([...prompt].length > 12_000) reject("提示词最多 12000 个字符。", "prompt");
  return { name, prompt };
}

export function createPresetDialog({ getPresets, getChannels = () => [], createPreset, onCreated }) {
  const dialog = document.getElementById("preset-create-dialog");
  const form = document.getElementById("preset-create-form");
  const name = document.getElementById("preset-create-name");
  const prompt = document.getElementById("preset-create-prompt");
  const channel = document.getElementById("preset-create-channel");
  const error = document.getElementById("preset-create-error");
  const cancel = document.getElementById("preset-create-cancel");
  const submit = document.getElementById("preset-create-submit");
  let saving = false;
  let opener = null;

  function setSaving(value) {
    saving = value;
    [name, prompt, channel, cancel, submit].filter(Boolean).forEach((control) => { control.disabled = value; });
    submit.textContent = value ? "正在保存…" : "确认添加";
    form.setAttribute("aria-busy", String(value));
  }

  function open() {
    if (saving || dialog.open) return;
    opener = document.activeElement;
    name.value = prompt.value = "";
    if (channel) { channel.innerHTML = presetChannelOptions(getChannels()); channel.value = ""; }
    error.hidden = true;
    error.textContent = "";
    setSaving(false);
    dialog.showModal();
    name.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    error.hidden = true;
    let preset;
    try {
      preset = validateNewPreset(name.value, prompt.value, getPresets());
      if (channel?.value) preset.channel_id = channel.value;
    } catch (invalid) {
      error.textContent = invalid.message;
      error.hidden = false;
      (invalid.field === "prompt" ? prompt : name).focus();
      return;
    }
    setSaving(true);
    try {
      const response = await createPreset(preset);
      if (!response?.success) throw new Error(response?.message || "预设保存失败，请重试。");
      await onCreated(response.preset || { ...preset, source: "custom" });
      dialog.close();
    } catch (failed) {
      error.textContent = failed.message || "预设保存失败，请重试。";
      error.hidden = false;
    } finally {
      setSaving(false);
    }
  });
  cancel.addEventListener("click", () => { if (!saving) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", () => { if (opener?.isConnected) opener.focus({ preventScroll: true }); });
  return { open };
}
