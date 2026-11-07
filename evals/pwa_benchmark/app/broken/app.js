// BROKEN page logic - reproduces T-0026's flaws on purpose:
// staging writes into "fw-<version>" (the ACTIVE cache when the server still advertises it) and a failure deletes it;
// the "ready" marker is trusted without checking the cache; a disappearing button leaves focus on the page body.
const $ = (id) => document.getElementById(id);
const MARKER = "ready-version";

async function sha256(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function stageAndVerify(onProgress) {
  const res = await fetch("/asset-manifest.json?stage=1", { cache: "no-store" });
  if (!res.ok) throw new Error("manifest unavailable");
  const m = await res.json();
  const name = "fw-" + m.version;                                  // FLAW: the same name as the active cache
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
    await caches.delete(name);                                     // FLAW: may delete the active version
    throw e;
  }
  return { name, version: m.version };
}

async function activate(staged) {
  await (await caches.open("meta")).put("/__active", new Response(staged.version));
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
  show("progress");                                                // FLAW: no focus hand-off
  try {
    const staged = await stageAndVerify((p) => {
      $("progress").setAttribute("aria-valuenow", String(p));
      $("progress").firstElementChild.style.width = p + "%";
    });
    await activate(staged);
    show("ready", `מוכן לשימוש ללא חיבור (גרסה ${staged.version})`);
  } catch (e) {
    show("error", "ההורדה נכשלה.");
  }
}

async function checkUpdate() {
  try {
    const staged = await stageAndVerify(() => {});
    await activate(staged);
    $("status").textContent = `עודכן לגרסה ${staged.version}`;
  } catch (e) {
    $("status").textContent = "העדכון נכשל.";
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
  if (localStorage.getItem(MARKER)) show("ready", `מוכן לשימוש ללא חיבור (גרסה ${localStorage.getItem(MARKER)})`);   // FLAW: trusted
  document.body.dataset.ready = "1";
}
init();
