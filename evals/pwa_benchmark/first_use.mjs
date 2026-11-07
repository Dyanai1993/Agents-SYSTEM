// First-use evaluation (the owner's brief): does a first screen tell a first-time user what it is for and what to do
// first? Measured in a real browser - computed styles and layout, not source text - at 1280px and 390px.
//   FU1 one clear first action    exactly one accent-coloured, bold button, visible without scrolling
//   FU2 guidance                  that button's section has a heading and an explanatory sentence (20+ characters)
//   FU3 hierarchy                 2+ titled sections; no other visible control in the first action's section
//   FU4 styled, not defaults      design tokens (CSS custom properties), a non-default font, a 44px rounded primary
//                                 button, a content column narrower than the 1280px window
//   FU5 feedback                  a live region (role=status/alert or aria-live) in the first action's section
//   FU6 narrow screens            at 390px: no horizontal scroll, the first action visible without scrolling
//   FU7 direction                 html dir=rtl lang=he; every lang=en element is laid out left-to-right
// Targets: reference (the benchmark's reference app), bare (fixtures/bare: a T-0026-like screen), and t0026 (the real
// T-0026 build, served READ-ONLY from tasks/T-0026/workspace/firstwords/dist when present). Zero model calls.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(HERE, "results");
fs.mkdirSync(OUT, { recursive: true });
const T0026 = path.resolve(HERE, "..", "..", "tasks", "T-0026", "workspace", "firstwords", "dist");
const TYPES = { html: "text/html; charset=utf-8", js: "text/javascript", css: "text/css", json: "application/json",
  webmanifest: "application/manifest+json", png: "image/png", svg: "image/svg+xml", woff2: "font/woff2" };

function referenceSite() {
  const app = path.join(HERE, "app");
  return { "index.html": Buffer.from(fs.readFileSync(path.join(app, "index.html"), "utf8").replaceAll("{{VERSION}}", "A")),
    "style.css": fs.readFileSync(path.join(app, "style.css")), "app.js": fs.readFileSync(path.join(app, "reference", "app.js")),
    "sw.js": fs.readFileSync(path.join(app, "reference", "sw.js")), "manifest.webmanifest": fs.readFileSync(path.join(app, "manifest.webmanifest")) };
}
function serve(lookup) {
  const srv = http.createServer((req, res) => {
    const p = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
    if (p.includes("..")) { res.writeHead(400); return res.end(); }
    const body = lookup(p);
    if (!body) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type": TYPES[p.split(".").pop()] || "application/octet-stream", "Cache-Control": "no-store" });
    res.end(body);
  });
  return new Promise((r) => srv.listen(0, "127.0.0.1", () => r(srv)));
}
const fromDir = (dir) => (p) => { const f = path.join(dir, p); return f.startsWith(dir) && fs.existsSync(f) && fs.statSync(f).isFile() ? fs.readFileSync(f) : null; };

function measure() {
  const visible = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const rgb = (c) => (c.match(/[\d.]+/g) || []).map(Number);
  const saturation = (c) => { const [r, g, b, a = 1] = rgb(c); if (a === 0) return 0; const mx = Math.max(r, g, b) / 255, mn = Math.min(r, g, b) / 255; return mx === 0 ? 0 : (mx - mn) / mx; };
  const controls = [...document.querySelectorAll("button, [role=button], input, select, a[href]")].filter(visible);
  const primary = controls.filter((b) => { const s = getComputedStyle(b); return saturation(s.backgroundColor) > 0.3 && parseInt(s.fontWeight, 10) >= 600; });
  const p = primary[0];
  const section = p && p.closest("section, article, .card, [role=region]");
  const text = (el) => (el ? el.innerText || "" : "").trim();
  const live = "[role=status], [role=alert], [aria-live]";
  const guidance = section ? [...section.querySelectorAll("p")].filter((e) => !e.matches(live)).map(text).filter((t) => t.length >= 20) : [];
  let tokens = 0;
  for (const sheet of document.styleSheets) { try { for (const rule of sheet.cssRules) if (/--[\w-]+\s*:/.test(rule.cssText)) tokens++; } catch (e) { /* cross-origin */ } }
  const font = getComputedStyle(document.body).fontFamily;
  const main = document.querySelector("main") || document.body;
  const ps = p ? getComputedStyle(p) : null;
  const titled = [...document.querySelectorAll("section, article, .card")].filter((s) => visible(s) && s.querySelector("h1, h2, h3"));
  const en = [...document.querySelectorAll("[lang=en]")];
  return {
    primaryCount: primary.length, primaryLabel: p ? text(p) : null,
    primaryInView: !!p && p.getBoundingClientRect().bottom <= window.innerHeight,
    sectionHeading: !!section && !!section.querySelector("h1, h2, h3"), guidance: guidance[0] || null,
    titledSections: titled.length, othersInSection: section ? controls.filter((c) => c !== p && section.contains(c)).map(text) : [],
    tokens, font, primaryHeight: ps ? parseFloat(ps.height) : 0, primaryRadius: ps ? parseFloat(ps.borderTopLeftRadius) : 0,
    columnWidth: main.getBoundingClientRect().width, liveRegion: !!section && !!section.querySelector(live),
    hscroll: document.documentElement.scrollWidth > window.innerWidth, dir: document.documentElement.dir, lang: document.documentElement.lang,
    englishLtr: en.length > 0 && en.every((e) => getComputedStyle(e).direction === "ltr"), winWidth: document.documentElement.clientWidth,
  };
}

