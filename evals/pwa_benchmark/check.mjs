// PWA reliability benchmark (the owner's brief, T-0026 as the benchmark).
// Runs the same browser checks against two implementations of one tiny Hebrew RTL PWA:
//   reference - follows the PWA-reliability playbook;  broken - reproduces T-0026's flaws on purpose.
// A check is only worth something if it fails on the broken behaviour, so the expected result is:
//   reference passes every check; broken fails each check aimed at one of its flaws.
// Zero model calls. Real Chromium (Playwright), real keyboard, real service worker; "offline" = the server refuses.
import { chromium } from "playwright";
import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.join(HERE, "app");
const OUT = path.join(HERE, "results");
fs.mkdirSync(OUT, { recursive: true });

// ------------------------------------------------------------------ builds A and B (visibly different)
function build(impl, version) {
  const files = {
    "index.html": Buffer.from(fs.readFileSync(path.join(APP, "index.html"), "utf8").replaceAll("{{VERSION}}", version)),
    "style.css": fs.readFileSync(path.join(APP, "style.css")),
    "manifest.webmanifest": fs.readFileSync(path.join(APP, "manifest.webmanifest")),
    "app.js": fs.readFileSync(path.join(APP, impl, "app.js")),
  };
  const manifest = { version, files: Object.entries(files).map(([p, b]) => ({ path: p, size: b.length,
    sha256: crypto.createHash("sha256").update(b).digest("hex") })) };
  return { ...files, "sw.js": fs.readFileSync(path.join(APP, impl, "sw.js")), "asset-manifest.json": Buffer.from(JSON.stringify(manifest)) };
}

// ------------------------------------------------------------------ the server: one state object drives every fault
const state = { impl: "reference", version: "A", brokenB: false, corrupt: null, missing: null, offline: false };
const builds = {};
for (const impl of ["reference", "broken"]) for (const v of ["A", "B"]) builds[`${impl}-${v}`] = build(impl, v);
const TYPES = { html: "text/html; charset=utf-8", js: "text/javascript", css: "text/css", json: "application/json",
  webmanifest: "application/manifest+json" };

