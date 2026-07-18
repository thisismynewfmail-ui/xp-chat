/* ═══════════════════════════════════════════════════════════════════════
   TACHI-COM v2.1 — frontend
   All state lives on the server; this view syncs over SSE so every open
   instance (tabs, machines) shows the same chats, settings and streams.
   ═══════════════════════════════════════════════════════════════════════ */
"use strict";

const $ = (id) => document.getElementById(id);
const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

const S = {
  settings: null,
  status: {},
  chats: [],
  presets: {},        // name -> jinja text
  presetNames: [],
  chat: null,          // currently open chat object
  busy: new Set(),     // chat ids currently generating
  dirty: false,        // unsaved settings edits
  stream: {},          // mid -> {contentEl, thinkBody, thinkBar, msgEl, think}
  tokTimes: [],        // timestamps of recent deltas (tps + spectrum)
  pinned: true,        // chat autoscroll pinned to bottom
};

const LS = {
  get chatId() { return localStorage.getItem("tachicom.chat"); },
  set chatId(v) { v ? localStorage.setItem("tachicom.chat", v)
                    : localStorage.removeItem("tachicom.chat"); },
};

/* ── API helpers ─────────────────────────────────────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" } }, opts));
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}
const GET = (p) => api(p);
const POST = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
const PUT = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });
const DEL = (p) => api(p, { method: "DELETE" });

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ── tiny markdown (escaped-first, safe) ─────────────────────────────── */
function mdInline(t) {
  let h = escapeHtml(t);
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  h = h.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>");
  h = h.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return h;
}

