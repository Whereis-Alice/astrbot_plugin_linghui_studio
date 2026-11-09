import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../pages/linghui-studio/preset-editor.js", import.meta.url), "utf8");
const { capturePresetDrafts, createPresetDialog, presetChannelOptions, sortedPresetEntries, validateNewPreset } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

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
  const row = (index, name, prompt) => ({ dataset: { presetIndex: String(index) }, querySelector: (selector) => selector.includes('"channel_id"') ? null : ({ value: selector.includes('"name"') ? name : prompt }) });
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
    querySelector: (selector) => ({ value: values[selector.match(/"(.+)"/)[1]] }),
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
    assert.deepEqual(f.calls, [{ name: "新预设", prompt: "new prompt" }]);
    assert.equal(f.presets.length, 2);
    assert.equal(f.node("dialog").open, false);
    assert.equal(f.node("submit").disabled, false);
  } finally { f.restore(); }
});

test("creating a preset submits the selected channel", async () => {
  const f = fixture();
  try {
    f.dialog.open();
    f.node("name").value = "GIF 预设"; f.node("prompt").value = "animate";
    f.node("channel").value = "gif";
    await f.node("form").emit("submit");
    assert.deepEqual(f.calls, [{ name: "GIF 预设", prompt: "animate", channel_id: "gif" }]);
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
    assert.equal(f.node("dialog").open, true);
    resolve({ success: true }); await pending;
    assert.equal(f.presets.length, 2);
  } finally { f.restore(); }
});
