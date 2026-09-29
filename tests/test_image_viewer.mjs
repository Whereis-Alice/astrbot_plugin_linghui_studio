// No browser dependencies: regression tests for async ownership and resources.
// Run: node --test tests/test_image_viewer.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../pages/linghui-studio/image-viewer.js", import.meta.url), "utf8");
const { createImageViewer } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { resolve, promise }; };
const success = { success: true, image: "data:image/png;base64,AA==", original_width: 1600, original_height: 1000, preview_only: false };

class Element {
  constructor() {
    this.events = {};
    this.style = {};
    this.attrs = {};
    this.dataset = {};
    this.classList = { add() {}, remove() {} };
    this.clientWidth = 832;
    this.clientHeight = 632;
    this.scrollLeft = this.scrollTop = 0;
    this.isConnected = true;
    this.hidden = false;
    this.disabled = false;
  }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  async emit(type, event = {}) { for (const handler of this.events[type] || []) await handler(event); }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this[key]; }
  focus(options) { this.focusOptions = options; }
  showModal() { this.open = true; }
  close() { this.open = false; return this.emit("close"); }
  closest() { return this; }
  setPointerCapture() {}
}

function fixture(loadImage = async () => success, decode = async () => {}) {
  const nodes = new Map();
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const dialog = node("image-viewer");
  const buttons = new Map(["close", "fit", "actual", "in", "out", "retry", "download"].map((action) => {
    const button = new Element(); button.dataset.viewerAction = action; return [action, button];
  }));
  dialog.querySelector = (selector) => buttons.get(/="(.*?)"/.exec(selector)[1]);
  dialog.querySelectorAll = () => ["fit", "actual", "in", "out"].map((key) => buttons.get(key));
  const opener = new Element();
  const original = { document: globalThis.document, Image: globalThis.Image, ResizeObserver: globalThis.ResizeObserver, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  const created = [];
  const revoked = [];
  let resize;
  globalThis.document = { getElementById: node, activeElement: opener };
  globalThis.Image = class { naturalWidth = 1600; naturalHeight = 1000; decode = decode; };
  globalThis.ResizeObserver = class { constructor(fn) { resize = fn; } observe() {} };
  URL.createObjectURL = () => { const url = `blob:test-${created.length}`; created.push(url); return url; };
  URL.revokeObjectURL = (url) => revoked.push(url);
  const downloads = [];
  const viewer = createImageViewer({ loadImage, downloadImage: async (...args) => { downloads.push(args); } });
  return {
    node, dialog, viewer, created, revoked, opener, downloads, buttons,
    resize: () => resize(),
    click: (action) => dialog.emit("click", { target: buttons.get(action) }),
    restore() { globalThis.document = original.document; globalThis.Image = original.Image; globalThis.ResizeObserver = original.ResizeObserver; URL.createObjectURL = original.create; URL.revokeObjectURL = original.revoke; },
  };
}

test("loads only when opened; fit, actual pixels, zoom and cleanup", async () => {
  let calls = 0;
  const f = fixture(async () => { ++calls; return success; });
  try {
    assert.equal(calls, 0);
    await f.viewer.open("result");
    assert.equal(calls, 1);
    assert.equal(f.node("image-viewer-image").style.width, "800px");
    assert.equal(f.node("image-viewer-scale").textContent, "50%");
    await f.click("actual");
    assert.equal(f.node("image-viewer-image").style.width, "1600px");
    assert.equal(f.node("image-viewer-viewport").scrollLeft, 400);
    assert.equal(f.node("image-viewer-viewport").scrollTop, 200);
    await f.click("in");
    assert.equal(f.node("image-viewer-scale").textContent, "125%");
    await f.click("close");
    assert.equal(f.node("image-viewer-image").hidden, true);
    assert.deepEqual(f.revoked, f.created);
    assert.deepEqual(f.opener.focusOptions, { preventScroll: true });
  } finally { f.restore(); }
});

test("closing before a response never decodes or resurrects the image", async () => {
  const pending = deferred();
  const f = fixture(() => pending.promise);
  try {
    const request = f.viewer.open("old");
    await f.click("close");
    pending.resolve(success);
    await request;
    assert.deepEqual(f.created, []);
    assert.equal(f.dialog.open, false);
  } finally { f.restore(); }
});

test("older response cannot overwrite a newly opened source image", async () => {
  const old = deferred();
  const args = [];
  const f = fixture((id, index) => { args.push([id, index]); return id === "old" ? old.promise : success; });
  try {
    const oldRequest = f.viewer.open("old");
    await f.viewer.open("new", 2);
    old.resolve({ success: false, message: "stale error" });
    await oldRequest;
    assert.equal(f.node("image-viewer-title").textContent, "输入原图 2");
    assert.equal(f.node("image-viewer-notice").hidden, true);
    await f.click("download");
    assert.deepEqual(f.downloads, [["new", 2]]);
    assert.deepEqual(args, [["old", 0], ["new", 2]]);
  } finally { await f.click("close"); f.restore(); }
});

test("closing during image decoding releases the pending object URL", async () => {
  const decode = deferred();
  const f = fixture(async () => success, () => decode.promise);
  try {
    const request = f.viewer.open("decoding");
    await Promise.resolve();
    assert.equal(f.created.length, 1);
    await f.click("close");
    decode.resolve();
    await request;
    assert.deepEqual(f.revoked, f.created);
    assert.equal(f.node("image-viewer-image").hidden, true);
  } finally { f.restore(); }
});

test("unavailable and malformed images show an error and allow retry/download", async () => {
  const responses = [{ success: false, message: "缓存已清理" }, { success: true, image: "https://invalid.example/image" }, success];
  const f = fixture(async () => responses.shift());
  try {
    await f.viewer.open("missing");
    assert.equal(f.node("image-viewer-message").textContent, "缓存已清理");
    assert.equal(f.buttons.get("retry").hidden, false);
    assert.equal(f.buttons.get("download").disabled, false);
    await f.viewer.open("bad");
    assert.match(f.node("image-viewer-message").textContent, /格式/);
    assert.deepEqual(f.created, []);
    await f.viewer.open("good");
    assert.equal(f.node("image-viewer-notice").hidden, true);
  } finally { await f.click("close"); f.restore(); }
});

test("converted previews are labelled and refit on a viewport resize", async () => {
  const f = fixture(async () => ({ ...success, preview_only: true, original_width: 8000, original_height: 5000 }));
  try {
    await f.viewer.open("converted");
    assert.match(f.node("image-viewer-info").textContent, /高清预览.*8000 × 5000/);
    f.node("image-viewer-viewport").clientWidth = 432;
    f.resize();
    assert.equal(f.node("image-viewer-scale").textContent, "25%");
    await f.dialog.emit("keydown", { key: "1", preventDefault() {} });
    assert.equal(f.node("image-viewer-scale").textContent, "100%");
  } finally { await f.click("close"); f.restore(); }
});