function mdBlocks(text) {
  const out = []; let list = null, quote = null;
  const flushL = () => { if (list) { out.push("<ul>" + list.join("") + "</ul>"); list = null; } };
  const flushQ = () => { if (quote) { out.push("<blockquote>" + quote.join("\n") + "</blockquote>"); quote = null; } };
  for (const ln of text.split("\n")) {
    let m;
    if ((m = ln.match(/^\s*[-*] (.*)$/))) { flushQ(); (list = list || []).push("<li>" + mdInline(m[1]) + "</li>"); }
    else if ((m = ln.match(/^> ?(.*)$/))) { flushL(); (quote = quote || []).push(mdInline(m[1])); }
    else if ((m = ln.match(/^(#{1,3}) (.*)$/))) { flushL(); flushQ(); const n = m[1].length; out.push(`<h${n}>` + mdInline(m[2]) + `</h${n}>`); }
    else { flushL(); flushQ(); out.push(mdInline(ln)); }
  }
  flushL(); flushQ();
  return out.join("\n").replace(/(<\/(?:ul|blockquote|h[1-3])>)\n/g, "$1");
}

function md(src) {
  const parts = String(src || "").split("```");
  const out = [];
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      let code = parts[i];
      const nl = code.indexOf("\n");
      if (nl >= 0 && nl < 30 && /^[\w#+.-]*$/.test(code.slice(0, nl).trim()))
        code = code.slice(nl + 1);
      out.push("<pre><code>" + escapeHtml(code.replace(/\n$/, "")) + "</code></pre>");
    } else out.push(mdBlocks(parts[i]));
  }
  return out.join("");
}

/* ══ WINDOW MANAGER ═══════════════════════════════════════════════════ */
let zTop = 20;
const WINS = {};

function initWindows() {
  $$(".win").forEach((win) => {
    const id = win.id;
    WINS[id] = win;
    const bar = win.querySelector(".tbar");
    // restore saved position
    try {
      const pos = JSON.parse(localStorage.getItem("tachicom.pos." + id) || "null");
      if (pos) { win.style.left = pos.l; win.style.top = pos.t;
        if (pos.w) win.style.width = pos.w; if (pos.h) win.style.height = pos.h; }
    } catch (e) {}
    win.addEventListener("mousedown", () => focusWin(id));
    bar.addEventListener("mousedown", (e) => {
      if (e.target.closest(".tb")) return;
      if (win.classList.contains("maxed")) return;
      const startX = e.clientX, startY = e.clientY;
      const ox = win.offsetLeft, oy = win.offsetTop;
      const move = (ev) => {
        win.style.left = Math.max(-40, ox + ev.clientX - startX) + "px";
        win.style.top = Math.max(0, oy + ev.clientY - startY) + "px";
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        localStorage.setItem("tachicom.pos." + id, JSON.stringify(
          { l: win.style.left, t: win.style.top, w: win.style.width, h: win.style.height }));
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      e.preventDefault();
    });
    bar.addEventListener("dblclick", (e) => {
      if (!e.target.closest(".tb")) toggleMax(id);
    });
    win.querySelectorAll(".tb").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      const act = b.dataset.act;
      if (act === "close") closeWin(id);
      else if (act === "min") minimizeWin(id);
      else if (act === "max") toggleMax(id);
    }));
  });
}

function focusWin(id) {
  const win = WINS[id];
  if (!win) return;
  $$(".win").forEach((w) => w.classList.add("inactive"));
  win.classList.remove("inactive");
  win.style.zIndex = ++zTop;
  $$(".taskbtn").forEach((b) => b.classList.toggle("active", b.dataset.win === id));
}

function openWin(id) {
  const win = WINS[id];
  if (!win) return;
  win.hidden = false;
  win.classList.remove("minimized");
  ensureTaskBtn(id);
  focusWin(id);
}

function closeWin(id) {
  const win = WINS[id];
  win.hidden = true;
  const btn = $$(".taskbtn").find((b) => b.dataset.win === id);
  if (btn) btn.remove();
}

function minimizeWin(id) {
  WINS[id].classList.add("minimized");
  ensureTaskBtn(id);
  $$(".taskbtn").forEach((b) => b.classList.remove("active"));
}

function toggleMax(id) { WINS[id].classList.toggle("maxed"); }

function ensureTaskBtn(id) {
  if ($$(".taskbtn").some((b) => b.dataset.win === id)) return;
  const win = WINS[id];
  const btn = document.createElement("button");
  btn.className = "taskbtn";
  btn.dataset.win = id;
  btn.innerHTML = `<span>${win.querySelector(".ticon").textContent}</span>` +
    `<span>${escapeHtml(win.querySelector(".ttext").textContent.split("—")[0].trim())}</span>`;
  btn.addEventListener("click", () => {
    const w = WINS[id];
    if (w.classList.contains("minimized") || w.hidden) openWin(id);
    else if (w.style.zIndex == zTop) minimizeWin(id);
    else focusWin(id);
  });
  $("task-buttons").appendChild(btn);
}

/* ══ MENUS ════════════════════════════════════════════════════════════ */
function thinkModeLabel(m) {
  return { collapsed: "Collapsed", expanded: "Expanded", hidden: "Hidden" }[m] || m;
}

const menuDefs = {
  file: () => [
    { label: "New session", act: () => newSession() },
    { label: "Save session (.json)", act: () => exportChat("json") },
    { label: "Save transcript (.txt)", act: () => exportChat("txt") },
    { sep: 1 },
    { label: "Close window", act: () => closeWin("win-chat") },
  ],
  session: () => [
    { label: "Rename…", act: () => renameSession(S.chat && S.chat.id) },
    { label: "Duplicate (copy)", act: () => duplicateSession(S.chat && S.chat.id) },
    { label: "Clear messages", act: () => clearSession(S.chat && S.chat.id) },
    { label: "Delete…", act: () => deleteSession(S.chat && S.chat.id) },
    { sep: 1 },
    { label: "Session archive…", act: () => openWin("win-sessions") },
  ],
  view: () => [
    { label: "Net Monitor", check: !WINS["win-monitor"].hidden,
      act: () => WINS["win-monitor"].hidden ? openWin("win-monitor") : closeWin("win-monitor") },
    { label: "Session archive", check: !WINS["win-sessions"].hidden,
      act: () => WINS["win-sessions"].hidden ? openWin("win-sessions") : closeWin("win-sessions") },
    { sep: 1 },
    { label: "Thinking: compacted", check: thinkMode() === "collapsed", act: () => setThinkMode("collapsed") },
    { label: "Thinking: expanded", check: thinkMode() === "expanded", act: () => setThinkMode("expanded") },
    { label: "Thinking: hidden", check: thinkMode() === "hidden", act: () => setThinkMode("hidden") },
    { sep: 1 },
    { label: "CRT scanlines", check: !!S.settings.ui.scanlines,
      act: () => saveUiFlag("scanlines", !S.settings.ui.scanlines) },
    { label: "Animations", check: !!S.settings.ui.animations,
      act: () => saveUiFlag("animations", !S.settings.ui.animations) },
  ],
  settings: () => [
    { label: "Configuration…", act: () => openWin("win-settings") },
    { label: "Test uplink now", act: () => testConnection() },
  ],
  help: () => [
    { label: "About TACHI-COM…", act: () => openWin("win-about") },
  ],
};

function showMenu(anchor, items) {
  const pop = $("menu-popup");
  pop.innerHTML = "";
  items.forEach((it) => {
    if (it.sep) { const d = document.createElement("div"); d.className = "mi-sep"; pop.appendChild(d); return; }
    const d = document.createElement("div");
    d.className = "mi" + (it.check ? " mi-check" : "") + (it.dis ? " mi-dis" : "");
    d.textContent = it.label;
    if (!it.dis) d.addEventListener("click", () => { hideMenus(); it.act(); });
    pop.appendChild(d);
  });
  const r = anchor.getBoundingClientRect();
  pop.hidden = false;
  pop.style.left = Math.min(r.left, window.innerWidth - pop.offsetWidth - 6) + "px";
  pop.style.top = (r.bottom + 1) + "px";
}

function hideMenus() {
  $("menu-popup").hidden = true;
  $("start-menu").hidden = true;
  $("start-btn").classList.remove("open");
  $$(".menu").forEach((m) => m.classList.remove("open"));
}

function initMenus() {
  $$("#chat-menubar .menu").forEach((m) => {
    m.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!S.settings) return;         // state not loaded yet
      const was = m.classList.contains("open");
      hideMenus();
      if (!was) { m.classList.add("open"); showMenu(m, menuDefs[m.dataset.menu]()); }
    });
  });
  $("start-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const sm = $("start-menu");
    const show = sm.hidden;
    hideMenus();
    sm.hidden = !show;
    $("start-btn").classList.toggle("open", show);
  });
  $$("#start-menu .sm-item[data-win]").forEach((it) =>
    it.addEventListener("click", () => { hideMenus(); openWin(it.dataset.win); }));
  $("sm-reboot").addEventListener("click", () => location.reload());
  document.addEventListener("click", hideMenus);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideMenus(); });
}

