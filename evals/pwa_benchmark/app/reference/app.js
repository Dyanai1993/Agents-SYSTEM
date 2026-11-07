// REFERENCE page logic - the PWA-reliability playbook, literally.
// 1 active version always servable · 2 isolated staging (never the active cache's name) · 3 verify every file before
// the pointer moves · 4 cleanup only after a successful switch · 5 a "ready" marker is checked against the real cache ·
// plus deliberate focus: a control that disappears hands focus to what replaces it.
const $ = (id) => document.getElementById(id);
const MARKER = "ready-version";

async function sha256(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}
async function activeName() {
  const hit = await (await caches.open("meta")).match("/__active");
  return hit ? await hit.text() : null;
}
async function activeUsable() {
  const name = await activeName();
  if (!name || !(await caches.has(name))) return false;
  return !!(await (await caches.open(name)).match("/index.html"));
}

async function stageAndVerify(onProgress) {
  const res = await fetch("/asset-manifest.json?stage=1", { cache: "no-store" });
  if (!res.ok) throw new Error("manifest unavailable");
  const m = await res.json();
  const name = `stage-${m.version}-${Date.now()}`;                 // isolated, never the active cache
  const cache = await caches.open(name);
  try {
    let done = 0;
    for (const f of m.files) {
      const r = await fetch("/" + f.path + "?stage=1", { cache: "no-store" });
      if (!r.ok) throw new Error("missing " + f.path);
      const buf = await r.clone().arrayBuffer();
      if (buf.byteLength !== f.size || (await sha256(buf)) !== f.sha256) throw new Error("hash " + f.path);
      await cache.put("/" + f.path, r);
      onProgress(Math.round((++done / m.files.length) * 100));
    }
  } catch (e) {
    await caches.delete(name);                                     // only the failed staging cache
    throw e;
  }
  return { name, version: m.version };
}

async function activate(staged) {
  await (await caches.open("meta")).put("/__active", new Response(staged.name));   // one step, to a verified cache
  localStorage.setItem(MARKER, staged.version);
  for (const k of await caches.keys()) if (k !== "meta" && k !== staged.name) await caches.delete(k);
}

function show(state, text) {
  const s = $("status");
  s.textContent = text || "";
  s.className = state === "error" ? "error" : "";
  $("progress").hidden = state !== "progress";
  $("download").hidden = state === "progress" || state === "ready";
  $("retry").hidden = state !== "error";
}

async function download() {
  show("progress");
  $("progress").focus();                                           // the button disappears: focus goes to progress
  try {
    const staged = await stageAndVerify((p) => {
      $("progress").setAttribute("aria-valuenow", String(p));
      $("progress").firstElementChild.style.width = p + "%";
    });
    await activate(staged);
    show("ready", `מוכן לשימוש ללא חיבור (גרסה ${staged.version})`);
    $("status").focus();
  } catch (e) {
    show("error", "ההורדה נכשלה. שום דבר לא נמחק - אפשר לנסות שוב.");
    $("retry").focus();
  }
}

async function checkUpdate() {
  const before = await activeName();
  if (!before) return;
  try {
    const staged = await stageAndVerify(() => {});
    if (before.startsWith(`stage-${staged.version}-`)) { await caches.delete(staged.name); $("status").textContent = "אין עדכון"; return; }
    await activate(staged);
    $("status").textContent = `עודכן לגרסה ${staged.version} - טענו מחדש כדי לראות אותה`;
  } catch (e) {
    $("status").textContent = "העדכון נכשל. הגרסה הקודמת נשארה פעילה.";
  }
}

function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("theme").setAttribute("aria-pressed", String(dark));
  $("theme").textContent = dark ? "מצב בהיר" : "מצב כהה";
}

async function init() {
  applyTheme(localStorage.getItem("theme") === "dark");
  $("theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    localStorage.setItem("theme", dark ? "dark" : "light");
    applyTheme(dark);
  });
  $("download").addEventListener("click", download);
  $("retry").addEventListener("click", download);
  $("update").addEventListener("click", checkUpdate);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
  if (await activeUsable()) show("ready", `מוכן לשימוש ללא חיבור (גרסה ${localStorage.getItem(MARKER) || ""})`);
  else if (localStorage.getItem(MARKER)) {                        // the marker says ready, the cache is gone: say so
    show("idle", "ההורדה הקודמת כבר לא שמורה במכשיר. צריך להוריד שוב.");
    $("download").textContent = "הורדה מחדש";
  }
  document.body.dataset.ready = "1";
}
init();
