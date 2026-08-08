const bridge = window.AstrBotPluginPage || {
  ready: async () => ({}),
  apiGet: async (endpoint, params = {}) => {
    const query = new URLSearchParams(params || {}).toString();
    const response = await fetch(`/api/plug/astrbot_plugin_linghui_studio/${endpoint}${query ? `?${query}` : ""}`);
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
  history: { records: [], pagination: { offset: 0, limit: 24, total: 0 }, summary: {}, favorite_only: false },
  historyFavoriteOnly: false,
};

const THEME_STORAGE_KEY = "linghui-studio-theme";
const THEME_VALUES = new Set(["dark", "light", "alice"]);
const HISTORY_PAGE_SIZE = 24;
const PLUGIN_API_BASE = "/api/plug/astrbot_plugin_linghui_studio";

const titles = {
  overview: ["概览", "查看当前绘图服务状态和今日用量。"],
  history: ["成功记录", "查看成功图片、请求提示词与缓存保护状态。"],
  favorites: ["收藏", "快速查看已收藏且不会自动清理的成功图片。"],
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

function storedTheme() {
  try {
    const theme = window.localStorage.getItem(THEME_STORAGE_KEY);
    return THEME_VALUES.has(theme) ? theme : "dark";
  } catch {
    return "dark";
  }
}

function setTheme(theme, persist = true) {
  const nextTheme = THEME_VALUES.has(theme) ? theme : "dark";
  document.documentElement.dataset.theme = nextTheme;
  const selector = byId("theme-select");
  if (selector) selector.value = nextTheme;
  if (persist) {
    try { window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme); } catch { /* Storage may be disabled. */ }
  }
}

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

let settleConfirmation = null;

function confirmAction({ title = "确认操作", message, confirmLabel = "确认", danger = true }) {
  const dialog = byId("action-confirm");
  const titleNode = byId("confirm-title");
  const messageNode = byId("confirm-message");
  const cancelButton = byId("confirm-cancel");
  const proceedButton = byId("confirm-proceed");
  if (!dialog || typeof dialog.showModal !== "function") {
    showToast("当前页面无法显示确认窗口，操作未执行。", true);
    return Promise.resolve(false);
  }

  if (settleConfirmation) settleConfirmation(false);
  const lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  titleNode.textContent = title;
  messageNode.textContent = message || "此操作无法撤销。";
  proceedButton.textContent = confirmLabel;
  proceedButton.classList.toggle("danger-action", danger);
  proceedButton.classList.toggle("primary-action", !danger);

  return new Promise((resolve) => {
    const cleanup = () => {
      cancelButton.removeEventListener("click", onCancel);
      proceedButton.removeEventListener("click", onProceed);
      dialog.removeEventListener("cancel", onDialogCancel);
      dialog.removeEventListener("click", onBackdropClick);
      if (settleConfirmation === finish) settleConfirmation = null;
      lastFocused?.focus?.();
    };
    const finish = (accepted) => {
      cleanup();
      if (dialog.open) dialog.close();
      resolve(accepted);
    };
    const onCancel = () => finish(false);
    const onProceed = () => finish(true);
    const onDialogCancel = (event) => {
      event.preventDefault();
      finish(false);
    };
    const onBackdropClick = (event) => {
      if (event.target === dialog) finish(false);
    };

    settleConfirmation = finish;
    cancelButton.addEventListener("click", onCancel);
    proceedButton.addEventListener("click", onProceed);
    dialog.addEventListener("cancel", onDialogCancel);
    dialog.addEventListener("click", onBackdropClick);
    dialog.showModal();
    proceedButton.focus();
  });
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
    image_edit_transport: channel.image_edit_transport || "auto",
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
        <label class="field"><span>渠道 ID</span><input data-channel="id" value="${escapeHtml(item.id)}" /><small class="field-hint">唯一标识，只能使用字母、数字、下划线和连字符；可用于管理员切换主渠道。</small></label>
        <label class="field"><span>显示名称</span><input data-channel="name" value="${escapeHtml(item.name)}" /><small class="field-hint">仅用于 Dashboard 和状态展示，留空时显示渠道 ID。</small></label>
        <label class="field"><span>接口模式</span><select data-channel="interface_mode">
          ${["openai_chat", "openai_image", "gemini_official", "custom_endpoint"].map((kind) => `<option value="${kind}" ${item.interface_mode === kind ? "selected" : ""}>${kind}</option>`).join("")}
        </select><small class="field-hint">New API/OpenAI Images 请选择 openai_image；不要把地址填到 /v1/images。</small></label>
        <label class="field"><span>图生图上传格式</span><select data-channel="image_edit_transport">
          ${[["auto", "自动"], ["multipart", "multipart 文件上传"], ["json", "JSON / Base64"]].map(([kind, label]) => `<option value="${kind}" ${item.image_edit_transport === kind ? "selected" : ""}>${label}</option>`).join("")}
        </select><small class="field-hint">人设拍照和参考图会使用此设置。auto 会为 gpt-image-* 选择标准 multipart，其余渠道保留兼容策略。</small></label>
        <label class="field"><span>超时（秒）</span><input data-channel="timeout" type="number" min="5" value="${escapeHtml(item.timeout)}" /><small class="field-hint">单次请求的最长等待时间。超时后会尝试下一个可回退渠道。</small></label>
        <label class="field full"><span>接口地址</span><input data-channel="base_url" value="${escapeHtml(item.base_url)}" placeholder="https://api.example.com" /><small class="field-hint">OpenAI Images/New API 填站点基础地址，例如 https://example.com；程序会自动补齐 /v1/images/generations 或 /v1/images/edits。自定义接口才填写完整地址。</small></label>
        <label class="field full"><span>API Key</span><textarea data-channel="api_keys" rows="2" placeholder="${escapeHtml(masked)}"></textarea><small class="field-hint">每行一个 Key，可轮换使用。已有 Key 不会回显；留空保存会保留原值。</small></label>
        ${item.api_keys_masked ? '<label class="toggle-field full"><input data-channel="clear_api_keys" type="checkbox" /><span class="toggle-copy"><span>清除已保存的 API Key</span><small class="field-hint">保存后永久删除该渠道的所有 Key。</small></span></label>' : ""}
        <label class="field"><span>默认模型</span><input data-channel="model" value="${escapeHtml(item.model)}" /><small class="field-hint">图生图和未指定模型的请求使用此模型。</small></label>
        <label class="field"><span>文生图模型（可选）</span><input data-channel="text_to_image_model" value="${escapeHtml(item.text_to_image_model)}" /><small class="field-hint">显式文生图优先使用此模型；留空时使用默认模型。</small></label>
        <label class="toggle-field"><input data-channel="enabled" type="checkbox" ${item.enabled ? "checked" : ""} /><span class="toggle-copy"><span>启用渠道</span><small class="field-hint">关闭后该渠道不会被主路由或回退流程调用。</small></span></label>
        <label class="toggle-field"><input data-channel="fallback_enabled" type="checkbox" ${item.fallback_enabled ? "checked" : ""} /><span class="toggle-copy"><span>允许作为回退渠道</span><small class="field-hint">前方已尝试的渠道报错后，才会按列表顺序尝试此渠道。</small></span></label>
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
      image_edit_transport: get("image_edit_transport").value,
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
  const dailyStats = state.usage?.daily_stats || {};
  const daily = dailyStats.users ? Object.values(dailyStats.users).reduce((sum, count) => sum + Number(count || 0), 0) : 0;
  byId("metric-channels").textContent = String(enabled);
  byId("metric-today").textContent = String(daily);
  byId("metric-presets").textContent = String((config.presets || []).length);
  byId("metric-references").textContent = String(config.references?.stats?.total_images || 0);
  const active = config.active_drawing_channel || "自动";
  byId("overview-channels").innerHTML = channels.map((channel, index) => `
    <div class="summary-row"><span><i class="status-dot ${channel.enabled ? "ok" : "off"}"></i> ${escapeHtml(channel.id)}</span><small>${escapeHtml(channel.name || channel.interface_mode)} · ${escapeHtml(channel.model || "未填模型")}</small><span>${channel.id === active ? "主渠道" : (channel.fallback_enabled ? "备用" : "不回退")}</span></div>`).join("") || '<p class="empty">未配置绘图渠道，当前会使用兼容单接口模式。</p>';
  renderDailyUsage("overview-usage", dailyStats);
}

function renderDailyUsage(targetId, dailyStats) {
  const target = byId(targetId);
  const stats = dailyStats && typeof dailyStats === "object" ? dailyStats : {};
  const labels = state.usage?.identity_labels || {};
  const toRows = (records, kind) => Object.entries(records && typeof records === "object" ? records : {})
    .map(([id, count]) => ({
      id,
      kind,
      name: kind === "群" ? labels.groups?.[id] : labels.users?.[id],
      count: Number(count || 0),
    }))
    .filter((row) => row.id && row.count > 0);
  const rows = [...toRows(stats.groups, "群"), ...toRows(stats.users, "用户")]
    .sort((left, right) => right.count - left.count || left.kind.localeCompare(right.kind) || left.id.localeCompare(right.id));
  if (!rows.length) {
    target.innerHTML = '<p class="empty">暂无用量记录。</p>';
    return;
  }
  target.innerHTML = `<table><thead><tr><th>类型</th><th>名称 / ID</th><th>今日成功数</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(row.kind)}</td><td>${escapeHtml(formatIdentity(row.name, row.id, row.kind))}</td><td>${escapeHtml(row.count)}</td></tr>`).join("")}</tbody></table>`;
}

function formatIdentity(name, id, kind) {
  const displayName = String(name || "").trim();
  const identityId = String(id || "").trim();
  const idLabel = kind === "群" ? "群号" : "QQ号";
  if (!identityId) return displayName || "未知";
  return displayName ? `${displayName}（${idLabel} ${identityId}）` : `${idLabel} ${identityId}`;
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
  target.innerHTML = `<table><thead><tr><th>类型</th><th>名称 / ID</th><th>额度</th><th>签到</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(row.kind)}</td><td>${escapeHtml(formatIdentity(row.name, row.id, row.kind))}</td><td>${escapeHtml(row.credits)}</td><td>${escapeHtml(row.checked_in || "-")}</td></tr>`).join("")}</tbody></table>`;
}