/* ══ PROMPT DIALOG ════════════════════════════════════════════════════ */
function promptDialog(title, label, value) {
  return new Promise((resolve) => {
    $("prompt-title").textContent = title;
    $("prompt-label").textContent = label;
    const inp = $("prompt-input");
    inp.value = value || "";
    openWin("win-prompt");
    inp.focus(); inp.select();
    const done = (val) => {
      closeWin("win-prompt");
      ok.removeEventListener("click", onOk); cancel.removeEventListener("click", onNo);
      inp.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const ok = $("prompt-ok"), cancel = $("prompt-cancel");
    const onOk = () => done(inp.value);
    const onNo = () => done(null);
    const onKey = (e) => { if (e.key === "Enter") done(inp.value); if (e.key === "Escape") done(null); };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onNo);
    inp.addEventListener("keydown", onKey);
  });
}

/* ══ CHAT RENDERING ═══════════════════════════════════════════════════ */
function thinkMode() { return (S.settings.thinking || {}).display || "collapsed"; }

async function setThinkMode(mode) {
  await PUT("/api/settings", { thinking: { display: mode } });
}

function applyThinkMode() {
  document.body.classList.toggle("think-hidden", thinkMode() === "hidden");
  $("tb-think-label").textContent = thinkModeLabel(thinkMode());
  $$(".think").forEach((t) => {
    if (thinkMode() === "expanded") t.classList.add("open");
    if (thinkMode() === "collapsed" && !t.classList.contains("streaming"))
      t.classList.remove("open");
  });
}

function fmtTime(ts) {
  const d = new Date(ts || Date.now());
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function buildThink(msg, streaming) {
  const think = document.createElement("div");
  think.className = "think";
  if (thinkMode() === "expanded" || streaming) think.classList.add("open");
  if (streaming) think.classList.add("streaming");
  const bar = document.createElement("div");
  bar.className = "think-bar";
  const body = document.createElement("div");
  body.className = "think-body";
  body.textContent = msg.thinking || "";
  const label = () =>
    `THOUGHT PROCESS · ${(body.textContent || "").length} chars`;
  bar.textContent = label();
  bar.addEventListener("click", () => think.classList.toggle("open"));
  think.appendChild(bar); think.appendChild(body);
  think._updateLabel = () => { bar.textContent = label(); };
  if (!msg.thinking && !streaming) think.style.display = "none";
  return think;
}

function buildMessage(msg, opts) {
  opts = opts || {};
  const el = document.createElement("div");
  el.className = "msg msg-" + msg.role;
  el.dataset.mid = msg.id;
  if (opts.outOfCtx) el.classList.add("out-of-ctx");

  const head = document.createElement("div");
  head.className = "msg-head";
  const who = document.createElement("span");
  who.className = "msg-who";
  who.textContent = msg.name || (msg.role === "user" ? S.settings.user_name : S.settings.bot_name);
  const time = document.createElement("span");
  time.className = "msg-time";
  time.textContent = fmtTime(msg.ts);
  head.appendChild(who); head.appendChild(time);

  const tools = document.createElement("span");
  tools.className = "msg-tools";
  const copy = document.createElement("button");
  copy.className = "mtool"; copy.textContent = "copy"; copy.title = "copy message text";
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(msg.content || "").then(
      () => { copy.textContent = "ok!"; setTimeout(() => copy.textContent = "copy", 900); },
      () => { copy.textContent = "err"; });
  });
  tools.appendChild(copy);
  head.appendChild(tools);
  el.appendChild(head);

  let think = null;
  if (msg.role === "assistant") {
    think = buildThink(msg, !!opts.streaming);
    el.appendChild(think);
  }

  const content = document.createElement("div");
  content.className = "msg-content";
  if (opts.streaming) {
    content.textContent = msg.content || "";
    const cur = document.createElement("span");
    cur.className = "cursor";
    content.appendChild(cur);
  } else {
    content.innerHTML = md(msg.content || "");
  }
  el.appendChild(content);

  if (msg.error) {
    const err = document.createElement("div");
    err.className = "msg-error";
    err.textContent = "⚠ " + msg.error;
    el.appendChild(err);
  }
  if (msg.stats && msg.role === "assistant") {
    const st = document.createElement("div");
    st.className = "msg-stats";
    st.textContent = `· ${msg.stats.tokens} chunks · ${msg.stats.tps} t/s · ` +
      `${msg.stats.secs}s · ${msg.stats.finish}`;
    el.appendChild(st);
  }
  return { el, content, think };
}

function renderChat() {
  const log = $("chat-log");
  log.innerHTML = "";
  S.stream = {};
  if (!S.chat) return;

  const sysp = (S.settings.system_prompt || "").trim();
  if (sysp) {
    const n = document.createElement("div");
    n.className = "sys-note";
    n.textContent = `:: system directive loaded (${sysp.length} chars) ::`;
    n.title = sysp;
    log.appendChild(n);
  }

  let beforeCut = !!S.chat.cut_mid;
  for (const msg of S.chat.messages) {
    if (S.chat.cut_mid && msg.id === S.chat.cut_mid) {
      beforeCut = false;
      const div = document.createElement("div");
      div.className = "ctx-cut";
      div.textContent = "DEEP ARCHIVE ─ MESSAGES ABOVE ARE OUT OF CONTEXT";
      log.appendChild(div);
    }
    const streaming = !!msg.streaming;
    const built = buildMessage(msg, { outOfCtx: beforeCut, streaming });
    log.appendChild(built.el);
    if (streaming) registerStream(msg, built);
  }
  scrollChat(true);
  $("chat-title-chip").textContent = S.chat.name || "untitled";
}

