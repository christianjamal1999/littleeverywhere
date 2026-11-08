// KidSong Studio front end. Plain JS, no build step.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}`;

const state = { config: null, settings: {}, keys: {}, password: "", characters: [], project: null, dirty: false };

// ------------------------------------------------------------ settings (saved on the server)
// API keys are stored server-side (encrypted) so they work on every device. Only the optional
// app password stays in this browser. Older versions kept keys here; boot() migrates them.
const LEGACY_SETTINGS_KEY = "kidsong.settings.v1";
const PASSWORD_KEY = "kidsong.password";
const SECRET_FIELDS = ["anthropic_api_key", "hf_key_id", "hf_key_secret", "elevenlabs_api_key"];
function readLocal(key) { try { return localStorage.getItem(key); } catch { return null; } }
function writeLocal(key, value) {
  try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* private mode */ }
}

// ------------------------------------------------------------ api
async function api(method, path, body) {
  const headers = { "X-App-Password": state.password || "" };
  const init = { method, headers };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    if (res.status === 413) throw new Error("That file is too big to upload here (Vercel's limit is about 4.5 MB).");
    if (data?.auth) showTab("settings");
    throw new Error(data?.error || `Request failed (${res.status})`);
  }
  return data;
}

// Project writes are serialized so two requests never overwrite each other's changes.
let chain = Promise.resolve();
function queued(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}

// Remember which folded sections are open across a redraw (background jobs redraw often).
function readFolds(root) { return new Map($$("details[data-fold]", root).map((d) => [d.dataset.fold, d.open])); }
function applyFolds(root, folds) { $$("details[data-fold]", root).forEach((d) => { if (folds.has(d.dataset.fold)) d.open = folds.get(d.dataset.fold); }); }

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = isError ? "err" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), isError ? 7000 : 3500);
}

async function busy(btn, fn) {
  const label = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> ${label}`;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.innerHTML = label; }
}

// ------------------------------------------------------------ background activity
// Work keeps going while you move around the app (or close it). One poller, on every screen,
// asks the server to collect finished work and turns what finished into notices.
const SINCE_KEY = "kidsong.activitySince";
const activity = {
  server: [], local: new Map(), recent: [], suppress: new Set(), timer: null, ticking: false,
  since: Number(readLocal(SINCE_KEY)) || Date.now() / 1000,
};

// Show a request from this browser as running until it settles. Server-side jobs show up via
// the poller too; the shared key keeps them from being listed twice.
function trackLocal(key, label, promise, extra = {}) {
  activity.local.set(key, { key, label, started: Date.now() / 1000, ...extra });
  renderActivity();
  const settle = (ok) => {
    activity.local.delete(key);
    if (!ok) activity.suppress.add(key); // this screen already showed the error
    else if (extra.notifyHere && (document.hidden || !tabActive(extra.tab))) {
      notice({ key, label, ok: true, tab: extra.tab, ts: Date.now() / 1000 });
    }
    renderActivity();
    kickActivity();
  };
  promise.then(() => settle(true), () => settle(false));
  return promise;
}

function runningItems() {
  const items = [...activity.server];
  for (const l of activity.local.values()) if (!items.some((s) => s.key === l.key)) items.push(l);
  return items;
}

async function activityTick() {
  if (activity.ticking) return;
  clearTimeout(activity.timer);
  activity.ticking = true;
  try {
    const res = await api("POST", "/api/activity", { since: activity.since });
    activity.server = res.running;
    activity.since = res.now; writeLocal(SINCE_KEY, String(res.now));
    for (const ev of res.events) handleEvent(ev);
  } catch { /* offline or signed out: try again on the next tick */ }
  activity.ticking = false;
  renderActivity();
  activity.timer = setTimeout(activityTick, runningItems().length ? 5000 : 30000);
}
function kickActivity() { setTimeout(activityTick, 300); }

// Run fn once even if several events in the same poll ask for it.
const pendingOnce = new Set();
function once(key, fn) {
  if (pendingOnce.has(key)) return;
  pendingOnce.add(key);
  setTimeout(() => { pendingOnce.delete(key); fn(); }, 0);
}

function handleEvent(ev) {
  if (!ev.ok && activity.suppress.delete(ev.key)) return;
  activity.recent = [ev, ...activity.recent].slice(0, 8);
  // Refresh whatever is on screen that this touched; never navigate.
  // A rewritten storyboard replaces the scenes, so unsaved edits to the old ones are dropped.
  if (ev.project && viewing(ev.project)) once("project", () => refreshOpenProject(ev.kind !== "write"));
  if (ev.tab === "characters" && tabActive("characters")) once("characters", renderCharacters);
  if (tabActive("dashboard")) once("spend", renderSpend);
  if (ev.tab === "settings") once("channel", renderChannel);
  if (tabActive("studio") && !$("#studio-list").hidden) once("list", loadProjects);
  notice(ev);
}

function notice(ev) {
  const box = $("#notices");
  const el = document.createElement("div");
  el.className = `notice ${ev.ok ? "" : "bad"}`;
  el.innerHTML = `<div><b>${ev.ok ? "✓" : "✗"} ${esc(ev.label)} ${ev.ok ? "finished" : "failed"}</b>
      ${ev.title ? `<div class="muted">${esc(ev.title)}</div>` : ""}
      ${ev.error ? `<div class="error">${esc(ev.error)}</div>` : ""}</div>
    <div class="row">${ev.project || ev.tab ? `<button class="small primary" data-open>Open</button>` : ""}
      <button class="small ghost" data-close aria-label="Dismiss">✕</button></div>`;
  el.querySelector("[data-open]")?.addEventListener("click", () => { el.remove(); openTarget(ev); });
  el.querySelector("[data-close]").addEventListener("click", () => el.remove());
  box.prepend(el);
  while (box.children.length > 4) box.lastChild.remove();
  setTimeout(() => el.remove(), ev.ok ? 12000 : 30000);
  desktopNotify(ev);
}

function desktopNotify(ev) {
  if (!("Notification" in window) || Notification.permission !== "granted" || document.hasFocus()) return;
  const n = new Notification(`${ev.label} ${ev.ok ? "finished" : "failed"}`, { body: ev.title || ev.error || "KidSong Studio", tag: ev.key });
  n.onclick = () => { window.focus(); openTarget(ev); n.close(); };
}

function openTarget(item) {
  $("#activity-panel").hidden = true;
  if (item.project) { activateTab("studio"); openProject(item.project); }
  else if (item.tab) showTab(item.tab);
}

const ago = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`;
};

// A render's time left, from how fast it's actually going; before the first scene, a typical pace.
// Measured on real renders: a generated clip takes about 1.4x its length, a still about 0.75x.
const RENDER_PACE = { clip: 1.4, still: 0.75 }, UPLOAD_SECONDS = 15;
const BUMPER_SECONDS = 30; // building and joining the intro and outro
const renderEstimate = (scenes) => UPLOAD_SECONDS + BUMPER_SECONDS + scenes.reduce((sum, s) => sum + (s.end - s.start) * (s.video ? RENDER_PACE.clip : RENDER_PACE.still), 0);
const roughTime = (s) => (s < 60 ? "under a minute" : `about ${Math.ceil(s / 60)} min`);
function renderEta(item) {
  if (!item?.total || !item.done) return "";
  const elapsed = Date.now() / 1000 - item.started;
  const left = (elapsed / item.done) * (item.total - item.done) + UPLOAD_SECONDS;
  return `Scene ${item.done} of ${item.total} · ${roughTime(left)} left`;
}
function updateTaskBanner() {
  const el = $("#task-eta"); if (!el || !state.project) return;
  const item = runningItems().find((i) => i.key === `prj:${state.project.id}:${el.dataset.kind}`);
  if (item?.total) el.textContent = renderEta(item) || el.textContent;
}