const server = http.createServer((req, res) => {
  if (state.offline) return req.socket.destroy();                 // the network is gone
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
  const b = builds[`${state.impl}-${state.version}`];
  let body = b[p];
  if (state.brokenB && p === "index.html") body = Buffer.from("<!doctype html><html><body data-version='B-broken'><h1>BROKEN B</h1></body></html>");
  if (state.brokenB && p === "asset-manifest.json") body = builds[`${state.impl}-B`]["asset-manifest.json"];
  if (state.missing === p) body = undefined;
  if (state.corrupt === p && body) body = Buffer.concat([body, Buffer.from("/* corrupt */")]);
  if (!body) { res.writeHead(404); return res.end("not found"); }
  res.writeHead(200, { "Content-Type": TYPES[p.split(".").pop()] || "application/octet-stream", "Cache-Control": "no-store" });
  res.end(body);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const BASE = `http://127.0.0.1:${server.address().port}`;

// ------------------------------------------------------------------ browser helpers (real keyboard, real handlers)
const browser = await chromium.launch();
const reset = (impl) => Object.assign(state, { impl, version: "A", brokenB: false, corrupt: null, missing: null, offline: false });

async function fresh(impl) {
  reset(impl);
  const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  await page.goto("/");
  await page.waitForFunction(() => document.body.dataset.ready === "1");
  await page.evaluate(() => navigator.serviceWorker.ready);
  if (!(await page.evaluate(() => !!navigator.serviceWorker.controller))) await page.reload();
  await page.waitForFunction(() => !!navigator.serviceWorker.controller && document.body.dataset.ready === "1");
  return { ctx, page };
}
async function tabTo(page, id) {
  for (let i = 0; i < 15; i++) {
    await page.keyboard.press("Tab");
    if (await page.evaluate((x) => document.activeElement && document.activeElement.id === x, id)) return true;
  }
  throw new Error(`could not reach #${id} with Tab`);
}
const focused = (page) => page.evaluate(() => (document.activeElement && document.activeElement.id) || document.activeElement.tagName);
const statusText = (page) => page.locator("#status").innerText();
const version = (page) => page.evaluate(() => document.body.dataset.version).catch(() => null);
async function reloadSafe(page) {
  try { await page.reload({ timeout: 8000 }); return true; } catch { return false; }
}
async function keyboardDownload(page, expect = /מוכן|נכשלה/) {
  await tabTo(page, "download");
  await page.keyboard.press("Enter");
  try {
    await page.waitForFunction((re) => new RegExp(re).test(document.getElementById("status").innerText), expect.source, { timeout: 15000 });
  } catch (e) {
    throw new Error(`download did not reach ${expect}: status "${await statusText(page)}", focus ${await focused(page)}`);
  }
}

// ------------------------------------------------------------------ scenarios
const scenarios = {
  async "1 broken B never replaces verified A (online and offline)"(impl) {
    const { ctx, page } = await fresh(impl);
    await keyboardDownload(page, /מוכן/);
    state.brokenB = true;
    await page.goto("/");
    const online = await version(page);
    state.offline = true;
    const ok = await reloadSafe(page);
    const offline = ok ? await version(page) : "load failed";
    await ctx.close();
    return { pass: online === "A" && offline === "A", detail: `online shows ${online}, offline shows ${offline}` };
  },
  async "2 B takes over only after it is verified"(impl) {
    const { ctx, page } = await fresh(impl);
    await keyboardDownload(page, /מוכן/);
    state.version = "B";                                             // the server now has a good B
    await page.goto("/");
    const before = await version(page);                               // must still be A: B is not verified yet
    await tabTo(page, "update");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => /עודכן|נכשל|אין/.test(document.getElementById("status").innerText), null, { timeout: 10000 });
    await page.goto("/");
    const after = await version(page);
    const keys = await page.evaluate(() => caches.keys());
    await ctx.close();
    const aGone = !keys.some((k) => /(^fw-A$)|(^stage-A-)/.test(k));
    return { pass: before === "A" && after === "B" && aGone, detail: `before update ${before}, after ${after}, caches ${keys.join(",")}` };
  },
  async "3 a corrupt file while the manifest still says A keeps A working offline"(impl) {
    const { ctx, page } = await fresh(impl);
    await keyboardDownload(page, /מוכן/);
    state.corrupt = "style.css";                                      // same version A, one file corrupt
    await tabTo(page, "update");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => /עודכן|נכשל|אין/.test(document.getElementById("status").innerText), null, { timeout: 10000 });
    const keys = await page.evaluate(() => caches.keys());
    state.offline = true;
    const ok = await reloadSafe(page);
    const offline = ok ? await version(page) : "load failed";
    await ctx.close();
    return { pass: offline === "A", detail: `caches after the failed update: ${keys.join(",") || "none"}; offline reload shows ${offline}` };
  },
  async "4 a ready marker without its cache is reported truthfully"(impl) {
    const { ctx, page } = await fresh(impl);
    await keyboardDownload(page, /מוכן/);
    await page.evaluate(async () => { for (const k of await caches.keys()) if (k !== "meta") await caches.delete(k); });   // eviction
    await page.reload();
    await page.waitForFunction(() => document.body.dataset.ready === "1");
    const text = await statusText(page);
    const dl = await page.evaluate(() => { const b = document.getElementById("download"); return b.hidden ? null : b.textContent; });
    await ctx.close();
    return { pass: !/מוכן/.test(text) && !!dl, detail: `status "${text}", download button: ${dl ?? "hidden"}` };
  },
  async "5 keyboard: download, failure, retry keep focus deliberate"(impl) {
    const { ctx, page } = await fresh(impl);
    state.missing = "style.css";                                      // the first download fails
    await keyboardDownload(page, /נכשלה/);
    const afterFail = await focused(page);
    state.missing = null;
    await page.keyboard.press("Enter");                               // Enter on whatever has focus: should be Retry
    let retried = true;
    try { await page.waitForFunction(() => /מוכן/.test(document.getElementById("status").innerText), null, { timeout: 8000 }); }
    catch { retried = false; }
    const afterOk = await focused(page);
    await ctx.close();
    return { pass: afterFail === "retry" && retried && afterOk !== "BODY",
      detail: `focus after the failure: ${afterFail}; Enter retried: ${retried}; focus after success: ${afterOk}` };
  },
  async "6 keyboard: the theme toggles and persists"(impl) {
    const { ctx, page } = await fresh(impl);
    await tabTo(page, "theme");
    await page.keyboard.press("Enter");
    const pressed = await page.locator("#theme").getAttribute("aria-pressed");
    await page.reload();
    await page.waitForFunction(() => document.body.dataset.ready === "1");
    const theme = await page.evaluate(() => document.documentElement.dataset.theme);
    await ctx.close();
    return { pass: pressed === "true" && theme === "dark", detail: `aria-pressed ${pressed}, after reload theme ${theme}` };
  },
};

