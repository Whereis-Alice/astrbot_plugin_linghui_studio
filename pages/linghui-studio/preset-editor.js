const nameOrder = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
const sourceOrder = { custom: 0, modified: 1, default: 2 };
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));

export function presetChannelIds(preset = {}) {
  if (Array.isArray(preset.channel_ids)) return [...preset.channel_ids];
  return preset.channel_id ? [preset.channel_id] : [];
}

export function selectedPresetChannels(container) {
  return [...container.querySelectorAll("[data-preset-channel]:checked")].map((input) => input.value);
}

export function movePresetChannel(selected, channelId, direction) {
  const next = [...selected];
  const index = next.indexOf(channelId);
  const destination = index + (direction === "up" ? -1 : 1);
  if (index >= 0 && destination >= 0 && destination < next.length) [next[index], next[destination]] = [next[destination], next[index]];
  return next;
}

function presetChannelPickerContents(channels, selected) {
  const byId = new Map(channels.map((channel) => [channel.id, channel]));
  const ordered = [...selected.map((id) => byId.get(id) || { id, unavailable: true }), ...channels.filter((channel) => !selected.includes(channel.id))];
  const summary = selected.length ? selected.map((id) => byId.get(id)?.name || id).join(" → ") : "跟随会话 / 默认渠道";
  return `<summary title="${escapeHtml(summary)}">${escapeHtml(summary)}</summary><div class="preset-channel-choices">
    <p class="field-hint">勾选主用和备用渠道，用 ↑ ↓ 调整顺序；失败只在所选渠道间回退。全部取消则跟随默认路由。</p>
    ${ordered.map((channel) => {
      const index = selected.indexOf(channel.id);
      const chosen = index >= 0;
      const unavailable = channel.unavailable || channel.enabled === false;
      const label = `${chosen ? index === 0 ? "主用 · " : `备用 ${index} · ` : ""}${channel.name || channel.id}${channel.name && channel.name !== channel.id ? ` (${channel.id})` : ""}${unavailable ? "（不可用，请取消或更换）" : ""}`;
      return `<div class="preset-channel-choice"><label><input type="checkbox" data-preset-channel value="${escapeHtml(channel.id)}" ${chosen ? "checked" : ""} ${unavailable && !chosen ? "disabled" : ""} /><span>${escapeHtml(label)}</span></label><span class="preset-channel-order" ${chosen ? "" : "hidden"}><button type="button" data-preset-channel-move="up" data-channel-id="${escapeHtml(channel.id)}" aria-label="提前 ${escapeHtml(channel.name || channel.id)}" ${index <= 0 ? "disabled" : ""}>↑</button><button type="button" data-preset-channel-move="down" data-channel-id="${escapeHtml(channel.id)}" aria-label="延后 ${escapeHtml(channel.name || channel.id)}" ${index === selected.length - 1 ? "disabled" : ""}>↓</button></span></div>`;
    }).join("") || '<p class="field-hint">尚未配置渠道，请先在“绘图渠道”中添加。</p>'}
  </div>`;
}

export function presetChannelPicker(channels = [], selected = []) {
  return `<details class="preset-channel-picker" data-preset-channel-picker>${presetChannelPickerContents(channels, selected)}</details>`;
}

export function handlePresetChannelPickerEvent(event, channels = []) {
  const move = event.target.closest?.("[data-preset-channel-move]");
  const choice = event.target.closest?.("[data-preset-channel]");
  if (!move && !choice) return false;
  const picker = event.target.closest("[data-preset-channel-picker]");
  if (!picker) return false;
  let selected = selectedPresetChannels(picker);
  if (move) {
    event.preventDefault();
    if (move.disabled) return false;
    selected = movePresetChannel(selected, move.dataset.channelId, move.dataset.presetChannelMove);
  }
  picker.innerHTML = presetChannelPickerContents(channels, selected);
  picker.open = true;
  const controls = [...picker.querySelectorAll(move ? "[data-preset-channel-move]" : "[data-preset-channel]")];
  controls.find((control) => move
    ? control.dataset.channelId === move.dataset.channelId && control.dataset.presetChannelMove === move.dataset.presetChannelMove
    : control.value === choice.value)?.focus();
  return true;
}

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
    const picker = row.querySelector("[data-preset-channel-picker]");
    if (picker) {
      preset.channel_ids = selectedPresetChannels(picker);
      preset.channel_id = preset.channel_ids[0] || "";
    }
    for (const key of ["image_output_format", "image_frame_count", "image_frame_duration_ms", "image_complexity"]) {
      const field = row.querySelector(`[data-preset="${key}"]`);
      if (field) preset[key] = field.value;
    }
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

export function normalizePresetGenerationOptions(preset = {}) {
  const reject = (message, field) => { const error = new Error(message); error.field = field; throw error; };
  const format = String(preset.image_output_format || "auto").trim().toLowerCase();
  if (!["auto", "png", "jpeg", "webp", "gif"].includes(format)) reject("预设输出格式无效。", "image_output_format");
  const count = Number(preset.image_frame_count ?? 9);
  const duration = Number(preset.image_frame_duration_ms ?? 200);
  if (!Number.isInteger(count) || count < 2 || count > 16) reject("GIF 帧数必须在 2 到 16 之间。", "image_frame_count");
  if (!Number.isInteger(duration) || duration < 20 || duration > 2000 || duration % 10) reject("GIF 每帧时长必须为 20 到 2000 的 10 的倍数。", "image_frame_duration_ms");
  const complexity = String(preset.image_complexity || "auto").trim().toLowerCase();
  if (!["auto", "low", "medium", "high"].includes(complexity)) reject("预设复杂度无效。", "image_complexity");
  return { image_output_format: format, image_frame_count: count, image_frame_duration_ms: duration, image_complexity: complexity };
}

