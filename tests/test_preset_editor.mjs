import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../pages/linghui-studio/preset-editor.js", import.meta.url), "utf8");
const { capturePresetDrafts, createPresetDialog, movePresetChannel, normalizePresetGenerationOptions, presetChannelIds, presetChannelOptions, presetChannelPicker, presetGenerationFields, sortedPresetEntries, validateNewPreset } = await import(`data:text/javascript;base64,${Buffer.from(`${source}\n//# sourceURL=linghui-preset-editor.js`).toString("base64")}`);
const defaultImageOptions = { image_output_format: "auto", image_frame_count: 9, image_frame_duration_ms: 200, image_complexity: "auto" };

test("groups custom/modified/default and sorts Chinese names and numbers naturally", () => {
  const presets = [
    { name: "自带10", source: "default" }, { name: "自带2", source: "default" },
    { name: "猫咪", source: "custom" }, { name: "白猫", source: "custom" },
    { name: "修改1", source: "modified" },
  ];
  assert.deepEqual(sortedPresetEntries(presets).map(({ index }) => index), [3, 2, 4, 1, 0]);
  assert.equal(presets[0].name, "自带10");
  const ascending = sortedPresetEntries(presets, "name_asc").map(({ index }) => index);
  assert.deepEqual(sortedPresetEntries(presets, "name_desc").map(({ index }) => index), [...ascending].reverse());
});

test("sorted and hidden rows preserve edits by original index, including whitespace", () => {
  const presets = [{ name: "first", prompt: "old1" }, { name: "second", prompt: "old2" }];
  const row = (index, name, prompt) => ({ dataset: { presetIndex: String(index) }, querySelector: (selector) => ({ '[data-preset="name"]': { value: name }, '[data-preset="prompt"]': { value: prompt } })[selector] || null });
  capturePresetDrafts(presets, { querySelectorAll: () => [row(1, "second edited", "\nnew2\n"), row(0, "first", "new1")] });
  assert.deepEqual(presets, [{ name: "first", prompt: "new1" }, { name: "second edited", prompt: "\nnew2\n" }]);
});

test("new preset validation rejects incomplete, duplicate and invalid names", () => {
  for (const [name, prompt] of [["", "text"], ["猫", ""], ["猫:白", "text"], ["猫：白", "text"], ["猫\n白", "text"], ["a".repeat(49), "text"], ["猫", "text"]]) {
    assert.throws(() => validateNewPreset(name, prompt, [{ name: "猫" }]));
  }
  assert.deepEqual(validateNewPreset(" 新预设 ", " \nfirst\nsecond \n"), { name: "新预设", prompt: "first\nsecond" });
});

test("channel selection preserves missing targets visibly and escapes provider names", () => {
  const markup = presetChannelOptions([{ id: "gif", name: '<GIF "channel">' }, { id: "off", enabled: false }], "missing");
  assert.match(markup, /value="missing" selected>不可用/);
  assert.match(markup, /&lt;GIF &quot;channel&quot;&gt;/);
  assert.match(markup, /value="off"\s+disabled/);
  assert.match(markup, /跟随会话/);
});

test("draft channel and original preset identity survive prompt and name edits", () => {
  const presets = [{ name: "old", original_name: "old", prompt: "first", channel_id: "gif" }];
  const values = { name: "new", prompt: "updated", channel_id: "other" };
  capturePresetDrafts(presets, { querySelectorAll: () => [{ dataset: { presetIndex: "0" },
    querySelector: (selector) => {
      const key = selector.match(/data-preset="(.+)"/)?.[1];
      return key in values ? { value: values[key] } : null;
    },
  }] });
  assert.deepEqual(presets, [{ name: "new", original_name: "old", prompt: "updated", channel_id: "other" }]);
});

class Element {
  events = {}; value = ""; disabled = false; hidden = false; isConnected = true; open = false;
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  async emit(type, event = {}) { for (const handler of this.events[type] || []) await handler({ preventDefault() {}, ...event }); }
  setAttribute(key, value) { this[key] = value; }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; void this.emit("close"); }
  querySelectorAll(selector) { return selector === "[data-preset-channel]:checked" ? (this.selectedChannels || []).map((value) => ({ value })) : []; }
}