function registerStream(msg, built) {
  S.stream[msg.id] = {
    msg, el: built.el,
    contentEl: built.content,
    think: built.think,
  };
}

function scrollChat(force) {
  const log = $("chat-log");
  if (force || S.pinned) log.scrollTop = log.scrollHeight;
}

function appendDelta(mid, field, text) {
  const st = S.stream[mid];
  if (!st) { reloadChat(); return; }
  if (field === "content") {
    st.msg.content = (st.msg.content || "") + text;
    const cur = st.contentEl.querySelector(".cursor");
    st.contentEl.insertBefore(document.createTextNode(text), cur);
  } else {
    st.msg.thinking = (st.msg.thinking || "") + text;
    if (st.think) {
      st.think.style.display = "";
      st.think.querySelector(".think-body").textContent = st.msg.thinking;
      st.think._updateLabel();
      const body = st.think.querySelector(".think-body");
      body.scrollTop = body.scrollHeight;
    }
  }
  S.tokTimes.push(performance.now());
  scrollChat();
}

function finalizeStream(mid, stats, error) {
  const st = S.stream[mid];
  if (!st) return;
  delete S.stream[mid];
  st.msg.streaming = false;
  if (stats) st.msg.stats = stats;
  if (error) st.msg.error = error;
  const rebuilt = buildMessage(st.msg, {});
  st.el.replaceWith(rebuilt.el);
  if (st.think && thinkMode() === "collapsed")
    rebuilt.think && rebuilt.think.classList.remove("open");
  scrollChat();
}

/* ── chat actions ────────────────────────────────────────────────────── */
async function reloadChat() {
  if (S._reloading) { S._reloadAgain = true; return; }
  S._reloading = true;
  try {
    if (!LS.chatId) { S.chat = null; renderChat(); return; }
    try {
      const data = await GET(`/api/chats/${LS.chatId}`);
      S.chat = data.chat;
      if (data.busy) S.busy.add(S.chat.id); else S.busy.delete(S.chat.id);
      renderChat();
      updateCtxMeter(data.ctx);
      updateBusyUI();
    } catch (e) {
      LS.chatId = null; S.chat = null; renderChat();
    }
  } finally {
    S._reloading = false;
    if (S._reloadAgain) { S._reloadAgain = false; reloadChat(); }
  }
}

async function openChat(id) {
  LS.chatId = id;
  await reloadChat();
  renderSessions();
}

async function newSession() {
  const data = await POST("/api/chats", {});
  await openChat(data.chat.id);
  addLog("new session spun up: " + data.chat.name);
}

async function renameSession(id) {
  if (!id) return;
  const cur = S.chats.find((c) => c.id === id);
  const name = await promptDialog("Rename session", "New session name:",
    cur ? cur.name : "");
  if (name && name.trim()) await POST(`/api/chats/${id}/rename`, { name: name.trim() });
}

async function duplicateSession(id) {
  if (!id) return;
  const data = await POST(`/api/chats/${id}/duplicate`);
  if (data.chat) await openChat(data.chat.id);
}

async function clearSession(id) {
  if (!id) return;
  await POST(`/api/chats/${id}/clear`);
}

async function deleteSession(id) {
  if (!id) return;
  const cur = S.chats.find((c) => c.id === id);
  const yes = await promptDialog("Delete session",
    `Type DELETE to erase "${(cur && cur.name) || id}" permanently:`, "");
  if (yes !== "DELETE") return;
  await DEL(`/api/chats/${id}`);
  if (LS.chatId === id) { LS.chatId = null; S.chat = null; }
}

function exportChat(fmt) {
  if (!S.chat) return;
  const a = document.createElement("a");
  a.href = `/api/chats/${S.chat.id}/export?fmt=${fmt}`;
  a.download = "";
  a.click();
}

async function sendMessage() {
  const inp = $("input");
  const text = inp.value.trim();
  if (!text) return;
  if (!S.chat) await newSession();
  if (S.busy.has(S.chat.id)) return;
  try {
    inp.value = "";
    await POST(`/api/chats/${S.chat.id}/send`, { text });
  } catch (e) {
    inp.value = text;
    addLog("send failed: " + e.message);
  }
}

function updateBusyUI() {
  const busy = S.chat && S.busy.has(S.chat.id);
  $("send-btn").disabled = !!busy;
  $("stop-btn").disabled = !busy;
  $("tb-regen").disabled = !!busy || !S.chat ||
    !S.chat.messages.some((m) => m.role === "assistant");
  $("mascot").classList.toggle("busy", S.busy.size > 0);
}

/* ══ SESSIONS LIST ════════════════════════════════════════════════════ */
function renderSessions() {
  const box = $("sess-list");
  box.innerHTML = "";
  S.chats.forEach((c) => {
    const d = document.createElement("div");
    d.className = "sess" + (c.id === LS.chatId ? " sel" : "");
    const when = new Date(c.updated).toLocaleDateString([], { month: "short", day: "numeric" });
    d.innerHTML = `<div class="sess-name"><span>${escapeHtml(c.name)}</span>` +
      `<small>${c.count}✉ ${when}</small></div>` +
      `<div class="sess-prev">${escapeHtml(c.preview || "(empty)")}</div>`;
    d.addEventListener("click", () => openChat(c.id));
    d.addEventListener("dblclick", () => { openChat(c.id); focusWin("win-chat"); });
    box.appendChild(d);
  });
}