function renderActivity() {
  updateTaskBanner();
  const items = runningItems();
  const btn = $("#activity-btn");
  btn.hidden = !items.length && !activity.recent.length;
  btn.innerHTML = items.length ? `<span class="spinner"></span> ${items.length} running` : "✓ All done";
  btn.classList.toggle("active", items.length > 0);
  document.title = items.length ? `⏳ ${items.length} · KidSong Studio` : "KidSong Studio";
  const panel = $("#activity-panel");
  if (panel.hidden) return;
  const row = (it, i, list) => `<div class="act-item" data-list="${list}" data-i="${i}">
      <div>${list === "recent" ? (it.ok ? "✓ " : "✗ ") : ""}${esc(it.label)}<div class="muted">${esc(it.title || (it.tab ? it.tab[0].toUpperCase() + it.tab.slice(1) : ""))}</div></div>
      <span class="muted">${(list === "running" && renderEta(it)) || ago(it.started || it.ts)}</span></div>`;
  const perm = !("Notification" in window) ? ""
    : Notification.permission === "granted" ? `<p class="hint">Desktop notifications are on.</p>`
    : Notification.permission === "denied" ? `<p class="hint">Desktop notifications are blocked in your browser's site settings.</p>`
    : `<button class="small" id="enable-notify">Turn on desktop notifications</button>`;
  panel.innerHTML = `<h3>Running</h3>${items.length ? items.map((it, i) => row(it, i, "running")).join("") : `<p class="hint">Nothing running.</p>`}
    ${activity.recent.length ? `<h3>Recently finished</h3>${activity.recent.map((it, i) => row(it, i, "recent")).join("")}` : ""}
    <p class="hint">Everything keeps going if you switch screens or close this tab. You'll get a notice when it's done.</p>${perm}`;
  panel.onclick = async (e) => {
    if (e.target.id === "enable-notify") { await Notification.requestPermission(); renderActivity(); return; }
    const row = e.target.closest(".act-item"); if (!row) return;
    const list = row.dataset.list === "recent" ? activity.recent : items;
    openTarget(list[+row.dataset.i]);
  };
}
$("#activity-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const panel = $("#activity-panel");
  panel.hidden = !panel.hidden;
  e.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
  renderActivity();
});
document.addEventListener("click", (e) => { if (!e.target.closest(".activity")) $("#activity-panel").hidden = true; });