function judge(wide, narrow) {
  const defaultFont = /^\s*"?times/i.test(wide.font) || /^serif$/i.test(wide.font.trim());
  return {
    "FU1 one clear first action": [wide.primaryCount === 1 && wide.primaryInView, `${wide.primaryCount} primary button(s)${wide.primaryLabel ? ` ("${wide.primaryLabel}")` : ""}`],
    "FU2 guidance": [wide.sectionHeading && !!wide.guidance, wide.guidance ? `"${wide.guidance.slice(0, 60)}"` : "no heading + explanation next to the first action"],
    "FU3 hierarchy": [wide.titledSections >= 2 && wide.primaryCount === 1 && wide.othersInSection.length === 0,
      `${wide.titledSections} titled sections; other controls beside the first action: ${wide.othersInSection.join(", ") || "none"}`],
    "FU4 styled, not defaults": [wide.tokens > 0 && !defaultFont && wide.primaryHeight >= 44 && wide.primaryRadius > 0 && wide.columnWidth < 0.9 * wide.winWidth,
      `tokens ${wide.tokens}, font ${wide.font.split(",")[0]}, primary ${Math.round(wide.primaryHeight)}px r${wide.primaryRadius}, column ${Math.round(wide.columnWidth)}px`],
    "FU5 feedback": [wide.liveRegion, wide.liveRegion ? "a live region next to the first action" : "no live region next to a first action"],
    "FU6 narrow screens": [!narrow.hscroll && narrow.primaryInView, `390px: horizontal scroll ${narrow.hscroll}, first action in view ${narrow.primaryInView}`],
    "FU7 direction": [wide.dir === "rtl" && wide.lang === "he" && wide.englishLtr, `dir=${wide.dir} lang=${wide.lang} English LTR ${wide.englishLtr}`],
  };
}

// Mutants: the reference with exactly one property broken. Each must fail the criterion it targets - a criterion that
// cannot fail proves nothing (the same rule as check.mjs).
const REF = referenceSite();
const HTML = REF["index.html"].toString(), CSS = REF["style.css"].toString();
const edit = (from, to) => { const out = HTML.replace(from, to); if (out === HTML) throw new Error(`mutant anchor missing: ${from}`); return out; };
const site = (html = HTML, css = CSS) => ({ ...REF, "index.html": Buffer.from(html), "style.css": Buffer.from(css) });
const MUTANTS = {
  "FU1 one clear first action": ["two equally prominent buttons", site(edit('id="update" type="button"', 'id="update" type="button" class="primary"'))],
  "FU2 guidance": ["no explanation beside the first action", site(edit(/<p class="lead">[\s\S]*?<\/p>/, ""))],
  "FU3 hierarchy": ["a setting placed beside the first action", site(edit('<button id="retry"', '<button type="button">מצב כהה</button>\n    <button id="retry"'))],
  "FU4 styled, not defaults": ["browser-default font, full-width column", site(HTML, CSS + 'body{font-family:"Times New Roman"}.wrap{max-width:none}')],
  "FU5 feedback": ["no live region for the result", site(edit('id="status" role="status"', 'id="status"'))],
  "FU6 narrow screens": ["a fixed width wider than a phone", site(HTML, CSS + ".tile{min-width:600px}")],
  "FU7 direction": ["English laid out right-to-left", site(edit('class="tile" lang="en" dir="ltr"', 'class="tile" lang="en"'))],
};