function fixture(createPreset = async (preset) => ({ success: true, preset: { ...preset, source: "custom" } })) {
  const nodes = new Map();
  const node = (name) => { if (!nodes.has(name)) nodes.set(name, new Element()); return nodes.get(name); };
  const previous = globalThis.document;
  const opener = new Element();
  globalThis.document = { getElementById: (id) => node(id.replace("preset-create-", "")), activeElement: opener };
  const presets = [{ name: "旧预设", prompt: "old" }];
  const calls = [];
  const dialog = createPresetDialog({ getPresets: () => presets, createPreset: async (preset) => { calls.push(preset); return createPreset(preset); }, onCreated: (preset) => { presets.push(preset); } });
  return { dialog, node, presets, calls, opener, restore: () => { globalThis.document = previous; } };
}

test("opening and cancelling creates no blank row or server request", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    assert.equal(f.node("name").focused, true);
    await f.node("cancel").emit("click");
    assert.equal(f.node("dialog").open, false);
    assert.equal(f.presets.length, 1);
    assert.deepEqual(f.calls, []);
    assert.equal(f.opener.focused, true);
  } finally { f.restore(); }
});

test("invalid form stays open with inline error and no saved preset", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    f.node("name").value = "旧预设"; f.node("prompt").value = "text";
    await f.node("form").emit("submit");
    assert.equal(f.node("dialog").open, true);
    assert.match(f.node("error").textContent, /已存在/);
    assert.equal(f.node("error").hidden, false);
    assert.equal(f.calls.length, 0);
  } finally { f.restore(); }
});

test("one confirmation saves, inserts only the successful preset and closes", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    f.node("name").value = " 新预设 "; f.node("prompt").value = "new prompt";
    await f.node("form").emit("submit");
    assert.deepEqual(f.calls, [{ name: "新预设", prompt: "new prompt", channel_ids: [], channel_id: "", ...defaultImageOptions }]);
    assert.equal(f.presets.length, 2);
    assert.equal(f.node("dialog").open, false);
    assert.equal(f.node("submit").disabled, false);
  } finally { f.restore(); }
});

test("creating a GIF preset submits the selected existing channels in order and animation settings", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    f.node("name").value = "GIF 预设"; f.node("prompt").value = "animate";
    f.node("channel").selectedChannels = ["miku", "orb"];
    f.node("output-format").value = "gif";
    f.node("frame-count").value = "12";
    f.node("frame-duration-ms").value = "170";
    await f.node("form").emit("submit");
    assert.deepEqual(f.calls, [{ name: "GIF 预设", prompt: "animate", channel_ids: ["miku", "orb"], channel_id: "miku", ...defaultImageOptions, image_output_format: "gif", image_frame_count: 12, image_frame_duration_ms: 170 }]);
  } finally { f.restore(); }
});

test("save failure preserves typed content and permits retry", async () => {
  let fail = true;
  const f = fixture(async () => fail ? { success: false, message: "保存失败" } : { success: true });
  try {
    f.dialog.open();
    f.node("name").value = "新预设"; f.node("prompt").value = "user content";
    await f.node("form").emit("submit");
    assert.equal(f.node("dialog").open, true);
    assert.equal(f.node("prompt").value, "user content");
    assert.equal(f.presets.length, 1);
    fail = false;
    await f.node("form").emit("submit");
    assert.equal(f.presets.length, 2);
  } finally { f.restore(); }
});

test("pending save blocks duplicate submission and cancelling", async () => {
  let resolve;
  const f = fixture(() => new Promise((done) => { resolve = done; }));
  try {
    f.dialog.open();
    f.node("name").value = "新预设"; f.node("prompt").value = "text";
    const pending = f.node("form").emit("submit");
    await f.node("form").emit("submit");
    await f.node("cancel").emit("click");
    let blocked = false;
    await f.node("dialog").emit("cancel", { preventDefault: () => { blocked = true; } });
    assert.equal(blocked, true);
    assert.equal(f.calls.length, 1);
    assert.equal(f.node("output-format").disabled, true);
    assert.equal(f.node("frame-count").disabled, true);
    assert.equal(f.node("channel").inert, true);
    assert.equal(f.node("dialog").open, true);
    resolve({ success: true }); await pending;
    assert.equal(f.presets.length, 2);
  } finally { f.restore(); }
});

test("GIF controls appear only for GIF and an invalid duration focuses the field without posting", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    assert.equal(f.node("animation").hidden, true);
    f.node("output-format").value = "gif";
    await f.node("output-format").emit("change");
    assert.equal(f.node("animation").hidden, false);
    f.node("name").value = "动图画"; f.node("prompt").value = "animate";
    f.node("frame-duration-ms").value = "167";
    await f.node("form").emit("submit");
    assert.equal(f.calls.length, 0);
    assert.match(f.node("error").textContent, /10 的倍数/);
    assert.equal(f.node("frame-duration-ms").focused, true);
    f.node("output-format").value = "auto";
    await f.node("output-format").emit("change");
    assert.equal(f.node("animation").hidden, true);
  } finally { f.restore(); }
});