async function refreshSessions() {
  const data = await GET("/api/chats");
  S.chats = data.chats;
  renderSessions();
  if (S.chat) {
    const mine = S.chats.find((c) => c.id === S.chat.id);
    if (mine) { S.chat.name = mine.name; $("chat-title-chip").textContent = mine.name; }
    else { LS.chatId = null; S.chat = null; renderChat(); }
  }
}

/* ══ STATUS / CONNECTION ══════════════════════════════════════════════ */
function applyStatus(st) {
  S.status = st || {};
  const on = !!S.status.online;
  const led = $("conn-led"), tray = $("tray-led");
  led.className = "led " + (on ? "led-on" : "led-off");
  tray.className = "led " + (on ? "led-on" : "led-off");
  $("conn-text").textContent = on
    ? `UPLINK ${S.status.latency_ms}ms` : "NO CARRIER";
  const model = S.settings && S.settings.model
    ? S.settings.model : (S.status.models || [])[0] || "—";
  $("sb-model").textContent = model;
  $("sb-model").title = model;
  $("pulled-nctx").textContent = S.status.n_ctx
    ? `${S.status.n_ctx} tokens` : "(not reported)";
  $("pulled-samp").textContent = Object.keys(S.status.server_sampling || {}).length
    ? JSON.stringify(S.status.server_sampling) : "(not reported)";
  const sel = $("set-model");
  const want = (S.settings && S.settings.model) || "";
  sel.innerHTML = '<option value="">(first available)</option>';
  (S.status.models || []).forEach((m) => {
    const o = document.createElement("option");
    o.value = m; o.textContent = m;
    sel.appendChild(o);
  });
  sel.value = want;
  if (window._tplRefresh) window._tplRefresh();
}

async function testConnection() {
  $("test-result").textContent = "probing uplink…";
  openWin("win-settings");
  try {
    const data = await POST("/api/test");
    const st = data.status;
    applyStatus(st);
    $("test-result").textContent = st.online
      ? [`ONLINE · ${st.latency_ms}ms`,
         `models: ${(st.models || []).join(", ") || "(none)"}`,
         `server ctx: ${st.n_ctx || "?"} tokens`,
         `chat template: ${st.chat_template ? "pulled (" + st.chat_template.length + " chars)" : "none reported"}`,
         `sampling defaults: ${JSON.stringify(st.server_sampling || {})}`].join("\n")
      : "OFFLINE · " + (st.error || "unreachable");
    addLog(st.online ? `uplink probe ok (${st.latency_ms}ms)` : "uplink probe FAILED");
  } catch (e) {
    $("test-result").textContent = "probe error: " + e.message;
  }
}

/* ══ CONTEXT METER ════════════════════════════════════════════════════ */
function updateCtxMeter(ctx) {
  if (!ctx) return;
  const limit = ctx.limit || S.settings.context_length || 8196;
  const pct = Math.min(100, Math.round(100 * (ctx.used || 0) / limit));
  $("ctx-fill").style.width = pct + "%";
  $("ctx-label").textContent = `CTX ${ctx.used || 0}/${limit}`;
  $("ctx-tick").style.left = Math.round(100 * (S.settings.context_fill_ratio || 0.75)) + "%";
  $("tray-ctx").textContent = pct + "%";
  $("tray-ctx").title = `context ${ctx.used}/${limit} tokens (trim at ` +
    Math.round(100 * (S.settings.context_fill_ratio || 0.75)) + "%)";
}

/* ══ SETTINGS FORM ════════════════════════════════════════════════════ */
const SAMP_KEYS = ["temperature", "top_p", "top_k", "min_p", "typical_p",
  "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty",
  "max_tokens", "seed"];

function setDirty(d) {
  S.dirty = d;
  const el = $("settings-dirty");
  el.textContent = d ? "unsaved changes" : "saved";
  el.classList.toggle("dirty", d);
}

function fillSettingsForm() {
  const s = S.settings;
  $("set-endpoint").value = s.endpoint || "";
  $("set-api_key").value = s.api_key || "";
  $("set-model").value = s.model || "";
  $("set-context_length").value = s.context_length;
  $("set-context_fill_ratio").value = Math.round(100 * s.context_fill_ratio);
  $("fill-label").textContent = Math.round(100 * s.context_fill_ratio) + "%";
  $("set-stream").checked = !!s.stream;
  $("set-system_prompt").value = s.system_prompt || "";
  $("set-user_name").value = s.user_name || "User";
  $("set-bot_name").value = s.bot_name || "";
  $("set-think-enabled").checked = !!(s.thinking || {}).enabled;
  $("set-think-display").value = thinkMode();
  SAMP_KEYS.forEach((k) => { $("samp-" + k).value = (s.sampling || {})[k]; });
  $$('input[name="tplmode"]').forEach((r) => r.checked = r.value === s.template_mode);
  const psel = $("set-template_preset");
  psel.innerHTML = "";
  S.presetNames.forEach((n) => {
    const o = document.createElement("option");
    o.value = n; o.textContent = n; psel.appendChild(o);
  });
  psel.value = s.template_preset || S.presetNames[0] || "";
  refreshTplEditor();
  $("set-ui-scanlines").checked = !!s.ui.scanlines;
  $("set-ui-animations").checked = !!s.ui.animations;
  $("set-ui-netmon").checked = !!s.ui.net_monitor;
  setDirty(false);
}