// ------------------------------------------------------------ tabs
function activateTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
}
function tabActive(name) { return $(`#tab-${name}`).classList.contains("active"); }
function showTab(name) {
  activateTab(name);
  if (name === "dashboard") renderSpend();
  if (name === "settings") renderChannel();
  if (name === "characters") renderCharacters();
  if (name === "studio" && !state.project) loadProjects();
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

// ------------------------------------------------------------ settings tab
function fillSettingsForm() {
  const f = $("#settings-form");
  const s = { ...state.config.defaults, ...state.settings };
  for (const el of f.elements) {
    if (!el.name) continue;
    if (el.name === "app_password") el.value = state.password;
    else if (SECRET_FIELDS.includes(el.name)) {
      // Saved keys never come back to the browser; show that one is saved instead.
      const k = state.keys[el.name] || {};
      el.value = "";
      el.placeholder = k.set ? `Saved (${k.hint}). Type a new one to replace it.` : "Not set";
      let rm = el.parentElement.querySelector("[data-remove]");
      if (!rm) {
        rm = document.createElement("button");
        rm.type = "button"; rm.className = "small ghost danger"; rm.dataset.remove = el.name; rm.textContent = "Remove saved key";
        el.after(rm);
      }
      rm.hidden = !k.set;
    } else if (el.type === "checkbox") el.checked = s[el.name] !== false;
    else el.value = s[el.name] ?? "";
  }
  $("#password-box").hidden = !state.config.password_required;
  $("#storage-note").textContent = {
    supabase: "Characters, songs and clips are saved in Supabase.",
    ephemeral: "⚠️ Supabase isn't connected yet: anything you make here will disappear. Add SUPABASE_URL and SUPABASE_SECRET_KEY to this Vercel project's environment variables, then redeploy.",
    local: "Running locally: characters, songs and clips are stored in the app's data folder.",
  }[state.config.storage];
  if (state.config.storage === "ephemeral") toast("Supabase isn't connected, so your work won't be saved (see Settings).", true);
}
function applySettingsView(view) {
  state.settings = view.settings; state.keys = view.keys;
  if (view.keys_unreadable) toast("Your saved API keys can't be decrypted (the server's encryption key changed). Please enter them again.", true);
}
// Read the form: plain settings, plus only the key fields you typed into (blank = keep saved).
function readSettingsForm() {
  const settings = {}, keys = {};
  for (const el of $("#settings-form").elements) {
    if (!el.name || el.name === "app_password") continue;
    if (SECRET_FIELDS.includes(el.name)) { if (el.value.trim()) keys[el.name] = el.value.trim(); }
    else settings[el.name] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  return { settings, keys };
}
async function saveSettings(payload) {
  applySettingsView(await api("PUT", "/api/settings", payload));
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.password = e.target.elements.app_password.value; writeLocal(PASSWORD_KEY, state.password || null);
  const btn = $("button[type=submit]", e.target);
  const ok = await busy(btn, async () => { await saveSettings(readSettingsForm()); return true; });
  if (ok) { fillSettingsForm(); toast("Settings saved. Your keys now work on any device you sign in from."); }
});
// ------------------------------------------------------------ settings: price check
let priceCheck = null;
const usd = (n) => (n == null ? "?" : n < 0.1 ? `${(n * 100).toFixed(1)}¢` : `$${n.toFixed(2)}`);
function cheapest(rows) { return rows.filter((r) => r.usd != null).sort((a, b) => a.usd - b.usd)[0]; }
function isCurrent(row) { return Object.entries(row.settings).every(([k, v]) => state.settings[k] === v); }
function renderPrices() {
  if (!priceCheck) return;
  const table = (title, unit, rows) => `<h3>${title} <span class="muted">(${unit})</span></h3>
    <table class="prices">${rows.map((r, i) => `<tr class="${r === cheapest(rows) ? "best" : ""}">
      <td>${esc(r.label)}${r === cheapest(rows) ? ' <span class="chip on">cheapest</span>' : ""}</td>
      <td class="num">${r.error ? `<span class="muted" title="${esc(r.error)}">${r.error.includes("price reply") ? "usage-based" : "unavailable"}</span>` : usd(r.usd)}</td>
      <td>${isCurrent(r) ? '<span class="muted">in use</span>' : r.usd != null ? `<button type="button" class="small" data-use="${title === "Keyframes" ? "keyframes" : "animation"}:${i}">Use</button>` : ""}</td></tr>`).join("")}</table>`;
  const scenes = 20, k = cheapest(priceCheck.keyframes), a = cheapest(priceCheck.animation);
  $("#prices").innerHTML = table("Keyframes", "per image", priceCheck.keyframes)
    + table("Animation", "per 5-second clip", priceCheck.animation)
    + (k && a ? `<p class="hint">With the cheapest options, a ${scenes}-scene video costs about ${usd(scenes * (k.usd + a.usd))} on Higgsfield (more if you redo scenes). For songwriting, Claude Sonnet 5.5 costs less than Opus, and Haiku 5.5 costs the least.</p>` : "");
  $("#use-cheapest").hidden = !(k && a) || (isCurrent(k) && isCurrent(a));
}
async function applyPriceSettings(patch, msg) {
  await saveSettings({ settings: patch });
  fillSettingsForm(); renderPrices(); toast(msg);
}
$("#check-prices").addEventListener("click", async (e) => {
  if (!hasKeys("hf_key_id", "hf_key_secret")) return;
  const res = await busy(e.target, () => api("POST", "/api/prices"));
  if (res) { priceCheck = res; renderPrices(); }
});
$("#use-cheapest").addEventListener("click", (e) => busy(e.target, () => applyPriceSettings(
  { ...cheapest(priceCheck.keyframes).settings, ...cheapest(priceCheck.animation).settings },
  "Switched to the cheapest keyframe and animation options.")));
$("#prices").addEventListener("click", (e) => {
  const use = e.target.dataset.use; if (!use) return;
  const [group, i] = use.split(":"), row = priceCheck[group][+i];
  busy(e.target, () => applyPriceSettings(row.settings, `Now using ${row.label}.`));
});

$("#tab-settings").addEventListener("click", async (e) => {
  const remove = e.target.dataset.remove;
  if (remove) {
    if (!confirm("Remove this saved key from the app?")) return;
    const ok = await busy(e.target, async () => { await saveSettings({ keys: { [remove]: null } }); return true; });
    if (ok) { fillSettingsForm(); toast("Key removed."); }
    return;
  }
  const service = e.target.dataset.test; if (!service) return;
  // Test exactly what's in the form right now, saved or not.
  const { settings, keys } = readSettingsForm();
  const out = $(`.test-result[data-for="${service}"]`);
  out.className = "test-result"; out.textContent = "";
  const res = await busy(e.target, () => api("POST", `/api/test/${service}`, { override: { ...settings, ...keys } }));
  if (!res) return;
  out.className = `test-result ${res.ok ? "ok" : "bad"}`;
  out.textContent = `${res.ok ? "✓" : "✗"} ${res.message}`;
});
function hasKeys(...keys) {
  const missing = keys.filter((k) => !state.keys[k]?.set);
  if (missing.length) { toast("Add your API keys in Settings first.", true); showTab("settings"); return false; }
  return true;
}

// ------------------------------------------------------------ channel icon and intro
async function renderChannel() {
  let ch;
  try { ch = await api("GET", "/api/channel"); } catch { return; }
  const busyIcon = !!ch.icon_job, busyClip = !!ch.clip_job;
  $("#channel-icon").innerHTML = ch.icon ? `<img src="${esc(ch.icon)}" alt="Channel icon">` : busyIcon ? `<span class="spinner"></span>` : "No icon yet";
  const iconBtn = $("#make-icon"), introBtn = $("#make-intro"), dl = $("#icon-download");
  iconBtn.disabled = busyIcon; iconBtn.textContent = busyIcon ? "Generating…" : ch.icon ? "New icon" : "Generate icon";
  introBtn.disabled = busyClip || !ch.icon; introBtn.textContent = busyClip ? "Animating…" : ch.clip ? "Animate again" : "Animate intro";
  dl.hidden = !ch.icon; if (ch.icon) dl.href = ch.icon;
  const err = ch.icon_error || ch.clip_error, errBox = $("#channel-error");
  errBox.hidden = !err; errBox.textContent = err || "";
  const preview = $("#intro-preview");
  preview.hidden = !ch.clip;
  if (ch.clip && preview.dataset.src !== ch.clip) {
    preview.dataset.src = ch.clip;
    preview.innerHTML = `<video src="${esc(ch.clip)}" controls muted playsinline preload="metadata"></video>`;
  }
}
for (const [id, path] of [["#make-icon", "icon"], ["#make-intro", "animate"]]) {
  $(id).addEventListener("click", async (e) => {
    if (!hasKeys("hf_key_id", "hf_key_secret")) return;
    const ok = await busy(e.currentTarget, () => api("POST", `/api/channel/${path}`));
    renderChannel();
    if (ok) { toast("Working on it. You'll get a notice when it's ready."); kickActivity(); }
  });
}

// ------------------------------------------------------------ dashboard
const money = (n) => (n > 0 && n < 0.01 ? "<$0.01" : `$${(n || 0).toFixed(2)}`);
const SERVICE_ROLE = { Anthropic: "Songs, storyboards, ideas", ElevenLabs: "Music", Higgsfield: "Keyframes and animation" };
async function renderSpend() {
  const box = $("#spend");
  let d;
  try { d = await api("GET", `/api/spend?days=${$("#spend-range").value}`); }
  catch (e) { box.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  const services = Object.entries(d.services);
  const when = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  box.innerHTML = `<div class="tiles">
      <div class="tile total"><div class="tile-label">Total spend</div><div class="tile-value">${money(d.total)}</div><div class="muted">${d.videos.length} video${d.videos.length === 1 ? "" : "s"}</div></div>
      ${services.map(([name, s]) => `<div class="tile"><div class="tile-label">${esc(name)}</div><div class="tile-value">${money(s.usd)}</div>
        <div class="muted">${esc(SERVICE_ROLE[name] || "")} · ${s.calls} call${s.calls === 1 ? "" : "s"}${s.unpriced ? `, ${s.unpriced} without a price` : ""}</div></div>`).join("")}
    </div>
    <div class="card spend-card"><h2>Cost per video</h2>
      ${d.videos.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>Video</th>${services.map(([n]) => `<th class="num">${esc(n)}</th>`).join("")}<th class="num">Total</th></tr></thead>
        <tbody>${d.videos.map((v) => `<tr><td>${esc(v.title)}</td>${services.map(([n]) => `<td class="num">${money(v.by_service[n])}</td>`).join("")}<td class="num strong">${money(v.usd)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">Nothing charged to a video in this period.</p>`}
    </div>
    <div class="card spend-card"><h2>Recent charges</h2>
      ${d.recent.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>When</th><th>API key</th><th>What</th><th>Details</th><th class="num">Cost</th></tr></thead>
        <tbody>${d.recent.map((e) => `<tr><td class="nowrap">${when(e.ts)}</td><td>${esc(e.service)}</td><td>${esc(e.what)}${e.title ? `<div class="muted">${esc(e.title)}</div>` : ""}</td><td class="muted">${esc(e.detail)}</td><td class="num">${e.usd == null ? '<span class="muted">No price</span>' : money(e.usd)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">No charges yet in this period.</p>`}
    </div>
    <p class="hint">Calculated from each provider's published prices, so treat these as close estimates; your provider invoices are the final word. Spending before this Dashboard was added isn't included. ElevenLabs is priced at its $0.15 per minute API rate, which may differ if your plan includes credits.</p>`;
}
$("#spend-range").addEventListener("change", renderSpend);

// ------------------------------------------------------------ characters
async function loadCharacters() {
  state.characters = await api("GET", "/api/characters");
  return state.characters;
}

async function renderCharacters() {
  const box = $("#characters");
  try { await loadCharacters(); } catch (e) { box.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (!state.characters.length) {
    box.innerHTML = `<div class="card"><h3>No characters yet</h3><p class="muted">Channels in this niche win with a recurring cast of 3-5 kids. Create your first one, or let Claude design one.</p></div>`;
  } else {
    const folds = readFolds(box);
    box.innerHTML = state.characters.map(charCard).join("");
    applyFolds(box, folds);
  }
}

function charCard(c) {
  const imgs = c.images.map((u) => `<figure><img src="${esc(u)}" alt="${esc(c.name)} reference" loading="lazy"><button class="small" data-act="rm-img" data-url="${esc(u)}" title="Remove image" aria-label="Remove image">×</button></figure>`).join("");
  const pending = c.job ? `<div class="empty"><span class="spinner"></span></div>` : "";
  const sub = [c.age ? `Age ${esc(c.age)}` : "", `${c.images.length} reference${c.images.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
  return `<div class="card" data-id="${c.id}">
    <div class="char-head"><div><h2>${esc(c.name)}</h2><p class="muted">${sub}</p></div><button class="ghost danger small" data-act="del">Delete</button></div>
    <div class="char-imgs">${imgs}${pending}${!imgs && !pending ? `<div class="empty">No image yet</div>` : ""}</div>
    ${c.job_error ? `<p class="error">${esc(c.job_error)}</p>` : ""}
    <div class="row char-actions">
      <button class="small ${c.images.length ? "" : "primary"}" data-act="gen">${c.images.length ? "New portrait" : "Generate portrait"}</button>
      ${c.images.length ? `<button class="small" data-act="sheet">Turnaround sheet</button>` : ""}
      <label style="margin:0"><input type="file" accept="image/png,image/jpeg,image/webp" data-act="upload" hidden><span class="btn-like small">Upload</span></label>
    </div>
    <details class="fold" data-fold="${c.id}:look"><summary>Appearance</summary>
      <label>Name <input data-f="name" value="${esc(c.name)}"></label>
      <div class="two"><label>Age <input data-f="age" value="${esc(c.age)}"></label><label>Signature move <input data-f="signature" value="${esc(c.signature)}"></label></div>
      <label>Look <textarea data-f="look" rows="3">${esc(c.look)}</textarea></label>
      <label>Face <span class="muted">eyes, eyebrows, nose, ears, teeth: must match in every scene</span><textarea data-f="face" rows="5">${esc(c.face)}</textarea></label>
      <label>Mannerisms <span class="muted">how they move</span><textarea data-f="mannerisms" rows="2">${esc(c.mannerisms)}</textarea></label>
      <label>Outfit <textarea data-f="outfit" rows="3">${esc(c.outfit)}</textarea></label>
      <label>Personality <textarea data-f="personality" rows="2">${esc(c.personality)}</textarea></label>
      <p class="hint">The first two images, the face then the full body, are sent as references for every scene. Remove any that drift off-model.</p>
    </details>
    <details class="fold" data-fold="${c.id}:bible"><summary>Story bible</summary>
      <div class="row bible-action"><button class="small" data-act="bible">Fill in with Claude</button></div>
      <p class="hint">Claude uses this in every song so ${esc(c.name)} stays in character from episode to episode.</p>
      <label>Voice <span class="muted">how they talk and sing</span><textarea data-f="voice" rows="2">${esc(c.voice)}</textarea></label>
      <label>Likes <span class="muted">favorite things to feature</span><textarea data-f="likes" rows="2">${esc(c.likes)}</textarea></label>
      <label>Friends <span class="muted">how they get along with the cast</span><textarea data-f="friends" rows="2">${esc(c.friends)}</textarea></label>
      <label>Growing <span class="muted">a small thing they're learning</span><textarea data-f="growth" rows="2">${esc(c.growth)}</textarea></label>
    </details>
  </div>`;
}

// Uploads pass through Vercel, which refuses anything over about 4.5 MB, so big photos are resized
// here first. References don't need more than this anyway.
const MAX_IMAGE_SIDE = 2048, MAX_UPLOAD_BYTES = 4 * 1024 * 1024;
async function shrinkImage(file) {
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { return file; }
  const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= MAX_UPLOAD_BYTES) return file;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  let blob;
  for (const quality of [0.9, 0.8, 0.65]) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob.size <= MAX_UPLOAD_BYTES) break;
  }
  return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
}

$("#characters").addEventListener("change", async (e) => {
  const card = e.target.closest("[data-id]"); if (!card) return;
  const id = card.dataset.id;
  if (e.target.dataset.act === "upload") {
    const fd = new FormData(); fd.append("file", await shrinkImage(e.target.files[0]));
    try { await api("POST", `/api/characters/${id}/images`, fd); toast("Image added."); renderCharacters(); } catch (err) { toast(err.message, true); }
  } else if (e.target.dataset.f) {
    try { await api("PUT", `/api/characters/${id}`, { [e.target.dataset.f]: e.target.value }); } catch (err) { toast(err.message, true); }
  }
});

$("#characters").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]"); if (!btn) return;
  const card = btn.closest("[data-id]"); const id = card.dataset.id;
  const c = state.characters.find((x) => x.id === id);
  if (btn.dataset.act === "del") {
    if (!confirm(`Delete ${c.name} and their images?`)) return;
    await busy(btn, () => api("DELETE", `/api/characters/${id}`)); renderCharacters();
  } else if (btn.dataset.act === "rm-img") {
    await busy(btn, () => api("PUT", `/api/characters/${id}`, { images: c.images.filter((u) => u !== btn.dataset.url) })); renderCharacters();
  } else if (btn.dataset.act === "bible") {
    if (!hasKeys("anthropic_api_key")) return;
    const ok = await busy(btn, () => trackLocal(`chr:${id}:bible`, `Story bible for ${c.name}`,
      api("POST", `/api/characters/${id}/bible`), { tab: "characters", notifyHere: true }));
    if (ok && tabActive("characters")) { toast(`${c.name}'s story bible is filled in. Edit anything you like.`); renderCharacters(); }
  } else if (btn.dataset.act === "gen" || btn.dataset.act === "sheet") {
    if (!hasKeys("hf_key_id", "hf_key_secret")) return;
    const ok = await busy(btn, () => api("POST", `/api/characters/${id}/generate`, { sheet: btn.dataset.act === "sheet" }));
    if (ok) { toast("Generating on Higgsfield. Feel free to keep working; you'll get a notice when it's ready."); renderCharacters(); kickActivity(); }
  }
});