test("new and legacy channel identities preserve deliberate unbinding and reorder without mutating the source", () => {
  assert.deepEqual(presetChannelIds({ channel_id: "miku" }), ["miku"]);
  assert.deepEqual(presetChannelIds({ channel_id: "miku", channel_ids: [] }), []);
  const selected = ["miku", "orb"];
  assert.deepEqual(movePresetChannel(selected, "orb", "up"), ["orb", "miku"]);
  assert.deepEqual(movePresetChannel(selected, "miku", "down"), ["orb", "miku"]);
  assert.deepEqual(movePresetChannel(selected, "miku", "up"), selected);
  assert.deepEqual(movePresetChannel(selected, "missing", "up"), selected);
  assert.deepEqual(selected, ["miku", "orb"]);
});

test("ordered picker shows primary/fallback, unavailable chosen targets and escaped channel names", () => {
  const markup = presetChannelPicker([{ id: "orb", name: '<orb "GIF">' }, { id: "miku" }, { id: "off", enabled: false }], ["miku", "missing", "orb"]);
  assert.ok(markup.indexOf('value="miku"') < markup.indexOf('value="missing"'));
  assert.ok(markup.indexOf('value="missing"') < markup.indexOf('value="orb"'));
  assert.match(markup, /主用 · miku/);
  assert.match(markup, /备用 1 · missing（不可用/);
  assert.match(markup, /&lt;orb &quot;GIF&quot;&gt;/);
  assert.match(markup, /value="off"\s+disabled/);
  assert.match(markup, /只在所选渠道间回退/);
});

test("renaming and filtered drafts retain channel order and GIF options until save", () => {
  const presets = [{ name: "动图画", original_name: "动图画", prompt: "old", channel_id: "miku", channel_ids: ["miku", "orb"], ...defaultImageOptions }];
  const fields = { name: "动图画2", prompt: "new", image_output_format: "gif", image_frame_count: "12", image_frame_duration_ms: "170", image_complexity: "medium" };
  const picker = { querySelectorAll: () => [{ value: "orb" }, { value: "miku" }] };
  capturePresetDrafts(presets, { querySelectorAll: () => [{ dataset: { presetIndex: "0" }, querySelector: (selector) => {
    if (selector === "[data-preset-channel-picker]") return picker;
    const key = selector.match(/data-preset="(.+)"/)?.[1];
    return key in fields ? { value: fields[key] } : null;
  } }] });
  assert.deepEqual(presets[0].channel_ids, ["orb", "miku"]);
  assert.equal(presets[0].channel_id, "orb");
  assert.equal(presets[0].original_name, "动图画");
  assert.equal(presets[0].name, "动图画2");
  assert.deepEqual(normalizePresetGenerationOptions(presets[0]), { image_output_format: "gif", image_frame_count: 12, image_frame_duration_ms: 170, image_complexity: "medium" });
});

test("animation option validation covers count, duration, types and static defaults", () => {
  assert.deepEqual(normalizePresetGenerationOptions(), defaultImageOptions);
  for (const image_frame_count of ["", 1, 17, 2.5, "bad"]) assert.throws(() => normalizePresetGenerationOptions({ image_output_format: "gif", image_frame_count }));
  for (const image_frame_duration_ms of ["", 19, 2001, 167, "bad"]) assert.throws(() => normalizePresetGenerationOptions({ image_output_format: "gif", image_frame_duration_ms }));
  assert.throws(() => normalizePresetGenerationOptions({ image_output_format: "svg" }));
  assert.throws(() => normalizePresetGenerationOptions({ image_complexity: "bad" }));
});

test("rendering an incomplete GIF draft remains editable and escapes injected attributes", () => {
  const markup = presetGenerationFields({ image_output_format: "gif", image_frame_count: "", image_frame_duration_ms: '\" onfocus=\"bad' });
  assert.match(markup, /value="gif" selected/);
  assert.match(markup, /&quot; onfocus=&quot;bad/);
  assert.doesNotMatch(markup, /value="" onfocus/);
  assert.match(presetGenerationFields(), />跟随渠道<\/option>/);
});