function presetGenerationFields(preset = {}) {
  // Rendering/searching must retain incomplete drafts; validate only on save.
  const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const options = {
    image_output_format: preset.image_output_format || "auto",
    image_frame_count: preset.image_frame_count ?? 9,
    image_frame_duration_ms: preset.image_frame_duration_ms ?? 200,
    image_complexity: preset.image_complexity || "auto",
  };
  const gif = options.image_output_format === "gif";
  return `
    <label class="preset-inline-field"><span>输出</span><select data-preset="image_output_format" aria-label="预设输出格式">
      <option value="auto" ${options.image_output_format === "auto" ? "selected" : ""}>跟随渠道</option>
      <option value="png" ${options.image_output_format === "png" ? "selected" : ""}>PNG</option>
      <option value="jpeg" ${options.image_output_format === "jpeg" ? "selected" : ""}>JPEG</option>
      <option value="webp" ${options.image_output_format === "webp" ? "selected" : ""}>WebP</option>
      <option value="gif" ${gif ? "selected" : ""}>GIF 动图</option>
    </select></label>
    <label class="preset-inline-field preset-gif-option" data-preset-gif-option ${gif ? "" : "hidden"}><span>帧数</span><input data-preset="image_frame_count" type="number" min="2" max="16" step="1" value="${escape(options.image_frame_count)}" /></label>
    <label class="preset-inline-field preset-gif-option" data-preset-gif-option ${gif ? "" : "hidden"}><span>每帧 ms</span><input data-preset="image_frame_duration_ms" type="number" min="20" max="2000" step="10" value="${escape(options.image_frame_duration_ms)}" /></label>
    <label class="preset-inline-field preset-gif-option" data-preset-gif-option ${gif ? "" : "hidden"}><span>复杂度</span><select data-preset="image_complexity"><option value="auto" ${options.image_complexity === "auto" ? "selected" : ""}>自动</option><option value="low" ${options.image_complexity === "low" ? "selected" : ""}>低</option><option value="medium" ${options.image_complexity === "medium" ? "selected" : ""}>中</option><option value="high" ${options.image_complexity === "high" ? "selected" : ""}>高</option></select></label>`;
}

export { presetGenerationFields };

export function createPresetDialog({ getPresets, getChannels = () => [], createPreset, onCreated }) {
  const dialog = document.getElementById("preset-create-dialog");
  const form = document.getElementById("preset-create-form");
  const name = document.getElementById("preset-create-name");
  const prompt = document.getElementById("preset-create-prompt");
  const channel = document.getElementById("preset-create-channel");
  const format = document.getElementById("preset-create-output-format");
  const animation = document.getElementById("preset-create-animation");
  const frameCount = document.getElementById("preset-create-frame-count");
  const frameDuration = document.getElementById("preset-create-frame-duration-ms");
  const complexity = document.getElementById("preset-create-complexity");
  const error = document.getElementById("preset-create-error");
  const cancel = document.getElementById("preset-create-cancel");
  const submit = document.getElementById("preset-create-submit");
  let saving = false;
  let opener = null;

  function setSaving(value) {
    saving = value;
    [name, prompt, channel, format, frameCount, frameDuration, complexity, cancel, submit].filter(Boolean).forEach((control) => { control.disabled = value; });
    if (channel) channel.inert = value;
    submit.textContent = value ? "正在保存…" : "确认添加";
    form.setAttribute("aria-busy", String(value));
  }

  function open() {
    if (saving || dialog.open) return;
    opener = document.activeElement;
    name.value = prompt.value = "";
    if (channel) channel.innerHTML = presetChannelPicker(getChannels());
    if (format) format.value = "auto";
    if (frameCount) frameCount.value = "9";
    if (frameDuration) frameDuration.value = "200";
    if (complexity) complexity.value = "auto";
    if (animation) animation.hidden = true;
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
      if (channel) {
        preset.channel_ids = selectedPresetChannels(channel);
        preset.channel_id = preset.channel_ids[0] || "";
      }
      Object.assign(preset, normalizePresetGenerationOptions({
        image_output_format: format?.value || "auto", image_frame_count: frameCount?.value ?? 9,
        image_frame_duration_ms: frameDuration?.value ?? 200, image_complexity: complexity?.value || "auto",
      }));
    } catch (invalid) {
      error.textContent = invalid.message;
      error.hidden = false;
      ({ prompt, image_output_format: format, image_frame_count: frameCount, image_frame_duration_ms: frameDuration, image_complexity: complexity }[invalid.field] || name).focus();
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
  channel?.addEventListener("change", (event) => { if (!saving) handlePresetChannelPickerEvent(event, getChannels()); });
  channel?.addEventListener("click", (event) => {
    if (!saving && event.target.closest?.("[data-preset-channel-move]")) handlePresetChannelPickerEvent(event, getChannels());
  });
  format?.addEventListener("change", () => { if (animation) animation.hidden = format.value !== "gif"; });
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", () => { if (opener?.isConnected) opener.focus({ preventScroll: true }); });
  return { open };
}