const charDialog = $("#char-dialog"), charForm = $("#char-form");
function showCharSource() {
  const source = charForm.elements.source.value;
  $$("[data-source]", charForm).forEach((el) => { el.hidden = el.dataset.source !== source; });
}
charForm.addEventListener("change", (e) => { if (e.target.name === "source") showCharSource(); });
$("#char-cancel").addEventListener("click", () => charDialog.close());
$("#new-character").addEventListener("click", () => {
  if (!hasKeys("anthropic_api_key", "hf_key_id", "hf_key_secret")) return;
  charForm.reset(); showCharSource();
  $("#char-error").hidden = true;
  charDialog.showModal();
});
charForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = charForm.elements, name = f.name.value.trim(), errBox = $("#char-error");
  const fail = (msg) => { errBox.textContent = msg; errBox.hidden = false; };
  errBox.hidden = true;
  let request;
  if (f.source.value === "photo") {
    if (!f.file.files[0]) return fail("Choose a photo of the child.");
    if (!f.consent.checked) return fail("Confirm you have permission to use this child's likeness.");
    const fd = new FormData();
    fd.append("file", await shrinkImage(f.file.files[0]));
    fd.append("name", name); fd.append("age", f.age.value.trim()); fd.append("consent", "yes");
    request = () => api("POST", "/api/characters/from-photo", fd);
  } else {
    const concept = [f.age.value.trim() && `Age ${f.age.value.trim()}.`, f.concept.value.trim()].filter(Boolean).join(" ");
    request = () => api("POST", "/api/characters/describe", { name, concept });
  }
  try {
    await busy($("#char-submit"), () => trackLocal("chr:new:create", `Creating ${name}`, request(), { tab: "characters", notifyHere: true }).catch((err) => { fail(err.message); }).then((c) => {
      if (!c) return;
      charDialog.close();
      toast(`${name} is on the way: the face first, then a full-body photo matched to it.`);
      renderCharacters(); kickActivity();
    }));
  } catch (err) { fail(err.message); }
});
$("#clear-cast").addEventListener("click", async (e) => {
  const n = state.characters.length;
  if (!n) { toast("The cast is already empty."); return; }
  if (!confirm(`Permanently remove all ${n} characters and their images? This can't be undone. Videos you've already made keep their keyframes and clips.`)) return;
  await busy(e.target, () => api("DELETE", "/api/characters"));
  renderCharacters();
});

