const bridge = window.AstrBotPluginPage || {
  ready: async () => ({}),
  apiGet: async (endpoint) => {
    const response = await fetch(`/api/plug/astrbot_plugin_linghui_studio/${endpoint}`);
    return response.json();
  },
  apiPost: async (endpoint, body) => {
    const response = await fetch(`/api/plug/astrbot_plugin_linghui_studio/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return response.json();
  },
};

const state = {
  config: null,
  usage: { users: [], groups: [], daily_stats: {} },
};

const titles = {
  overview: ["概览", "查看当前绘图服务状态和今日用量。"],
  channels: ["绘图渠道", "配置主渠道、模型和失败后的回退顺序。"],
  access: ["权限与额度", "把访问权限与无限次数名单分开管理。"],
  prompts: ["提示词与预设", "维护快捷预设，并选择可选的翻译和优化模型。"],
  persona: ["人设与参考图", "维护人设图片和每个预设的参考图集合。"],
  settings: ["运行设置", "调整兼容单接口模式和通用图像参数。"],
};

const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function bool(value) {
  return value === true || value === 1 || String(value).toLowerCase() === "true";
}

function value(id, fallback = "") {
  const element = byId(id);
  return element ? element.value : fallback;
}

function checked(id) {
  return Boolean(byId(id)?.checked);
}

function idsFromText(text) {
  return String(text || "").split(/[\s,;，；]+/).map((item) => item.trim()).filter(Boolean);
}

function linesFromText(text) {
  return String(text || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function showToast(message, error = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function setSaveState(message = "") {
  byId("save-state").textContent = message;
}

function channelTemplate(channel = {}, index = 0) {
  const item = {
    id: channel.id || `channel_${index + 1}`,
    name: channel.name || "",
    enabled: channel.enabled !== false,
    fallback_enabled: channel.fallback_enabled !== false,
    interface_mode: channel.interface_mode || "openai_chat",
    base_url: channel.base_url || "",
    model: channel.model || "",
    text_to_image_model: channel.text_to_image_model || "",
    timeout: channel.timeout || 120,
    api_keys_masked: channel.api_keys_masked || "",
  };
  const masked = item.api_keys_masked ? `已配置：${item.api_keys_masked}` : "每行一个 API Key";
  return `
    <article class="channel-row" data-index="${index}" data-original-id="${escapeHtml(channel.id || "")}">
      <div class="row-top">
        <div class="row-title"><span class="status-dot ${item.enabled ? "ok" : "off"}"></span><span>渠道 ${index + 1}</span></div>
        <button class="remove-button" type="button" data-remove-channel="${index}" title="删除渠道" aria-label="删除渠道">×</button>
      </div>
      <div class="form-grid">
        <label class="field"><span>渠道 ID</span><input data-channel="id" value="${escapeHtml(item.id)}" /></label>
        <label class="field"><span>显示名称</span><input data-channel="name" value="${escapeHtml(item.name)}" /></label>
        <label class="field"><span>接口模式</span><select data-channel="interface_mode">
          ${["openai_chat", "openai_image", "gemini_official", "custom_endpoint"].map((kind) => `<option value="${kind}" ${item.interface_mode === kind ? "selected" : ""}>${kind}</option>`).join("")}
        </select></label>
        <label class="field"><span>超时（秒）</span><input data-channel="timeout" type="number" min="5" value="${escapeHtml(item.timeout)}" /></label>
        <label class="field full"><span>接口地址</span><input data-channel="base_url" value="${escapeHtml(item.base_url)}" placeholder="https://api.example.com" /></label>
        <label class="field full"><span>API Key</span><textarea data-channel="api_keys" rows="2" placeholder="${escapeHtml(masked)}"></textarea></label>
        ${item.api_keys_masked ? '<label class="toggle-field full"><input data-channel="clear_api_keys" type="checkbox" /><span>清除已保存的 API Key</span></label>' : ""}
        <label class="field"><span>默认模型</span><input data-channel="model" value="${escapeHtml(item.model)}" /></label>
        <label class="field"><span>文生图模型（可选）</span><input data-channel="text_to_image_model" value="${escapeHtml(item.text_to_image_model)}" /></label>
        <label class="toggle-field"><input data-channel="enabled" type="checkbox" ${item.enabled ? "checked" : ""} /><span>启用渠道</span></label>
        <label class="toggle-field"><input data-channel="fallback_enabled" type="checkbox" ${item.fallback_enabled ? "checked" : ""} /><span>允许作为回退渠道</span></label>
      </div>
    </article>`;
}

function readChannels() {
  return [...document.querySelectorAll(".channel-row")].map((row) => {
    const get = (key) => row.querySelector(`[data-channel="${key}"]`);
    const clearApiKeys = Boolean(get("clear_api_keys")?.checked);
    return {
      id: get("id").value.trim(),
      original_id: row.dataset.originalId || "",
      name: get("name").value.trim(),
      interface_mode: get("interface_mode").value,
      timeout: Number(get("timeout").value) || 120,
      base_url: get("base_url").value.trim(),
      api_keys: clearApiKeys ? "" : get("api_keys").value.trim(),
      clear_api_keys: clearApiKeys,
      model: get("model").value.trim(),
      text_to_image_model: get("text_to_image_model").value.trim(),
      enabled: get("enabled").checked,
      fallback_enabled: get("fallback_enabled").checked,
    };
  });
}

function renderChannels() {
  const channels = state.config?.channels || [];
  byId("channel-list").innerHTML = channels.map(channelTemplate).join("") || '<p class="empty">尚未配置渠道。添加至少一个渠道后才能开始绘图。</p>';
  const current = state.config?.active_drawing_channel || "";
  const options = ['<option value="">自动（按列表顺序）</option>'].concat(channels.map((channel) => `<option value="${escapeHtml(channel.id)}" ${channel.id === current ? "selected" : ""}>${escapeHtml(channel.id)}${channel.name ? ` - ${escapeHtml(channel.name)}` : ""}</option>`));
  byId("active-channel").innerHTML = options.join("");
}

function renderPresets() {
  const presets = state.config?.presets || [];
  byId("preset-list").innerHTML = presets.map((preset, index) => `
    <div class="preset-row" data-preset-index="${index}">
      <input data-preset="name" value="${escapeHtml(preset.name)}" placeholder="预设名" />
      <textarea data-preset="prompt" rows="3" placeholder="提示词">${escapeHtml(preset.prompt)}</textarea>
      <button class="remove-button" type="button" data-remove-preset="${index}" title="删除预设" aria-label="删除预设">×</button>
    </div>`).join("") || '<p class="empty">尚未配置预设。</p>';
}

function readPresets() {
  return [...document.querySelectorAll("[data-preset-index]")].map((row) => ({
    name: row.querySelector('[data-preset="name"]').value.trim(),
    prompt: row.querySelector('[data-preset="prompt"]').value.trim(),
  })).filter((item) => item.name || item.prompt);
}

function renderOverview() {
  const config = state.config || {};
  const channels = config.channels || [];
  const enabled = channels.filter((item) => item.enabled).length;
  const daily = config && state.usage?.daily_stats?.users ? Object.values(state.usage.daily_stats.users).reduce((sum, count) => sum + Number(count || 0), 0) : 0;
  byId("metric-channels").textContent = String(enabled);
  byId("metric-today").textContent = String(daily);
  byId("metric-presets").textContent = String((config.presets || []).length);
  byId("metric-references").textContent = String(config.references?.stats?.total_images || 0);
  const active = config.active_drawing_channel || "自动";
  byId("overview-channels").innerHTML = channels.map((channel, index) => `
    <div class="summary-row"><span><i class="status-dot ${channel.enabled ? "ok" : "off"}"></i> ${escapeHtml(channel.id)}</span><small>${escapeHtml(channel.name || channel.interface_mode)} · ${escapeHtml(channel.model || "未填模型")}</small><span>${channel.id === active ? "主渠道" : (channel.fallback_enabled ? "备用" : "不回退")}</span></div>`).join("") || '<p class="empty">未配置绘图渠道，当前会使用兼容单接口模式。</p>';
  renderUsage("overview-usage", [...(state.usage?.users || [])].slice(0, 12), "用户");
}

function renderUsage(targetId, rows, kind) {
  const target = byId(targetId);
  if (!rows.length) {
    target.innerHTML = '<p class="empty">暂无用量记录。</p>';
    return;
  }
  target.innerHTML = `<table><thead><tr><th>${kind}</th><th>额度</th><th>签到</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(row.id)}</td><td>${escapeHtml(row.credits ?? row.count ?? "-")}</td><td>${escapeHtml(row.checked_in || "-")}</td></tr>`).join("")}</tbody></table>`;
}

function renderCreditTable() {
  const users = state.usage?.users || [];
  const groups = state.usage?.groups || [];
  const rows = users.map((row) => ({ ...row, kind: "用户" })).concat(groups.map((row) => ({ ...row, kind: "群" })));
  const target = byId("credit-table");
  if (!rows.length) {
    target.innerHTML = '<p class="empty">暂无额度记录。</p>';
    return;
  }
  target.innerHTML = `<table><thead><tr><th>类型</th><th>ID</th><th>额度</th><th>签到</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${row.kind}</td><td>${escapeHtml(row.id)}</td><td>${escapeHtml(row.credits)}</td><td>${escapeHtml(row.checked_in || "-")}</td></tr>`).join("")}</tbody></table>`;
}

function setListValue(id, items) { byId(id).value = (items || []).join("\n"); }

function hydrateFields() {
  const config = state.config;
  if (!config) return;
  const permissions = config.permissions || {};
  const usage = config.usage || {};
  const tools = config.prompt_tools || {};
  const persona = config.persona || {};
  const settings = config.settings || {};
  const commands = config.commands || {};
  byId("group-access-mode").value = permissions.group_access_mode || "whitelist";
  byId("allow-private").checked = bool(permissions.allow_private_messages);
  byId("admins-unlimited").checked = bool(permissions.admins_unlimited);
  ["allowed-users", "blocked-users", "group-whitelist", "group-blacklist", "unlimited-users", "unlimited-groups"].forEach((id, index) => setListValue(id, [permissions.allowed_users, permissions.blocked_users, permissions.group_whitelist, permissions.group_blacklist, permissions.unlimited_users, permissions.unlimited_groups][index]));
  byId("enable-user-limit").checked = bool(usage.enable_user_limit);
  byId("enable-group-limit").checked = bool(usage.enable_group_limit);
  byId("enable-checkin").checked = bool(usage.enable_checkin);
  byId("enable-random-checkin").checked = bool(usage.enable_random_checkin);
  byId("checkin-fixed").value = usage.checkin_fixed_reward ?? 3;
  byId("checkin-max").value = usage.checkin_random_reward_max ?? 5;
  byId("enable-translation").checked = bool(tools.enable_prompt_translation);
  byId("enable-optimization").checked = bool(tools.enable_prompt_optimization);
  byId("processor-base-url").value = tools.prompt_processor_base_url || "";
  byId("processor-api-key").placeholder = tools.prompt_processor_api_key_masked ? `已配置：${tools.prompt_processor_api_key_masked}` : "仅在要替换时填写";
  byId("clear-processor-api-key").checked = false;
  byId("translation-model").value = tools.prompt_translation_model || "";
  byId("optimization-model").value = tools.prompt_optimization_model || "";
  byId("processor-timeout").value = tools.prompt_processor_timeout ?? 30;
  byId("translation-system").value = tools.prompt_translation_system_prompt || "";
  byId("optimization-system").value = tools.prompt_optimization_system_prompt || "";
  byId("enable-persona").checked = bool(settings.enable_persona_mode);
  byId("command-namespace").value = commands.namespace || "";
  byId("enable-direct-commands").checked = bool(commands.enable_direct_commands);
  byId("persona-name").value = persona.name || "";
  byId("persona-description").value = persona.description || "";
  byId("persona-style").value = persona.photo_style || "";
  byId("persona-keywords").value = (persona.trigger_keywords || []).join(", ");
  byId("persona-default-prompt").value = persona.default_prompt || "";
  byId("persona-scene-prompts").value = (persona.scene_prompts || []).join("\n");
  byId("default-model").value = settings.model || "";
  byId("default-t2i-model").value = settings.text_to_image_model || "";
  byId("resolution").value = settings.image_resolution || "1K";
  byId("aspect-ratio").value = settings.image_aspect_ratio || "1:1";
  byId("default-timeout").value = settings.timeout ?? 120;
  byId("show-model-info").checked = bool(settings.show_model_info);
  byId("enable-preset-refs").checked = bool(settings.enable_preset_ref_images);
  renderChannels();
  renderPresets();
  renderReferenceSelector();
  renderPersonaImages();
  renderReferenceImages();
}

function renderReferenceSelector() {
  const selected = byId("reference-preset")?.value || "";
  const names = new Set((state.config?.presets || []).map((item) => item.name));
  (state.config?.references?.items || []).forEach((item) => names.add(item.preset));
  const options = [...names].filter((name) => name && name !== "_persona_").map((name) => `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(name)}</option>`);
  byId("reference-preset").innerHTML = options.join("") || '<option value="">请先添加一个预设</option>';
}

function imageTiles(images, preset) {
  if (!images?.length) return '<p class="empty">暂无参考图。</p>';
  return images.map((url, index) => `<div class="image-tile"><img src="${escapeHtml(url)}" alt="参考图 ${index + 1}" /><button type="button" data-delete-reference="${escapeHtml(preset)}" data-index="${index}" title="删除参考图" aria-label="删除参考图">×</button></div>`).join("");
}

function renderPersonaImages() {
  byId("persona-images").innerHTML = imageTiles(state.config?.persona?.reference_images || [], "_persona_");
}

function renderReferenceImages() {
  const preset = byId("reference-preset")?.value || "";
  const item = (state.config?.references?.items || []).find((entry) => entry.preset === preset);
  byId("reference-images").innerHTML = imageTiles(item?.images || [], preset);
}

function buildPayload() {
  const clearProcessorApiKey = checked("clear-processor-api-key");
  return {
    settings: {
      model: value("default-model").trim(),
      text_to_image_model: value("default-t2i-model").trim(),
      image_resolution: value("resolution"),
      image_aspect_ratio: value("aspect-ratio"),
      timeout: Number(value("default-timeout")) || 120,
      show_model_info: checked("show-model-info"),
      enable_preset_ref_images: checked("enable-preset-refs"),
      enable_persona_mode: checked("enable-persona"),
    },
    channels: readChannels(),
    active_drawing_channel: value("active-channel"),
    commands: {
      namespace: value("command-namespace").trim(),
      enable_direct_commands: checked("enable-direct-commands"),
    },
    permissions: {
      group_access_mode: value("group-access-mode"),
      allow_private_messages: checked("allow-private"),
      admins_unlimited: checked("admins-unlimited"),
      allowed_users: idsFromText(value("allowed-users")),
      blocked_users: idsFromText(value("blocked-users")),
      group_whitelist: idsFromText(value("group-whitelist")),
      group_blacklist: idsFromText(value("group-blacklist")),
      unlimited_users: idsFromText(value("unlimited-users")),
      unlimited_groups: idsFromText(value("unlimited-groups")),
    },
    usage: {
      enable_user_limit: checked("enable-user-limit"),
      enable_group_limit: checked("enable-group-limit"),
      enable_checkin: checked("enable-checkin"),
      enable_random_checkin: checked("enable-random-checkin"),
      checkin_fixed_reward: Number(value("checkin-fixed")) || 0,
      checkin_random_reward_max: Number(value("checkin-max")) || 1,
    },
    prompt_tools: {
      enable_prompt_translation: checked("enable-translation"),
      enable_prompt_optimization: checked("enable-optimization"),
      prompt_processor_base_url: value("processor-base-url").trim(),
      prompt_processor_api_key: clearProcessorApiKey ? "" : value("processor-api-key").trim(),
      clear_prompt_processor_api_key: clearProcessorApiKey,
      prompt_translation_model: value("translation-model").trim(),
      prompt_optimization_model: value("optimization-model").trim(),
      prompt_processor_timeout: Number(value("processor-timeout")) || 30,
      prompt_translation_system_prompt: value("translation-system").trim(),
      prompt_optimization_system_prompt: value("optimization-system").trim(),
    },
    persona: {
      name: value("persona-name").trim(),
      description: value("persona-description").trim(),
      photo_style: value("persona-style").trim(),
      trigger_keywords: idsFromText(value("persona-keywords")),
      default_prompt: value("persona-default-prompt").trim(),
      scene_prompts: linesFromText(value("persona-scene-prompts")),
    },
    presets: readPresets(),
  };
}

async function loadUsage() {
  const response = await bridge.apiGet("get_usage");
  if (!response?.success) throw new Error(response?.message || "无法读取用量");
  state.usage = response;
  renderOverview();
  renderCreditTable();
}

async function loadConfig() {
  setSaveState("正在加载...");
  const response = await bridge.apiGet("get_config");
  if (!response?.success) throw new Error(response?.message || "无法读取配置");
  state.config = response;
  hydrateFields();
  await loadUsage();
  setSaveState("配置已加载");
}

async function saveConfig() {
  setSaveState("正在保存...");
  const response = await bridge.apiPost("save_config", buildPayload());
  if (!response?.success) throw new Error(response?.message || "保存失败");
  showToast("配置已保存");
  await loadConfig();
}

async function updateCredit(reset = false) {
  const id = value("credit-id").trim();
  if (!id) { showToast("请填写 ID 或群号", true); return; }
  if (reset && !window.confirm(`确认重置 ${id} 的额度和签到状态吗？`)) return;
  const endpoint = reset ? "reset_credit" : "adjust_credit";
  const body = { kind: value("credit-kind"), id, amount: Number(value("credit-amount")) || 1 };
  const response = await bridge.apiPost(endpoint, body);
  if (!response?.success) throw new Error(response?.message || "额度操作失败");
  showToast(response.message || "额度已更新");
  await loadUsage();
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 10 * 1024 * 1024) { reject(new Error(`${file.name} 超过 10 MB`)); return; }
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function uploadReferences(files, preset) {
  if (!preset) { showToast("请先选择预设", true); return; }
  for (const file of [...files]) {
    const dataUrl = await readFile(file);
    const response = await bridge.apiPost("reference", { action: "upload", preset, data_url: dataUrl });
    if (!response?.success) throw new Error(response?.message || "上传失败");
  }
  showToast("参考图已上传");
  await loadConfig();
}

async function deleteReference(preset, index) {
  if (!preset || !window.confirm("确认删除这张参考图吗？")) return;
  const response = await bridge.apiPost("reference", { action: "delete", preset, index: Number(index) });
  if (!response?.success) throw new Error(response?.message || "删除失败");
  showToast("参考图已删除");
  await loadConfig();
}

async function clearReference(preset) {
  if (!preset || !window.confirm("确认清空该集合中的全部参考图吗？")) return;
  const response = await bridge.apiPost("reference", { action: "clear", preset });
  if (!response?.success) throw new Error(response?.message || "清空失败");
  showToast(response.message || "参考图已清空");
  await loadConfig();
}

function switchTab(tab) {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  document.querySelectorAll(".tab-pane").forEach((pane) => pane.classList.toggle("active", pane.id === `tab-${tab}`));
  const [title, subtitle] = titles[tab] || titles.overview;
  byId("page-title").textContent = title;
  byId("page-subtitle").textContent = subtitle;
}

document.addEventListener("click", async (event) => {
  try {
    const tab = event.target.closest("[data-tab]");
    if (tab) { switchTab(tab.dataset.tab); return; }
    if (event.target.closest("#save-button")) { await saveConfig(); return; }
    if (event.target.closest("#reload-button")) { await loadConfig(); return; }
    if (event.target.closest("#refresh-usage")) { await loadUsage(); showToast("用量已刷新"); return; }
    if (event.target.closest("#add-channel")) {
      state.config.channels.push({ id: `channel_${state.config.channels.length + 1}`, enabled: true, fallback_enabled: true, interface_mode: "openai_chat", timeout: 120 });
      renderChannels(); return;
    }
    const removeChannel = event.target.closest("[data-remove-channel]");
    if (removeChannel) {
      state.config.channels.splice(Number(removeChannel.dataset.removeChannel), 1);
      renderChannels(); return;
    }
    if (event.target.closest("#add-preset")) {
      state.config.presets.push({ name: "", prompt: "" }); renderPresets(); return;
    }
    const removePreset = event.target.closest("[data-remove-preset]");
    if (removePreset) {
      state.config.presets.splice(Number(removePreset.dataset.removePreset), 1); renderPresets(); return;
    }
    if (event.target.closest("#credit-add")) { await updateCredit(false); return; }
    if (event.target.closest("#credit-reset")) { await updateCredit(true); return; }
    const deleteButton = event.target.closest("[data-delete-reference]");
    if (deleteButton) { await deleteReference(deleteButton.dataset.deleteReference, deleteButton.dataset.index); return; }
    const clearButton = event.target.closest("[data-clear-ref]");
    if (clearButton) { await clearReference(clearButton.dataset.clearRef); return; }
    if (event.target.closest("#reference-clear")) { await clearReference(value("reference-preset")); }
  } catch (error) {
    console.error(error);
    showToast(error.message || "操作失败", true);
    setSaveState("操作失败");
  }
});

byId("reference-preset").addEventListener("change", renderReferenceImages);
byId("persona-upload").addEventListener("change", async (event) => {
  try { await uploadReferences(event.target.files, "_persona_"); event.target.value = ""; } catch (error) { showToast(error.message || "上传失败", true); }
});
byId("reference-upload").addEventListener("change", async (event) => {
  try { await uploadReferences(event.target.files, value("reference-preset")); event.target.value = ""; } catch (error) { showToast(error.message || "上传失败", true); }
});

try {
  await bridge.ready();
  await loadConfig();
} catch (error) {
  console.error(error);
  showToast(error.message || "页面初始化失败", true);
  setSaveState("加载失败");
}
