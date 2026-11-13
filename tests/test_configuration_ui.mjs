import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const app = await readFile(new URL("../pages/linghui-studio/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../pages/linghui-studio/index.html", import.meta.url), "utf8");
const presetSource = await readFile(new URL("../pages/linghui-studio/preset-editor.js", import.meta.url), "utf8");
const { selectedPresetChannels, presetChannelPicker, movePresetChannel } = await import(`data:text/javascript;base64,${Buffer.from(presetSource).toString("base64")}`);

function functionSource(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const following = app.slice(start + 1).search(/\n(?:async )?function /);
  assert.ok(following >= 0);
  return app.slice(start, start + 1 + following).trim();
}

const removedIds = ["default-model", "default-t2i-model", "resolution", "image-quality", "image-output-format", "image-frame-count", "image-frame-duration-ms", "image-complexity", "aspect-ratio", "default-timeout", "enable-binary-image-response", "enable-bare-base64-response", "stream-heartbeat-tolerant"];
const removedKeys = ["model", "text_to_image_model", "image_resolution", "image_quality", "image_output_format", "image_frame_count", "image_frame_duration_ms", "image_complexity", "image_aspect_ratio", "timeout", "enable_binary_image_response", "enable_bare_base64_response", "stream_heartbeat_tolerant"];

test("the settings page removes obsolete inputs and offers one ordered selfie selection", () => {
  for (const id of removedIds) {
    assert.ok(!html.includes(`id="${id}"`), id);
    assert.ok(!app.includes(`byId("${id}")`), id);
  }
  assert.equal((html.match(/id="persona-channel-picker"/g) || []).length, 1);
  assert.ok(!html.includes("兼容单接口"));
  assert.ok(!app.includes("兼容单接口"));
  for (const heading of ["命令与冷却", "QQ 图片发送", "结果与失败提示", "批量任务", "记录缓存"]) assert.ok(html.includes(`<h2>${heading}</h2>`));
});

test("selfie choices include only enabled reference-capable channels without mutating them", () => {
  const candidates = new Function(`return (${functionSource("personaChannelCandidates")});`)();
  const channels = [{ id: "orb" }, { id: "text", reference_image_enabled: false }, { id: "disabled", enabled: false }, { id: "miku", enabled: true, reference_image_enabled: true }];
  assert.deepEqual(candidates(channels).map((channel) => channel.id), ["orb", "miku"]);
  assert.equal(channels.length, 4);
  const picker = presetChannelPicker(candidates(channels), ["miku", "orb"]);
  assert.ok(picker.indexOf('value="miku"') < picker.indexOf('value="orb"'));
  assert.ok(!picker.includes('value="text"'));
  assert.ok(!picker.includes('value="disabled"'));
});

test("saving preserves selected selfie order and emits no retired top-level settings", () => {
  let selection = ["miku", "orb"];
  const picker = { querySelectorAll: () => selection.map((value) => ({ value })) };
  const channels = [{ id: "orb", model: "channel-model", image_resolution: "1K" }];
  const presets = [{ name: "动图画", channel_ids: ["miku", "orb"], image_output_format: "gif" }];
  const dependencies = {
    checked: () => false,
    value: (_id, fallback = "") => fallback,
    byId: (id) => { assert.equal(id, "persona-channel-picker"); return picker; },
    selectedPresetChannels,
    readChannels: () => channels,
    readPresets: () => presets,
    idsFromText: () => [],
    linesFromText: () => [],
  };
  const build = new Function(...Object.keys(dependencies), `return (${functionSource("buildPayload")});`)(...Object.values(dependencies));
  let result = build();
  assert.deepEqual(result.settings.persona_channel_ids, ["miku", "orb"]);
  for (const key of removedKeys) assert.ok(!(key in result.settings), key);
  assert.equal(result.channels, channels);
  assert.equal(result.presets, presets);
  selection = movePresetChannel(selection, "orb", "up");
  assert.deepEqual(build().settings.persona_channel_ids, ["orb", "miku"]);
  selection = [];
  assert.deepEqual(build().settings.persona_channel_ids, []);
});

test("configuration hydration uses current form IDs and restores ordered selfie routes", () => {
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
  const nodes = new Map();
  const state = { config: { settings: { persona_channel_ids: ["orb", "miku"] } } };
  let restored;
  const noOp = () => {};
  const dependencies = {
    state, bool: Boolean,
    byId: (id) => { assert.ok(ids.has(id), `hydration references absent input ${id}`); if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    setListValue: noOp,
    renderChannels: noOp,
    renderPersonaChannels: (selection) => { restored = selection; },
    renderPresets: noOp, renderReferenceSelector: noOp, renderPersonaImages: noOp,
    renderReferenceImages: noOp, renderRouteHealth: noOp, renderStudio: noOp,
    renderPersonaState: noOp, renderStudioChannelOptions: noOp,
  };
  const hydrate = new Function(...Object.keys(dependencies), `return (${functionSource("hydrateFields")});`)(...Object.values(dependencies));
  hydrate();
  assert.deepEqual(restored, ["orb", "miku"]);
});