// ------------------------------------------------------------ ideas
// Just enough markdown for the research brief: headings/bold, bullets, links.
function miniMarkdown(md) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html = "", inList = false;
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)/);
    if (bullet && !inList) { html += "<ul>"; inList = true; }
    if (!bullet && inList) { html += "</ul>"; inList = false; }
    if (bullet) html += `<li>${inline(bullet[1])}</li>`;
    else if (/^#+\s/.test(line)) html += `<h3>${inline(line.replace(/^#+\s/, ""))}</h3>`;
    else if (line) html += `<p>${inline(line)}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

$("#get-ideas").addEventListener("click", async (e) => {
  if (!hasKeys("anthropic_api_key")) return;
  const focus = $("#idea-focus").value;
  const box = $("#research");
  let research = null;
  if ($("#idea-research").checked) {
    box.innerHTML = `<div class="card"><span class="spinner"></span> Searching the web for what's trending in kids' music… this takes a minute or two.</div>`;
    research = await busy(e.target, () => trackLocal("ideas:research", "Researching trends",
      api("POST", "/api/research", { focus }), { tab: "ideas" }));
    if (!research) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="card research"><div class="row between"><h2>Trend brief</h2><span class="muted">${new Date().toLocaleDateString()}</span></div>
      ${miniMarkdown(research.brief)}
      ${research.sources.length ? `<details><summary>${research.sources.length} sources</summary><ul class="sources">${research.sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></li>`).join("")}</ul></details>` : ""}</div>`;
  } else box.innerHTML = "";
  const ideas = await busy(e.target, () => trackLocal("ideas:ideas", "Brainstorming video ideas",
    api("POST", "/api/ideas", { focus, research: research?.brief || "" }), { tab: "ideas", notifyHere: true }));
  if (!ideas) return;
  $("#ideas").innerHTML = ideas.map((i, n) => `<div class="card">
    <h3>${esc(i.topic)}</h3>
    <p class="lyric">"${esc(i.hook)}"</p>
    <p class="muted">${esc(i.lesson)} · ${esc(i.setting)} · ${esc(i.mood)}</p>
    <p>${esc(i.why)}</p>
    <button class="primary small" data-idea="${n}">Make this video</button></div>`).join("");
  $("#ideas").onclick = (ev) => {
    const n = ev.target.dataset.idea; if (n === undefined) return;
    const i = ideas[+n];
    showTab("studio");
    openBriefForm({ topic: i.topic, lesson: i.lesson, setting: i.setting, mood: i.mood, notes: `Chorus hook idea: ${i.hook}` });
  };
});

// ------------------------------------------------------------ studio: list
async function loadProjects() {
  state.project = null;
  $("#studio-editor").hidden = true; $("#studio-list").hidden = false;
  const box = $("#projects");
  try {
    const list = await api("GET", "/api/projects");
    box.innerHTML = list.length ? list.map((p) => `<div class="card click" data-open="${p.id}">
      <div class="thumb">${p.thumb ? `<img src="${esc(p.thumb)}" alt="" loading="lazy">` : "No image yet"}</div>
      <h3>${esc(p.title)}</h3>
      <p class="muted">${p.scenes ? `${p.scenes} scenes` : "Brief only"}${p.final ? " · rendered ✓" : ""}</p>
      ${p.busy ? `<span class="chip busy"><span class="spinner"></span> ${p.busy} running</span>` : ""}</div>`).join("")
      : `<div class="card"><h3>No videos yet</h3><p class="muted">Start with a new video, or get ideas from Claude in the Ideas tab. Tip: set up your cast in Characters first.</p></div>`;
  } catch (e) { box.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}
$("#projects").addEventListener("click", (e) => { const c = e.target.closest("[data-open]"); if (c) openProject(c.dataset.open); });
$("#new-video").addEventListener("click", () => openBriefForm({}));

// ------------------------------------------------------------ studio: brief
async function openBriefForm(brief, project) {
  await loadCharacters().catch(() => {});
  $("#studio-list").hidden = true;
  const ed = $("#studio-editor"); ed.hidden = false;
  const cast = new Set(brief.cast || state.characters.map((c) => c.id));
  ed.innerHTML = `<button class="ghost small back" id="back">← All videos</button>
    <h1>${project ? "Edit brief" : "New video"}</h1>
    <form id="brief" class="card settings">
      <label>Topic <input name="topic" required value="${esc(brief.topic)}" placeholder="e.g. Counting ducks at the pond"></label>
      <label>What kids learn <input name="lesson" value="${esc(brief.lesson)}" placeholder="e.g. counting 1-10, taking turns, farm animal sounds"></label>
      <div class="two">
        <label>Setting <input name="setting" value="${esc(brief.setting)}" placeholder="e.g. sunny farm, snowy mountain, coral reef"></label>
        <label>Mood / genre <input name="mood" value="${esc(brief.mood)}" placeholder="e.g. bouncy dance pop, gentle lullaby"></label>
      </div>
      <label>Length
        <select name="length">${[90, 120, 150, 180, 210].map((s) => `<option value="${s}" ${+(brief.length || 150) === s ? "selected" : ""}>${fmtTime(s)}</option>`).join("")}</select>
      </label>
      <label>Cast</label>
      <div class="chips" id="cast-pick">${state.characters.length ? state.characters.map((c) => `<span class="chip ${cast.has(c.id) ? "on" : ""}" data-cid="${c.id}">${esc(c.name)}</span>`).join("") : `<span class="muted">No characters yet. Add some in the Characters tab for a consistent cast.</span>`}</div>
      <label>Notes for Claude <textarea name="notes" rows="2">${esc(brief.notes)}</textarea></label>
      <div class="row"><button class="primary" type="submit">${project ? "Save brief" : "Save & write the song"}</button></div>
    </form>`;
  $("#back").onclick = loadProjects;
  $("#cast-pick").onclick = (e) => { const c = e.target.dataset.cid; if (c) { e.target.classList.toggle("on"); } };
  $("#brief").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const b = Object.fromEntries(fd.entries());
    b.cast = $$("#cast-pick .chip.on").map((x) => x.dataset.cid);
    const btn = $("button[type=submit]", e.target);
    if (project) {
      const p = await busy(btn, () => queued(() => api("PUT", `/api/projects/${project.id}`, { brief: b })));
      if (p) renderProject(p);
      return;
    }
    const p = await busy(btn, () => api("POST", "/api/projects", { brief: b }));
    if (!p) return;
    state.project = p; renderProject(p);
    if (state.keys.anthropic_api_key?.set) writeSong($("#write-song"));
  };
}

// ------------------------------------------------------------ studio: editor
async function openProject(id) {
  try { renderProject(await api("GET", `/api/projects/${id}`)); } catch (e) { toast(e.message, true); }
}

function castName(id) { return state.characters.find((c) => c.id === id)?.name || "?"; }

// The long task running on this video (from the server, or just started from this browser).
function runningTask(p) {
  if (p.task) return p.task;
  const local = [...activity.local.values()].find((l) => l.project === p.id);
  return local ? { kind: local.key.split(":")[2], label: local.label, started: local.started } : null;
}

function renderProject(p) {
  state.project = p; state.dirty = false;
  $("#studio-list").hidden = true;
  const ed = $("#studio-editor"); ed.hidden = false;
  const plan = p.plan;
  const scenes = plan?.scenes || [];
  const imgs = scenes.filter((s) => s.image).length, vids = scenes.filter((s) => s.video).length;
  const keyframesDone = scenes.length > 0 && scenes.every((s) => s.image && !s.jobs?.image);
  const end = scenes.length ? scenes[scenes.length - 1].end : 0;
  const task = runningTask(p);
  const folds = readFolds(ed);
  const lock = (kind, label, idle) => (task?.kind === kind ? `disabled><span class="spinner"></span> ${label}` : `>${idle}`);
  ed.innerHTML = `<button class="ghost small back" id="back">← All videos</button>
    <div class="editor-head">
      <div><h1>${esc(plan?.title || p.brief.topic)}</h1><p class="muted">${esc(p.brief.lesson || "")} ${p.brief.setting ? "· " + esc(p.brief.setting) : ""}</p></div>
      <div class="row"><button class="small primary" id="make-all">${autopilotOn(p.id) ? "Stop making this video" : "Make the whole video"}</button><button class="small" id="edit-brief">Edit brief</button><button class="small ghost danger" id="del-project">Delete</button></div>
    </div>
    ${task ? `<div class="banner"><span class="spinner"></span> <b>${esc(task.label)}…</b> <span id="task-eta" data-kind="${esc(task.kind)}">${task.kind === "render" ? `Expect ${roughTime(renderEstimate(scenes))}` : `Started ${ago(task.started)}`}</span>. You can leave this page or close the tab; you'll get a notice when it's done.</div>` : ""}
    <div class="steps">
      <span class="step ${plan ? "done" : ""}">1. Song</span>
      <span class="step ${p.song ? "done" : ""}">2. Music</span>
      <span class="step ${scenes.length && imgs === scenes.length ? "done" : ""}">3. Keyframes ${imgs}/${scenes.length}</span>
      <span class="step ${scenes.length && vids === scenes.length ? "done" : ""}">4. Animation ${vids}/${scenes.length}</span>
      <span class="step ${p.final ? "done" : ""}">5. Publish</span>
    </div>

    <section class="panel card">
      <div class="panel-head"><h2>Song and storyboard</h2>
        <button class="${plan ? "" : "primary"}" id="write-song" ${lock("write", "Writing…", plan ? "Rewrite with Claude" : "Write song with Claude")}</button></div>
      ${plan ? `<label>Music style <span class="muted">${plan.bpm} BPM · ${fmtTime(end)}</span><input id="music-style" value="${esc(plan.music_style)}"></label>
        <details class="fold" data-fold="${p.id}:lyrics" ${p.song ? "" : "open"}><summary>Lyrics · ${plan.sections.length} sections</summary>
        <div class="sections">${plan.sections.map((s, i) => `<div><label>${esc(s.name)} <span class="muted">(${Math.round(s.duration_seconds)}s)</span>
          <textarea data-sec="${i}" rows="${Math.max(2, s.lyrics.split("\n").length + 1)}">${esc(s.lyrics)}</textarea></label></div>`).join("")}</div>
        <div class="row"><button class="small" id="save-lyrics">Save lyric edits</button></div></details>`
      : `<p class="muted">Claude writes original lyrics, a music style prompt, timed sections and a shot list that uses your cast.</p>`}
    </section>

    ${plan ? `<section class="panel card">
      <div class="panel-head"><h2>Music</h2>
        <div class="row">
          <button class="${p.song ? "" : "primary"}" id="compose" ${lock("compose", "Composing…", "Generate with ElevenLabs")}</button>
          <label style="margin:0"><input type="file" id="song-file" accept=".mp3,.wav,audio/mpeg,audio/wav" hidden><span class="btn-like">Upload MP3 or WAV</span></label>
        </div></div>
      ${p.song ? `<audio controls src="${esc(p.song.url)}" style="width:100%"></audio>
        <p class="muted">Song length ${fmtTime(p.song.duration)} · storyboard ${fmtTime(end)}
        ${Math.abs(p.song.duration - end) > 1 ? ` <button class="small primary" id="retime">Fit scenes to song</button>` : " ✓ in sync"}</p>`
      : `<p class="muted">Generate the vocals and music from these lyrics, or make the song in Suno or elsewhere and upload it. Scenes can be re-timed to fit.</p>`}
    </section>

    <section class="panel">
      <div class="panel-head"><h2>Scenes</h2>
        <div class="row"><button id="all-images" class="${keyframesDone ? "" : "primary"}">Generate all keyframes</button><button id="all-videos" class="${keyframesDone ? "primary" : ""}" ${keyframesDone ? "" : "disabled"} title="${keyframesDone ? "" : "Unlocks when every scene has its keyframe"}">Animate all</button></div></div>
      ${keyframesDone ? "" : `<p class="muted">Animate all unlocks when all ${scenes.length} keyframes are done (${imgs} so far).</p>`}
      <div class="scenes">${scenes.map(sceneCard).join("")}</div>
    </section>

    <section class="panel card">
      <h2>Render and publish</h2>
      <div class="publish">
        <div>
          ${p.final ? `<video controls src="${esc(p.final)}"></video><p><a href="${esc(p.final)}" download>Download MP4</a></p>` : `<p class="muted">Stitches every scene to the song timing. Scenes without video use their keyframe with a slow zoom, so you can render a draft any time.</p>`}
          ${p.render_note ? `<p class="error">${esc(p.render_note)}</p>` : ""}
          <label class="check"><input type="checkbox" id="captions" ${state.settings.sing_along_captions !== false ? "checked" : ""}> Sing-along lyrics: karaoke captions where each word lights up as it's sung</label>
          <label class="check"><input type="checkbox" id="add-intro" ${state.settings.add_intro !== false ? "checked" : ""}> Open with the animated channel intro (make it in Settings)</label>
          <label class="check"><input type="checkbox" id="add-outro" ${state.settings.add_outro !== false ? "checked" : ""}> End with an outro: meet the cast, then like and subscribe</label>
          <div class="row"><button class="primary" id="render" ${lock("render", "Rendering…", p.final ? "Re-render" : "Render video")}</button><span class="muted">Takes ${roughTime(renderEstimate(scenes))}, no credits used</span></div>
        </div>
        <div>
          <div class="thumb">${p.thumbnail ? `<img src="${esc(p.thumbnail)}" alt="Thumbnail">` : p.thumb_job ? `<span class="spinner"></span>` : "No thumbnail yet"}</div>
          ${p.thumb_error ? `<p class="error">${esc(p.thumb_error)}</p>` : ""}
          <button class="small" id="thumb">${p.thumbnail ? "New thumbnail" : "Generate thumbnail"}</button>
          <label>YouTube title <input id="yt-title" value="${esc(plan.youtube_title)}"></label>
          <label>Description <textarea id="yt-desc" rows="6">${esc(plan.youtube_description)}</textarea></label>
          <label>Tags <textarea id="yt-tags" rows="2">${esc(plan.tags.join(", "))}</textarea></label>
          <div class="row"><button class="small" id="save-meta">Save</button><button class="small" id="copy-meta">Copy all</button></div>
          <p class="hint">When uploading: mark the video "Made for kids" and turn on YouTube's altered or synthetic content label.</p>
        </div>
      </div>
    </section>` : ""}`;
  applyFolds(ed, folds);
  bindEditor(p);
  updateTaskBanner();
  autopilotStep(p);
}

// ------------------------------------------------------------ studio: make the whole video
// Presses each step's button when the one before it is done. It runs while this video is open
// and picks up where it left off when you come back. Any failure stops it, so nothing is paid for twice.
const AUTOPILOT_KEY = "kidsong.autopilot", AUTOPILOT_RETRY_MS = 20000;
const USD_PER_SCENE = { lipsync: 0.85, kling_i2v: 0.45, wan_i2v: 0.85, ltx_fast: 0.85 };
const autopilot = { step: "", at: 0, batching: false };
function autopilotOn(pid) { return readLocal(AUTOPILOT_KEY) === pid; }
function autopilotNext(p) {
  const scenes = p.plan?.scenes || [];
  if (!p.plan) return { step: "write", button: "#write-song" };
  if (!p.song) return { step: "compose", button: "#compose" };
  if (scenes.some((s) => s.errors?.image || s.errors?.video)) return { stop: "A scene failed. Fix or redo it, then press Make the whole video again." };
  if (scenes.some((s) => s.jobs?.image || s.jobs?.video) || p.thumb_job) return {};
  if (scenes.some((s) => !s.image)) return { step: "keyframes", button: "#all-images" };
  if (scenes.some((s) => !s.video)) return { step: "animation", button: "#all-videos" };
  if (!p.thumbnail) return p.thumb_error ? { stop: `The thumbnail failed: ${p.thumb_error}` } : { step: "thumbnail", button: "#thumb" };
  if (!p.final) return { step: "render", button: "#render" };
  return { stop: "Your video is ready to download and publish.", done: true };
}
function autopilotStep(p) {
  if (!autopilotOn(p.id) || autopilot.batching || runningTask(p)) return;
  const next = autopilotNext(p);
  if (next.stop) {
    writeLocal(AUTOPILOT_KEY, null);
    toast(next.stop, !next.done);
    $("#make-all").textContent = "Make the whole video";
    return;
  }
  if (!next.step || (autopilot.step === next.step && Date.now() - autopilot.at < AUTOPILOT_RETRY_MS)) return;
  autopilot.step = next.step; autopilot.at = Date.now();
  setTimeout(() => { if (viewing(p.id) && autopilotOn(p.id)) $(next.button)?.click(); }, 0);
}
function toggleAutopilot(p) {
  if (autopilotOn(p.id)) {
    writeLocal(AUTOPILOT_KEY, null);
    toast("Stopped. Anything already started will still finish.");
    renderProject(p);
    return;
  }
  if (!hasKeys("anthropic_api_key", "elevenlabs_api_key", "hf_key_id", "hf_key_secret")) return;
  const count = p.plan?.scenes.length || Math.ceil((+p.brief.length || 150) / 5);
  const left = p.plan ? p.plan.scenes.filter((s) => !s.video).length : count;
  const cost = left * (USD_PER_SCENE[state.settings.video_mode] ?? 0.85) + 1;
  if (!confirm(`Make everything still missing: song, music, keyframes, animation, thumbnail and final render. This costs about $${Math.round(cost)}. Keep this video open while it runs.`)) return;
  writeLocal(AUTOPILOT_KEY, p.id);
  renderProject(p);
}

function sceneCard(s) {
  const jobs = s.jobs || {}, errs = s.errors || {};
  const media = s.video ? `<video src="${esc(s.video)}" preload="none" controls muted loop playsinline poster="${esc(s.image || "")}"></video>`
    : s.image ? `<img src="${esc(s.image)}" alt="Scene ${s.n} keyframe" loading="lazy">`
    : jobs.image ? `<span><span class="spinner"></span><br>painting keyframe…</span>` : `<span>No keyframe yet</span>`;
  return `<div class="card scene" data-n="${s.n}">
    <div class="media">${media}</div>
    <div class="scene-body">
      <div class="meta"><span>Scene ${s.n} · ${esc(s.section)}</span><span>${fmtTime(s.start)}–${fmtTime(s.end)}</span></div>
      <p class="lyric">${s.sung !== undefined ? (s.sung ? `<span class="sung-label">Sung here:</span> ${esc(s.sung)}` : "<span class='muted'>No words in this part of the song</span>") : esc(s.lyric) || "<span class='muted'>Instrumental</span>"}</p>
      <div class="chips">${s.cast.map((id) => `<span class="chip">${esc(castName(id))}</span>`).join("")}</div>
      ${errs.image ? `<p class="error">Keyframe: ${esc(errs.image)}</p>` : ""}${errs.video ? `<p class="error">Video: ${esc(errs.video)}</p>` : ""}
      <div class="row">
        <button class="small" data-act="image" ${jobs.image ? "disabled" : ""}>${jobs.image ? '<span class="spinner"></span> Keyframe' : s.image ? "Redo keyframe" : "Keyframe"}</button>
        <button class="small ${s.image && !s.video ? "primary" : ""}" data-act="video" ${!s.image || jobs.video ? "disabled" : ""}>${jobs.video ? '<span class="spinner"></span> Animating' : s.video ? "Re-animate" : "Animate"}</button>
      </div>
      <details class="fold" data-fold="scene:${s.n}"><summary>Edit shot</summary>
        <label>Shot <textarea data-k="visual" rows="3">${esc(s.visual)}</textarea></label>
        <label>Action <textarea data-k="action" rows="2">${esc(s.action)}</textarea></label>
      </details>
    </div></div>`;
}

function collectPlanEdits() {
  const p = state.project, plan = p.plan;
  return {
    music_style: $("#music-style")?.value ?? plan.music_style,
    youtube_title: $("#yt-title")?.value ?? plan.youtube_title,
    youtube_description: $("#yt-desc")?.value ?? plan.youtube_description,
    tags: ($("#yt-tags")?.value ?? plan.tags.join(",")).split(",").map((t) => t.trim()).filter(Boolean),
    sections: plan.sections.map((s, i) => ({ lyrics: $(`[data-sec="${i}"]`)?.value ?? s.lyrics })),
    scenes: plan.scenes.map((s) => {
      const card = $(`.scene[data-n="${s.n}"]`);
      return { n: s.n, visual: $("[data-k=visual]", card)?.value ?? s.visual, action: $("[data-k=action]", card)?.value ?? s.action };
    }),
  };
}

async function savePlan() {
  const p = state.project;
  const saved = await queued(() => api("PUT", `/api/projects/${p.id}`, { plan: collectPlanEdits() }));
  state.dirty = false;
  return saved;
}

// Is this video open on screen right now?
function viewing(pid) {
  return state.project?.id === pid && tabActive("studio") && !$("#studio-editor").hidden;
}

// Reload the open video after background work changed it, keeping any text you typed.
async function refreshOpenProject(keepEdits = true) {
  const pid = state.project?.id; if (!pid) return;
  try {
    if (keepEdits && state.dirty && state.project.plan) await savePlan();
    const fresh = await api("GET", `/api/projects/${pid}`);
    if (viewing(pid)) renderProject(fresh);
  } catch (e) { toast(e.message, true); }
}

// Each project action: save text edits first, then run it. The result shows up here only if
// you're still on this video; otherwise you get a notice. Long tasks (long = {kind, label}) save
// onto the latest copy server-side, so they skip the queue and don't hold up other edits.
function action(btn, fn, needs = [], long = null) {
  if (needs.length && !hasKeys(...needs)) return;
  const pid = state.project.id, title = state.project.plan?.title || state.project.brief.topic;
  return busy(btn, async () => {
    const saved = state.project.plan ? await savePlan() : state.project;
    let run;
    if (long) {
      run = trackLocal(`prj:${pid}:${long.kind}`, long.label, fn(), { project: pid, title });
      if (viewing(pid)) renderProject(saved); // show the "running" banner right away
    } else run = queued(fn);
    const p = await run;
    if (viewing(pid)) renderProject(p);
    kickActivity();
    return p;
  });
}

async function writeSong(btn) {
  if (!btn || !hasKeys("anthropic_api_key")) return;
  if (state.project.plan && !confirm("Rewrite the song and storyboard? Current scenes and media links will be replaced.")) return;
  toast("Claude is writing the song and storyboard (a minute or two). Feel free to keep working; you'll get a notice.");
  const pid = state.project.id;
  return action(btn, () => api("POST", `/api/projects/${pid}/write`), [],
    { kind: "write", label: "Writing song & storyboard" });
}

function bindEditor(p) {
  const id = p.id;
  $("#studio-editor").oninput = () => { state.dirty = true; };
  $("#back").onclick = loadProjects;
  $("#edit-brief").onclick = () => openBriefForm(p.brief, p);
  $("#make-all").onclick = () => toggleAutopilot(state.project);
  $("#del-project").onclick = async (e) => {
    if (!confirm("Delete this video and all its media?")) return;
    await busy(e.target, () => queued(() => api("DELETE", `/api/projects/${id}`)));
    loadProjects();
  };
  $("#write-song").onclick = (e) => writeSong(e.target);
  if (!p.plan) return;
  $("#save-lyrics").onclick = (e) => busy(e.target, async () => { renderProject(await savePlan()); toast("Saved."); });
  $("#save-meta").onclick = (e) => busy(e.target, async () => { renderProject(await savePlan()); toast("Saved."); });
  $("#copy-meta").onclick = async () => {
    const text = `${$("#yt-title").value}\n\n${$("#yt-desc").value}\n\nTags: ${$("#yt-tags").value}`;
    try { await navigator.clipboard.writeText(text); toast("Copied."); } catch { toast("Couldn't copy. Select the text manually.", true); }
  };
  $("#compose").onclick = (e) => {
    toast("ElevenLabs is composing (a couple of minutes). Feel free to keep working; you'll get a notice.");
    action(e.target, () => api("POST", `/api/projects/${id}/song/compose`), ["elevenlabs_api_key"],
      { kind: "compose", label: "Composing music" });
  };
  $("#song-file").onchange = (e) => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    action($("#compose"), () => api("POST", `/api/projects/${id}/song/upload`, fd));
  };
  const retime = $("#retime"); if (retime) retime.onclick = (e) => action(e.target, () => api("POST", `/api/projects/${id}/retime`));
  $("#thumb").onclick = (e) => action(e.target, () => api("POST", `/api/projects/${id}/thumbnail`), ["hf_key_id", "hf_key_secret"]);
  for (const [box, key] of [["#captions", "sing_along_captions"], ["#add-intro", "add_intro"], ["#add-outro", "add_outro"]]) {
    $(box).onchange = (e) => saveSettings({ settings: { [key]: e.target.checked } }).catch((err) => toast(err.message, true));
  }
  $("#render").onclick = (e) => {
    toast("Rendering (longer videos take a few minutes). Feel free to keep working; you'll get a notice.");
    action(e.target, () => api("POST", `/api/projects/${id}/render`), [], { kind: "render", label: "Rendering video" });
  };
  $$(".scene button[data-act]").forEach((b) => (b.onclick = (e) => {
    const btn = e.target.closest("button"), n = btn.closest(".scene").dataset.n;
    action(btn, () => api("POST", `/api/projects/${id}/scenes/${n}/${btn.dataset.act}`), ["hf_key_id", "hf_key_secret"]);
  }));
  $("#all-images").onclick = (e) => batch(e.target, "image", (s) => !s.image && !s.jobs?.image);
  $("#all-videos").onclick = (e) => batch(e.target, "video", (s) => s.image && !s.video && !s.jobs?.video);
}

async function batch(btn, kind, want) {
  if (!hasKeys("hf_key_id", "hf_key_secret")) return;
  const todo = state.project.plan.scenes.filter(want).map((s) => s.n);
  if (!todo.length) { toast(kind === "video" ? "Every scene with a keyframe is already animated." : "Every scene already has a keyframe."); return; }
  const pid = state.project.id;
  autopilot.batching = true;
  await busy(btn, async () => {
    await savePlan();
    let p, queuedCount = 0;
    for (const n of todo) {
      try { p = await queued(() => api("POST", `/api/projects/${pid}/scenes/${n}/${kind}`)); queuedCount++; }
      catch (e) { toast(`Scene ${n}: ${e.message}`, true); break; }
      if (viewing(pid)) renderProject(p);
    }
    kickActivity();
    if (queuedCount) toast(`Queued ${queuedCount} ${kind === "video" ? "animations" : "keyframes"} on Higgsfield. They keep going if you leave this page.`);
  });
  autopilot.batching = false;
}

// ------------------------------------------------------------ boot
// Older versions kept API keys in this browser: move them to the server once, then forget them here.
async function migrateLegacySettings() {
  let legacy;
  try { legacy = JSON.parse(readLocal(LEGACY_SETTINGS_KEY) || "null"); } catch { legacy = null; }
  if (!legacy) return;
  const keys = {};
  for (const k of SECRET_FIELDS) if (legacy[k] && !state.keys[k]?.set) keys[k] = legacy[k];
  if (legacy.app_password && !state.password) { state.password = legacy.app_password; writeLocal(PASSWORD_KEY, state.password); }
  if (Object.keys(keys).length) {
    await saveSettings({ keys });
    toast("Moved the API keys saved in this browser to your account, so they now work on every device.");
  }
  writeLocal(LEGACY_SETTINGS_KEY, null);
}

(async function boot() {
  state.password = readLocal(PASSWORD_KEY) || "";
  try { state.config = await (await fetch("/api/config")).json(); }
  catch { state.config = { defaults: {}, storage: "local", password_required: false }; }
  state.settings = { ...state.config.defaults };
  try {
    applySettingsView(await api("GET", "/api/settings"));
    await migrateLegacySettings();
  } catch (e) { toast(e.message, true); }
  fillSettingsForm();
  if (!state.keys.anthropic_api_key?.set || !state.keys.hf_key_id?.set) { showTab("settings"); toast("Welcome! Add your API keys to get started."); }
  else showTab("studio");
  loadCharacters().catch(() => {});
  activityTick();
})();