function formatBytes(rawBytes) {
  const bytes = Math.max(0, Number(rawBytes) || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

function formatHistoryTime(rawValue) {
  const date = new Date(rawValue || "");
  if (Number.isNaN(date.getTime())) return rawValue || "时间未知";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function renderGenerationHistory() {
  const history = state.history || {};
  const summary = history.summary || {};
  const pagination = history.pagination || {};
  const records = history.records || [];
  const retentionDays = Math.max(1, Number(history.retention_days) || 7);
  const favoriteOnly = bool(history.favorite_only);
  state.historyFavoriteOnly = favoriteOnly;

  byId("history-total").textContent = String(summary.total ?? pagination.total ?? 0);
  byId("history-today").textContent = String(summary.today ?? 0);
  byId("history-protected").textContent = String(summary.protected ?? 0);
  byId("history-size").textContent = formatBytes(summary.size_bytes);
  byId("history-retention").textContent = String(retentionDays);
  byId("history-detail-summary").textContent = [
    `覆盖 ${Number(summary.users) || 0} 位用户`,
    `${Number(summary.groups) || 0} 个群`,
    `${Number(summary.private) || 0} 条私聊记录`,
    `收藏 ${Number(summary.favorite) || 0} 条`,
    `锁定 ${Number(summary.locked) || 0} 条`,
  ].join(" · ");
  const favoritesButton = byId("history-favorites");
  favoritesButton.textContent = favoriteOnly ? "显示全部" : `只看收藏（${Number(summary.favorite) || 0}）`;
  favoritesButton.setAttribute("aria-pressed", String(favoriteOnly));

  const target = byId("generation-history-list");
  if (!records.length) {
    target.innerHTML = '<p class="empty">暂无成功记录。后续每张成功返回的图片会自动缓存并显示在这里。</p>';
  } else {
    target.innerHTML = records.map((record) => {
      const id = String(record.id || "");
      const isFavorite = bool(record.favorite);
      const isLocked = bool(record.locked);
      const preview = typeof record.preview === "string" ? record.preview : "";
      const prompt = String(record.prompt || "");
      const user = formatIdentity(record.user_name, record.user_id, "用户");
      const owner = record.group_id ? formatIdentity(record.group_name, record.group_id, "群") : "私聊";
      const dimension = record.width && record.height ? `${record.width} × ${record.height}` : "尺寸未知";
      const model = record.model || "未记录模型";
      const preset = record.preset || "无预设";
      const taskType = record.task_type || "成功图片";
      const protection = [isFavorite ? "已收藏" : "", isLocked ? "已锁定" : ""].filter(Boolean);
      const image = preview
        ? `<img src="${escapeHtml(preview)}" alt="成功图片预览" loading="lazy" />`
        : `<span class="generation-preview-fallback">${record.image_available ? "预览不可用" : "缓存图片不可用"}</span>`;
      return `
        <article class="generation-record${isFavorite || isLocked ? " protected" : ""}">
          <div class="generation-preview">${image}</div>
          <div class="generation-record-main">
            <div class="generation-record-topline">
              <div class="generation-record-title"><strong>${escapeHtml(taskType)}</strong><span>${escapeHtml(formatHistoryTime(record.created_at))}</span></div>
              <div class="generation-record-actions">
                <button type="button" class="history-icon-button download" data-history-download="${escapeHtml(id)}" title="下载原图" aria-label="下载原图" ${record.image_available ? "" : "disabled"}>↓</button>
                <button type="button" class="history-icon-button${isFavorite ? " active" : ""}" data-history-favorite="${escapeHtml(id)}" data-history-value="${String(!isFavorite)}" title="${isFavorite ? "取消收藏" : "收藏并永久保留"}" aria-label="${isFavorite ? "取消收藏" : "收藏并永久保留"}" aria-pressed="${String(isFavorite)}">★</button>
                <button type="button" class="history-icon-button${isLocked ? " active locked" : ""}" data-history-lock="${escapeHtml(id)}" data-history-value="${String(!isLocked)}" title="${isLocked ? "解除锁定" : "锁定并永久保留"}" aria-label="${isLocked ? "解除锁定" : "锁定并永久保留"}" aria-pressed="${String(isLocked)}">▣</button>
                <button type="button" class="history-icon-button danger" data-history-delete="${escapeHtml(id)}" title="删除成功记录" aria-label="删除成功记录">×</button>
              </div>
            </div>
            <div class="generation-meta">
              <span>用户 ${escapeHtml(user)}</span><span>${escapeHtml(owner)}</span><span>${escapeHtml(dimension)}</span><span>${escapeHtml(formatBytes(record.size_bytes))}</span><span>${escapeHtml(model)}</span><span>${escapeHtml(preset)}</span>
            </div>
            ${protection.length ? `<div class="generation-protection">${protection.map((label) => `<span>${label}</span>`).join("")}</div>` : ""}
            <details class="generation-prompt"><summary>请求提示词</summary><pre>${escapeHtml(prompt || "未记录提示词")}</pre></details>
          </div>
        </article>`;
    }).join("");
  }

  const total = Math.max(0, Number(pagination.total) || 0);
  const limit = Math.max(1, Number(pagination.limit) || HISTORY_PAGE_SIZE);
  const offset = Math.max(0, Number(pagination.offset) || 0);
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  const controls = byId("generation-history-pagination");
  controls.hidden = total <= limit;
  byId("history-page-status").textContent = `第 ${page} / ${pages} 页，共 ${total} 条`;
  byId("history-prev").disabled = offset <= 0;
  byId("history-next").disabled = offset + limit >= total;
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
  byId("custom-negative-prompt").value = tools.custom_drawing_negative_prompt || "";
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
  byId("generation-cache-retention").value = settings.generation_cache_retention_days ?? 7;
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
  return images.map((url, index) => {
    const source = typeof url === "string" ? url : "";
    const failed = !source;
    const preview = source
      ? `<img src="${escapeHtml(source)}" alt="参考图 ${index + 1}" loading="lazy" />`
      : "";
    return `<div class="image-tile${failed ? " failed" : ""}">${preview}<span class="image-index">${index + 1}</span><span class="image-failure">预览不可用</span><button type="button" data-delete-reference="${escapeHtml(preset)}" data-index="${index}" title="删除参考图" aria-label="删除参考图">×</button></div>`;
  }).join("");
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
      generation_cache_retention_days: Number(value("generation-cache-retention")) || 7,
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
      custom_drawing_negative_prompt: value("custom-negative-prompt").trim(),
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

async function loadGenerationHistory(offset = 0) {
  const safeOffset = Math.max(0, Number(offset) || 0);
  const response = await bridge.apiGet("generation_history", {
    limit: HISTORY_PAGE_SIZE,
    offset: safeOffset,
    favorite_only: state.historyFavoriteOnly ? "1" : "0",
  });
  if (!response?.success) throw new Error(response?.message || "无法读取成功记录");
  state.history = response;
  renderGenerationHistory();
}

async function updateGenerationRecord(action, id, nextValue) {
  const response = await bridge.apiPost("generation_record", {
    action,
    id,
    value: nextValue,
  });
  if (!response?.success) throw new Error(response?.message || "成功记录操作失败");
  showToast(response.message || "成功记录已更新");
  const mustRestartFavoritePage = state.historyFavoriteOnly && action === "favorite" && !nextValue;
  await loadGenerationHistory(mustRestartFavoritePage ? 0 : (state.history?.pagination?.offset || 0));
}

async function downloadGenerationImage(id) {
  const recordId = String(id || "").trim();
  if (!recordId) throw new Error("缺少成功记录 ID");
  if (typeof bridge.download === "function") {
    await bridge.download("generation_download", { id: recordId }, `linghui_${recordId}`);
    return;
  }
  const response = await fetch(`${PLUGIN_API_BASE}/generation_download?id=${encodeURIComponent(recordId)}`, {
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload?.message || "原图下载失败");
  }
  const image = await response.blob();
  if (!image.size) throw new Error("原图缓存为空");
  const header = response.headers.get("content-disposition") || "";
  const matchedName = /filename="?([^";]+)"?/i.exec(header);
  const filename = matchedName?.[1] || `linghui_${recordId}.png`;
  const url = URL.createObjectURL(image);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

async function deleteGenerationRecord(id) {
  if (!await confirmAction({
    title: "删除成功记录",
    message: "确认删除这条成功记录吗？即使已收藏或锁定，手动删除也会移除图片和提示词。",
    confirmLabel: "删除记录",
  })) return;
  const response = await bridge.apiPost("generation_record", { action: "delete", id });
  if (!response?.success) throw new Error(response?.message || "删除成功记录失败");
  showToast(response.message || "成功记录已删除");
  await loadGenerationHistory(0);
}

async function cleanupGenerationHistory() {
  if (!await confirmAction({
    title: "清理过期缓存",
    message: "确认清理已过期的成功记录吗？已收藏或锁定的图片和提示词会保留。",
    confirmLabel: "清理缓存",
  })) return;
  const response = await bridge.apiPost("generation_record", { action: "cleanup" });
  if (!response?.success) throw new Error(response?.message || "清理缓存失败");
  showToast(response.message || "过期缓存已清理");
  await loadGenerationHistory(0);
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
  if (reset && !await confirmAction({
    title: "重置额度",
    message: `确认重置 ${id} 的额度和签到状态吗？`,
    confirmLabel: "重置额度",
  })) return;
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
  if (!preset || !await confirmAction({
    title: "删除参考图",
    message: "确认删除这张参考图吗？删除后无法恢复。",
    confirmLabel: "删除图片",
  })) return;
  const response = await bridge.apiPost("reference", { action: "delete", preset, index: Number(index) });
  if (!response?.success) throw new Error(response?.message || "删除失败");
  showToast("参考图已删除");
  await loadConfig();
}

async function clearReference(preset) {
  if (!preset || !await confirmAction({
    title: "清空参考图",
    message: "确认清空该集合中的全部参考图吗？删除后无法恢复。",
    confirmLabel: "清空全部",
  })) return;
  const response = await bridge.apiPost("reference", { action: "clear", preset });
  if (!response?.success) throw new Error(response?.message || "清空失败");
  showToast(response.message || "参考图已清空");
  await loadConfig();
}

function switchTab(tab) {
  const contentTab = tab === "favorites" ? "history" : tab;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  document.querySelectorAll(".tab-pane").forEach((pane) => pane.classList.toggle("active", pane.id === `tab-${contentTab}`));
  const [title, subtitle] = titles[tab] || titles.overview;
  byId("page-title").textContent = title;
  byId("page-subtitle").textContent = subtitle;
}

document.addEventListener("click", async (event) => {
  try {
    const tab = event.target.closest("[data-tab]");
    if (tab) {
      const tabName = tab.dataset.tab;
      if (tabName === "favorites") state.historyFavoriteOnly = true;
      if (tabName === "history") state.historyFavoriteOnly = false;
      switchTab(tabName);
      if (tabName === "history" || tabName === "favorites") await loadGenerationHistory(0);
      return;
    }
    if (event.target.closest("#save-button")) { await saveConfig(); return; }
    if (event.target.closest("#reload-button")) { await loadConfig(); return; }
    if (event.target.closest("#refresh-usage")) { await loadUsage(); showToast("用量已刷新"); return; }
    if (event.target.closest("#history-refresh")) { await loadGenerationHistory(state.history?.pagination?.offset || 0); showToast("成功记录已刷新"); return; }
    if (event.target.closest("#history-favorites")) {
      state.historyFavoriteOnly = !state.historyFavoriteOnly;
      switchTab(state.historyFavoriteOnly ? "favorites" : "history");
      await loadGenerationHistory(0);
      return;
    }
    if (event.target.closest("#history-cleanup")) { await cleanupGenerationHistory(); return; }
    if (event.target.closest("#history-prev")) {
      const offset = Math.max(0, Number(state.history?.pagination?.offset) || 0);
      await loadGenerationHistory(Math.max(0, offset - HISTORY_PAGE_SIZE));
      return;
    }
    if (event.target.closest("#history-next")) {
      const offset = Math.max(0, Number(state.history?.pagination?.offset) || 0);
      await loadGenerationHistory(offset + HISTORY_PAGE_SIZE);
      return;
    }
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
    if (event.target.closest("#reference-clear")) { await clearReference(value("reference-preset")); return; }
    const downloadButton = event.target.closest("[data-history-download]");
    if (downloadButton) { await downloadGenerationImage(downloadButton.dataset.historyDownload); return; }
    const favoriteButton = event.target.closest("[data-history-favorite]");
    if (favoriteButton) {
      await updateGenerationRecord("favorite", favoriteButton.dataset.historyFavorite, bool(favoriteButton.dataset.historyValue));
      return;
    }
    const lockButton = event.target.closest("[data-history-lock]");
    if (lockButton) {
      await updateGenerationRecord("lock", lockButton.dataset.historyLock, bool(lockButton.dataset.historyValue));
      return;
    }
    const deleteHistoryButton = event.target.closest("[data-history-delete]");
    if (deleteHistoryButton) { await deleteGenerationRecord(deleteHistoryButton.dataset.historyDelete); }
  } catch (error) {
    console.error(error);
    showToast(error.message || "操作失败", true);
    setSaveState("操作失败");
  }
});

document.addEventListener("error", (event) => {
  const image = event.target;
  if (!(image instanceof HTMLImageElement)) return;
  if (image.matches(".image-tile img")) {
    const tile = image.closest(".image-tile");
    tile?.classList.add("failed");
    image.alt = "参考图预览不可用";
    return;
  }
  if (image.matches(".generation-preview img")) {
    const preview = image.closest(".generation-preview");
    preview?.classList.add("failed");
    image.alt = "成功图片预览不可用";
  }
}, true);

byId("reference-preset").addEventListener("change", renderReferenceImages);
byId("theme-select").addEventListener("change", (event) => setTheme(event.target.value));
byId("persona-upload").addEventListener("change", async (event) => {
  try { await uploadReferences(event.target.files, "_persona_"); event.target.value = ""; } catch (error) { showToast(error.message || "上传失败", true); }
});
byId("reference-upload").addEventListener("change", async (event) => {
  try { await uploadReferences(event.target.files, value("reference-preset")); event.target.value = ""; } catch (error) { showToast(error.message || "上传失败", true); }
});

setTheme(storedTheme(), false);

try {
  await bridge.ready();
  await loadConfig();
} catch (error) {
  console.error(error);
  showToast(error.message || "页面初始化失败", true);
  setSaveState("加载失败");
}