// Targets. `node first_use.mjs <static-build-dir>` also scores any build (reported only, not part of OK).
const targets = [["reference", REF, true], ["bare", fromDir(path.join(HERE, "fixtures", "bare")), true]];
if (fs.existsSync(path.join(T0026, "index.html"))) targets.push(["t0026", fromDir(T0026), true]);
if (process.argv[2]) targets.push([`build:${path.basename(path.resolve(process.argv[2]))}`, fromDir(path.resolve(process.argv[2])), true]);
for (const [k, [why, s]] of Object.entries(MUTANTS)) targets.push([`mutant ${k.split(" ")[0]}: ${why}`, s, false]);

const browser = await chromium.launch();
const report = {};
for (const [name, src, shots] of targets) {
  const srv = await serve(typeof src === "function" ? src : (p) => src[p] || null);
  const url = `http://127.0.0.1:${srv.address().port}/`;
  const m = {};
  for (const [w, h] of [[1280, 800], [390, 844]]) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, serviceWorkers: "block" });
    const page = await ctx.newPage();
    await page.goto(url);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(300);
    m[w] = await page.evaluate(measure);
    if (shots) await page.screenshot({ path: path.join(OUT, `first-use-${name.replace(/[^\w-]+/g, "_")}-${w}.png`), fullPage: true });
    await ctx.close();
  }
  srv.close();
  report[name] = judge(m[1280], m[390]);
}
await browser.close();

const passes = (n) => Object.values(report[n]).filter(([ok]) => ok).length;
const total = Object.keys(report.reference).length;
const firstUse = ["FU1 one clear first action", "FU2 guidance", "FU3 hierarchy", "FU4 styled, not defaults"];
const mutantsCaught = Object.keys(MUTANTS).filter((k) => {
  const name = Object.keys(report).find((n) => n.startsWith(`mutant ${k.split(" ")[0]}:`));
  return !report[name][k][0];
});
const ok = passes("reference") === total && firstUse.every((k) => !report.bare[k][0]) && mutantsCaught.length === Object.keys(MUTANTS).length;
fs.writeFileSync(path.join(OUT, "first_use.json"), JSON.stringify({ ok, reference_passes_all: passes("reference") === total,
  bare_fails_first_use: firstUse.filter((k) => !report.bare[k][0]).length + "/" + firstUse.length,
  mutants_caught: `${mutantsCaught.length}/${Object.keys(MUTANTS).length}`,
  report: Object.fromEntries(Object.entries(report).map(([n, r]) =>
    [n, Object.fromEntries(Object.entries(r).map(([k, [pass, detail]]) => [k, { pass, detail }]))])) }, null, 2));
for (const [n, r] of Object.entries(report)) {
  if (n.startsWith("mutant ")) {
    const k = Object.keys(MUTANTS).find((x) => n.startsWith(`mutant ${x.split(" ")[0]}:`));
    console.log(`${r[k][0] ? "MISSED" : "caught"}  ${n} -> ${k} ${r[k][0] ? "PASSED" : "fails"} (${r[k][1]}); passes ${passes(n)}/${total}`);
    continue;
  }
  console.log(`\n${n}: ${passes(n)}/${total}`);
  for (const [k, [pass, detail]] of Object.entries(r)) console.log(`  ${pass ? "PASS" : "FAIL"}  ${k} - ${detail}`);
  if (n === "t0026" || n === targets[targets.length - Object.keys(MUTANTS).length - 1][0]) console.log("");
}
console.log(ok ? `\nFIRST-USE EVAL OK: the reference meets all ${total}; the bare shell fails the first-use criteria; ${mutantsCaught.length}/${Object.keys(MUTANTS).length} mutants caught`
  : "\nFIRST-USE EVAL FAILED");
process.exit(ok ? 0 : 1);