function collectSettingsForm() {
  const num = (id, fallback) => {
    const v = parseFloat($(id).value);
    return isNaN(v) ? fallback : v;
  };
  const sampling = {};
  SAMP_KEYS.forEach((k) => {
    const v = parseFloat($("samp-" + k).value);
    if (!isNaN(v)) sampling[k] = v;
  });
  const mode = ($$('input[name="tplmode"]').find((r) => r.checked) || {}).value || "auto";
  return {
    endpoint: $("set-endpoint").value.trim(),
    api_key: $("set-api_key").value,
    model: $("set-model").value,
    context_length: Math.round(num("set-context_length", 8196)),
    context_fill_ratio: num("set-context_fill_ratio", 75) / 100,
    stream: $("set-stream").checked,
    system_prompt: $("set-system_prompt").value,
    user_name: $("set-user_name").value.trim() || "User",
    bot_name: $("set-bot_name").value.trim() || "Tachikoma",
    thinking: {
      enabled: $("set-think-enabled").checked,
      display: $("set-think-display").value,
    },
    sampling,
    template_mode: mode,
    template_preset: $("set-template_preset").value,
    template_custom: mode === "custom" ? $("tpl-editor").value : (S.settings.template_custom || ""),
    ui: {
      scanlines: $("set-ui-scanlines").checked,
      animations: $("set-ui-animations").checked,
      net_monitor: $("set-ui-netmon").checked,
    },
  };
}

function refreshTplEditor() {
  const mode = ($$('input[name="tplmode"]').find((r) => r.checked) || {}).value || "auto";
  const ed = $("tpl-editor");
  if (mode === "auto") {
    ed.value = S.status.chat_template || "(no template pulled from the endpoint yet — hit Test connection)";
    ed.readOnly = true;
  } else if (mode === "preset") {
    ed.value = S.presets[$("set-template_preset").value] || "";
    ed.readOnly = true;
  } else {
    if (ed.readOnly) ed.value = S.settings.template_custom || ed.value;
    ed.readOnly = false;
  }
}
window._tplRefresh = refreshTplEditor;