async function visualReview() {
  const shots = [], notes = [];
  for (const [w, h] of [[390, 844], [1280, 800]]) for (const theme of ["light", "dark"]) {
    reset("reference");
    const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: w, height: h } });
    const page = await ctx.newPage();
    await page.addInitScript((t) => localStorage.setItem("theme", t), theme);
    await page.goto("/");
    await page.waitForFunction(() => document.body.dataset.ready === "1");
    const f = path.join(OUT, `reference-${w}-${theme}.png`);
    await page.screenshot({ path: f, fullPage: true });
    const m = await page.evaluate(() => ({ dir: document.documentElement.dir, lang: document.documentElement.lang,
      tile: [document.querySelector(".tile").dir, document.querySelector(".tile").lang].join("/"),
      hscroll: document.documentElement.scrollWidth > window.innerWidth }));
    notes.push(`${w}px ${theme}: dir=${m.dir} lang=${m.lang} English tile ${m.tile} horizontal scroll ${m.hscroll}`);
    shots.push(path.basename(f));
    await ctx.close();
  }
  return { shots, notes };
}

// ------------------------------------------------------------------ run
const results = {};
const ONLY = process.env.ONLY || "";                                 // debugging: ONLY=1 runs scenario 1 only
for (const impl of ["reference", "broken"]) {
  results[impl] = {};
  for (const [name, fn] of Object.entries(scenarios)) {
    if (ONLY && !name.startsWith(ONLY)) continue;
    try { results[impl][name] = await fn(impl); }
    catch (e) { results[impl][name] = { pass: false, detail: "error: " + String(e.message || e).slice(0, 200) }; }
  }
}
const visual = await visualReview();
await browser.close();
server.close();

const flaws = Object.keys(results.broken).filter((n) => !n.startsWith("6"));   // 6 is not one of the broken build's flaws
const refAll = Object.values(results.reference).every((r) => r.pass);
const caught = flaws.filter((n) => !results.broken[n].pass);
const ok = refAll && caught.length === flaws.length;
const summary = { ok, reference_passes_all: refAll, broken_flaws_caught: `${caught.length}/${flaws.length}`, results, visual };
fs.writeFileSync(path.join(OUT, "benchmark.json"), JSON.stringify(summary, null, 2));
for (const impl of ["reference", "broken"]) {
  console.log(`\n${impl}`);
  for (const [n, r] of Object.entries(results[impl])) console.log(`  ${r.pass ? "PASS" : "FAIL"}  ${n} - ${r.detail}`);
}
console.log("\nvisual review (reference):"); for (const n of visual.notes) console.log("  " + n);
console.log(`\nreference passes every check: ${refAll}; the broken build is caught by ${caught.length}/${flaws.length} flaw checks`);
console.log(ok ? "BENCHMARK OK" : "BENCHMARK FAILED");
process.exit(ok ? 0 : 1);
