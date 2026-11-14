import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../pages/linghui-studio/app.js", import.meta.url), "utf8");
const start = source.indexOf("function renderRouteHealth(");
const end = source.indexOf("\nasync function loadRouteHealth", start);
assert.ok(start >= 0 && end > start);
const renderSource = source.slice(start, end);
const escapeHtml = (text) => String(text).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");

function render(health) {
  const nodes = { "route-health-list": { innerHTML: "" }, "route-health-note": { textContent: "" } };
  const run = new Function("state", "byId", "escapeHtml", `${renderSource}; return renderRouteHealth;`)(
    { routeHealth: health }, (id) => nodes[id], escapeHtml,
  );
  run();
  return { cards: nodes["route-health-list"].innerHTML, note: nodes["route-health-note"].textContent };
}

test("persisted health displays restored outcomes and actual duration samples", () => {
  const { cards, note } = render({ persistent: true, persistence_ok: true, history_records: 42, channels: [
    { id: "orb", enabled: true, successes: 9, failures: 1, success_rate: 0.9, fallback_successes: 3,
      duration_sample_count: 9, p50_duration: 18.2, p95_duration: 62.7, daily_used: 5, daily_limit: 20 },
  ] });
  assert.ok(cards.includes("9 / 1"));
  assert.ok(cards.includes("90.0%"));
  assert.ok(cards.includes("18.2s / 62.7s"));
  assert.ok(cards.includes("9 个成功耗时样本"));
  assert.ok(note.includes("重载后保留"));
  assert.ok(note.includes("42 条请求明细"));
  assert.ok(note.includes("无法补齐"));
});

test("unknown latency is distinct from a measured zero-duration result", () => {
  const { cards } = render({ channels: [
    { id: "unknown", successes: 1, duration_sample_count: 0 },
    { id: "measured", successes: 1, duration_sample_count: 1, p50_duration: 0, p95_duration: 0 },
  ] });
  assert.equal((cards.match(/暂无耗时样本/g) || []).length, 1);
  assert.equal((cards.match(/0\.0s \/ 0\.0s/g) || []).length, 1);
});

test("write failures stay visible without hiding counts and provider text stays escaped", () => {
  const { cards, note } = render({ persistent: true, persistence_ok: false, persistence_error: "统计暂未保存", channels: [
    { id: "orb", successes: 1, failures: 2, last_error: '<img src=x onerror="bad">', duration_sample_count: 1 },
  ] });
  assert.ok(note.includes("统计尚未落盘"));
  assert.ok(note.includes("统计暂未保存"));
  assert.ok(cards.includes("1 / 2"));
  assert.ok(!cards.includes("<img"));
  assert.ok(cards.includes("&lt;img"));
});