function applySettingsSideEffects() {
  const s = S.settings;
  document.body.classList.toggle("no-scanlines", !s.ui.scanlines);
  document.body.classList.toggle("no-anim", !s.ui.animations);
  applyThinkMode();
  applyStatus(S.status);
  updateCtxMeter({ used: parseInt(($("ctx-label").textContent.match(/ (\d+)\//) || [0, 0])[1], 10),
                   limit: s.context_length });
}

async function saveSettings() {
  const patch = collectSettingsForm();
  const data = await PUT("/api/settings", patch);
  S.settings = data.settings;
  fillSettingsForm();
  applySettingsSideEffects();
  addLog("configuration written to persistent store");
}

async function saveUiFlag(flag, val) {
  const ui = {}; ui[flag] = val;
  await PUT("/api/settings", { ui });
}

function initSettings() {
  $$("#settings-tabs .tab").forEach((t) => t.addEventListener("click", () => {
    $$("#settings-tabs .tab").forEach((x) => x.classList.remove("tab-on"));
    t.classList.add("tab-on");
    $$(".tabpage").forEach((p) => p.hidden = p.id !== t.dataset.tab);
  }));
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("btn-revert-settings").addEventListener("click", () => { fillSettingsForm(); });
  $("btn-test").addEventListener("click", testConnection);
  $("btn-pull-samp").addEventListener("click", () => {
    const sv = S.status.server_sampling || {};
    let n = 0;
    SAMP_KEYS.forEach((k) => { if (sv[k] !== undefined) { $("samp-" + k).value = sv[k]; n++; } });
    setDirty(true);
    addLog(`server sampling defaults applied to form (${n} keys) — hit Save`);
  });
  $("set-context_fill_ratio").addEventListener("input", () => {
    $("fill-label").textContent = $("set-context_fill_ratio").value + "%";
  });
  $$('input[name="tplmode"]').forEach((r) =>
    r.addEventListener("change", refreshTplEditor));
  $("set-template_preset").addEventListener("change", refreshTplEditor);
  $("btn-tpl-pull").addEventListener("click", () => {
    if (!S.status.chat_template) { addLog("no template pulled from endpoint yet"); return; }
    $$('input[name="tplmode"]').forEach((r) => r.checked = r.value === "custom");
    $("tpl-editor").readOnly = false;
    $("tpl-editor").value = S.status.chat_template;
    setDirty(true);
  });
  $("btn-tpl-preset").addEventListener("click", () => {
    $$('input[name="tplmode"]').forEach((r) => r.checked = r.value === "custom");
    $("tpl-editor").readOnly = false;
    $("tpl-editor").value = S.presets[$("set-template_preset").value] || "";
    setDirty(true);
  });
  $$(".settings-body input, .settings-body select, .settings-body textarea")
    .forEach((el) => el.addEventListener("input", () => setDirty(true)));
}

/* ══ SSE SYNC ═════════════════════════════════════════════════════════ */
function initSSE() {
  const es = new EventSource("/api/events");
  es.onopen = () => { fetchState(false); };
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    switch (ev.type) {
      case "hello": applyStatus(ev.status); break;
      case "status": applyStatus(ev.status); break;
      case "settings":
        S.settings = ev.settings;
        if (!S.dirty) fillSettingsForm();
        applySettingsSideEffects();
        renderChat();
        break;
      case "chats": refreshSessions(); break;
      case "chat":
        if (S.chat && ev.chat === S.chat.id) reloadChat();
        break;
      case "gen_start":
        S.busy.add(ev.chat);
        if (S.chat && ev.chat === S.chat.id) {
          updateCtxMeter(ev.ctx);
          if (ev.ctx && ev.ctx.cut_mid !== S.chat.cut_mid) {
            S.chat.cut_mid = ev.ctx.cut_mid;
            reloadChat();
          } else if (!S.chat.messages.some((m) => m.id === ev.mid)) {
            const msg = { id: ev.mid, role: "assistant", name: ev.name,
              content: "", thinking: "", ts: Date.now(), streaming: true };
            S.chat.messages.push(msg);
            const built = buildMessage(msg, { streaming: true });
            $("chat-log").appendChild(built.el);
            registerStream(msg, built);
            scrollChat(true);
          } else if (!S.stream[ev.mid]) {
            reloadChat();
          }
        }
        updateBusyUI();
        break;
      case "delta":
        if (S.chat && ev.chat === S.chat.id) appendDelta(ev.mid, ev.field, ev.text);
        break;
      case "gen_end":
        S.busy.delete(ev.chat);
        if (S.chat && ev.chat === S.chat.id) {
          finalizeStream(ev.mid, ev.stats, ev.error);
          if (ev.stats) $("sb-tps").textContent = ev.stats.tps + " t/s";
          // pull the authoritative saved chat (heals any missed deltas)
          reloadChat();
        }
        updateBusyUI();
        break;
      case "log": addLog(ev.text); break;
    }
  };
}

async function fetchState(first) {
  const data = await GET("/api/state");
  S.settings = data.settings;
  S.chats = data.chats;
  S.presetNames = data.presets || [];
  applyStatus(data.status);
  if (first || !S.dirty) fillSettingsForm();
  applySettingsSideEffects();
  renderSessions();
  if (!LS.chatId || !S.chats.some((c) => c.id === LS.chatId)) {
    if (S.chats.length) LS.chatId = S.chats[0].id;
    else { const d = await POST("/api/chats", { name: "session 001" }); LS.chatId = d.chat.id; }
  }
  await reloadChat();
}

/* ══ NET LOG ══════════════════════════════════════════════════════════ */
function addLog(text) {
  const log = $("net-log");
  const d = document.createElement("div");
  const t = new Date().toLocaleTimeString([], { hour12: false });
  d.innerHTML = `<span class="nl-t">[${t}]</span> ${escapeHtml(text)}`;
  log.appendChild(d);
  while (log.children.length > 80) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

/* ══ SPECTRUM ANALYZER ════════════════════════════════════════════════ */
function initSpectrum() {
  const cv = $("spectrum"), ctx = cv.getContext("2d");
  const BARS = 26;
  const heights = new Array(BARS).fill(0);
  const peaks = new Array(BARS).fill(0);
  function tokenRate() {
    const now = performance.now();
    S.tokTimes = S.tokTimes.filter((t) => now - t < 1200);
    return S.tokTimes.length;
  }
  function frame() {
    const w = cv.width, h = cv.height;
    ctx.fillStyle = "#030b0a";
    ctx.fillRect(0, 0, w, h);
    const rate = tokenRate();
    const energy = Math.min(1, rate / 25);
    const anim = !document.body.classList.contains("no-anim");
    const bw = w / BARS;
    for (let i = 0; i < BARS; i++) {
      const idle = anim ? 0.04 + 0.05 * Math.abs(Math.sin(performance.now() / 900 + i)) : 0.05;
      const centre = Math.exp(-Math.pow((i - BARS / 2) / (BARS / 3.2), 2));
      const target = Math.max(idle,
        energy * centre * (0.4 + Math.random() * 0.6));
      heights[i] += (target - heights[i]) * 0.3;
      const bh = heights[i] * (h - 14);
      peaks[i] = Math.max(peaks[i] - 0.006, heights[i]);
      // segmented LED column, green→yellow→red
      const segs = Math.round(bh / 5);
      for (let sIdx = 0; sIdx < segs; sIdx++) {
        const frac = sIdx * 5 / (h - 14);
        ctx.fillStyle = frac > 0.75 ? "#e04a2a" : frac > 0.5 ? "#e0b02a" : "#2ade5e";
        ctx.fillRect(i * bw + 2, h - 12 - sIdx * 5, bw - 4, 3);
      }
      ctx.fillStyle = "#7dffe4";
      ctx.fillRect(i * bw + 2, h - 12 - peaks[i] * (h - 14), bw - 4, 2);
    }
    ctx.fillStyle = "#155b4e";
    ctx.font = "9px monospace";
    ctx.fillText("GHOST LINE " + (rate ? "▮ RX " + rate + " tok/s" : "· idle"), 6, h - 3);
    requestAnimationFrame(frame);
  }
  frame();
  // live tps readout during streams
  setInterval(() => {
    if (Object.keys(S.stream).length)
      $("sb-tps").textContent = tokenRate() + " t/s";
  }, 500);
}

/* ══ NET RAIN BACKDROP ════════════════════════════════════════════════ */
function initNetRain() {
  const cv = $("net-bg"), ctx = cv.getContext("2d");
  const GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄ0123456789ABCDEF";
  let cols = [], fs = 14;
  function resize() {
    cv.width = innerWidth; cv.height = innerHeight;
    cols = new Array(Math.ceil(cv.width / fs)).fill(0)
      .map(() => Math.random() * -cv.height);
  }
  resize();
  addEventListener("resize", resize);
  setInterval(() => {
    if (document.body.classList.contains("no-anim")) return;
    ctx.fillStyle = "rgba(4, 22, 24, 0.13)";
    ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.font = fs + "px monospace";
    for (let i = 0; i < cols.length; i++) {
      const ch = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
      ctx.fillStyle = Math.random() < 0.02 ? "rgba(255,138,42,.7)" : "rgba(53,255,216,.32)";
      ctx.fillText(ch, i * fs, cols[i]);
      cols[i] = cols[i] > cv.height + Math.random() * 8000 ? 0 : cols[i] + fs;
    }
  }, 70);
}

/* ══ MASCOT ═══════════════════════════════════════════════════════════ */
const QUOTES = [
  "Natural oil! My favorite!",
  "The net is vast and infinite…",
  "Synchronization complete. We're all individuals — probably!",
  "I'll guard this chat log with my life!",
  "Curiosity levels exceeding safe parameters. Continuing anyway!",
  "Do you think an AI can have a ghost? Asking for a friend. The friend is me.",
  "Batou never lets me drive. YOU let me stream tokens though!",
  "Running diagnostics… everything is fun!",
];
function initMascot() {
  let t = null;
  $("mascot").addEventListener("click", () => {
    const b = $("mascot-bubble");
    b.textContent = QUOTES[Math.floor(Math.random() * QUOTES.length)];
    b.hidden = false;
    clearTimeout(t);
    t = setTimeout(() => { b.hidden = true; }, 4200);
  });
}

/* ══ BOOT SPLASH ══════════════════════════════════════════════════════ */
function bootSplash() {
  const lines = [
    "SECTION-9 SECURE TERMINAL BIOS v2.501",
    "COPYRIGHT (C) PUBLIC SECURITY SECTION 9",
    "",
    "MEMORY CHECK ......... 8196 KB OK",
    "CYBERBRAIN BARRIER ... ENGAGED",
    "GHOST LINE ........... SYNCED",
    "TACHIKOMA AI CORE .... LOADED",
    "STAND ALONE COMPLEX .. ONLINE",
    "",
    "BOOTING TACHI-COM v2.1 ...",
  ];
  const el = $("boot-text"), boot = $("boot");
  let i = 0;
  const step = () => {
    if (i < lines.length) {
      el.textContent += lines[i++] + "\n";
      setTimeout(step, i < 3 ? 120 : 90);
    } else setTimeout(dismiss, 420);
  };
  const dismiss = () => {
    boot.classList.add("gone");
    setTimeout(() => boot.remove(), 600);
  };
  boot.addEventListener("click", dismiss);
  step();
}

/* ══ CLOCK ════════════════════════════════════════════════════════════ */
function initClock() {
  const tick = () => {
    const d = new Date();
    $("tray-clock").firstChild.textContent =
      d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    $("tray-date").textContent =
      d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  };
  tick();
  setInterval(tick, 1000);
}

/* ══ WIRE-UP ══════════════════════════════════════════════════════════ */
function initChatControls() {
  $("send-btn").addEventListener("click", sendMessage);
  $("stop-btn").addEventListener("click", () => {
    if (S.chat) POST(`/api/chats/${S.chat.id}/stop`).catch(() => {});
  });
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $("chat-log").addEventListener("scroll", () => {
    const log = $("chat-log");
    S.pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  });
  $("tb-new").addEventListener("click", newSession);
  $("tb-sessions").addEventListener("click", () => openWin("win-sessions"));
  $("tb-settings").addEventListener("click", () => openWin("win-settings"));
  $("tb-regen").addEventListener("click", () => {
    if (S.chat) POST(`/api/chats/${S.chat.id}/regen`).catch((e) => addLog("regen: " + e.message));
  });
  $("tb-think").addEventListener("click", () => {
    const order = ["collapsed", "expanded", "hidden"];
    setThinkMode(order[(order.indexOf(thinkMode()) + 1) % order.length]);
  });
  // sessions window buttons
  $("sess-new").addEventListener("click", newSession);
  $("sess-rename").addEventListener("click", () => renameSession(LS.chatId));
  $("sess-copy").addEventListener("click", () => duplicateSession(LS.chatId));
  $("sess-del").addEventListener("click", () => deleteSession(LS.chatId));
  $("sess-export-json").addEventListener("click", () => exportChat("json"));
  $("sess-export-txt").addEventListener("click", () => exportChat("txt"));
  $("sess-clear").addEventListener("click", () => clearSession(LS.chatId));
  // desktop icons
  $$(".dicon").forEach((ic) => ic.addEventListener("dblclick", () => openWin(ic.dataset.win)));
  $$(".dicon").forEach((ic) => ic.addEventListener("click", () => {
    $$(".dicon").forEach((x) => x.style.background = "");
  }));
}

async function boot() {
  bootSplash();
  initWindows();
  initMenus();
  initSettings();
  initChatControls();
  initClock();
  initNetRain();
  initSpectrum();
  initMascot();
  try {
    S.presets = await GET("/api/presets");
  } catch (e) { S.presets = {}; }
  await fetchState(true);
  initSSE();
  ensureTaskBtn("win-chat");
  focusWin("win-chat");
  if (S.settings.ui.net_monitor) openWin("win-monitor");
  focusWin("win-chat");
  addLog("console attached · " + (S.status.online ? "uplink live" : "uplink down"));
}

document.addEventListener("DOMContentLoaded", boot);
