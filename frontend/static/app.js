// YTStep chat page. Talks to the ADK API server that frontend/server.py runs
// on the same address, so no extra setup (keys, CORS) is needed here.

const APP = "ytstep";          // the agent folder to run
const SESSION_KEY = "ytstep.sessionId";

// Each browser gets its own id, and the sidebar only shows that id's chats.
// The id is made of two halves:
//   - one kept in this site's storage (gone when cookies and site data are cleared)
//   - one kept in the browser's cache (gone when the cache is emptied)
// Clearing either one changes the id, so the chat history starts empty.
// (Old chats stay in ytstep/.adk/session.db on the server; this browser just
// can't reach them anymore.)
const STORAGE_KEY = "ytstep.storageKey";
let USER = null;

async function browserId() {
  let stored = localStorage.getItem(STORAGE_KEY);
  if (!stored) {
    stored = crypto.randomUUID().replaceAll("-", "");
    localStorage.setItem(STORAGE_KEY, stored);
  }
  let cached = "nocache";
  try {
    const res = await fetch("/api/cache-key", { cache: "force-cache" });
    cached = (await res.json()).key;
  } catch (err) {
    console.error(err);
  }
  return `browser-${stored.slice(0, 16)}-${cached.slice(0, 16)}`;
}

// Only these agents talk to the person. The others work behind the scenes
// and show up as progress steps instead of chat messages.
const SPEAKERS = new Set(["orchestrator", "query_eval_agent"]);

// One chapter of the progress bar per step of the pipeline.
const STEPS = {
  query_eval_agent: { n: 0, label: "Understanding what you're after" },
  search_agent: { n: 1, label: "Searching YouTube", again: "Looking for better matches" },
  video_summarizer_agent: { n: 2, label: "Watching the videos and reading comments" },
  relevance_check_agent: { n: 3, label: "Checking they fit what you asked" },
};

const $ = (id) => document.getElementById(id);
const log = $("log"), scroller = $("scroller"), empty = $("empty");
const input = $("input"), send = $("send"), form = $("composer");

let sessionId = null;
let busy = false;

// ---------- Talking to the server ----------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  // The passcode cookie expired or was cleared: show the passcode screen again.
  // (Only once in a row, so a cookie that won't stick can't cause endless reloads.)
  if (res.status === 401 && !sessionStorage.getItem("ytstep.relocked")) {
    sessionStorage.setItem("ytstep.relocked", "1");
    location.reload();
  }
  if (res.ok) sessionStorage.removeItem("ytstep.relocked");
  if (!res.ok) throw new Error(`Server said ${res.status}`);
  return res;
}

// ---------- Passcode screen ----------
// Only shows when the server has a passcode set (APP_PASSCODE). The server
// remembers a correct code with a cookie, so it's asked for once per browser.

async function unlockIfNeeded() {
  const { locked } = await (await fetch("/api/unlock")).json();
  if (!locked) return;
  document.body.classList.add("locked");
  const lock = $("lock"), lockInput = $("lock-input"), lockError = $("lock-error");
  empty.hidden = true;
  lock.hidden = false;
  lockInput.focus();
  await new Promise((resolve) => {
    $("lock-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const res = await fetch("/api/unlock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: lockInput.value }),
      });
      if (res.ok) return resolve();
      lockError.hidden = false;
      lockInput.select();
    });
  });
  lock.hidden = true;
  empty.hidden = false;
  document.body.classList.remove("locked");
}

// Chat names live in ADK's session metadata, the same field `adk web` reads,
// so a chat named here shows up with that name in `adk web` too.
const NO_TOPIC = "New Search";
const titleOf = (session) => session.state?.__session_metadata__?.displayName;
const titleState = (title) => ({ __session_metadata__: { displayName: title } });

// Asks the server (which asks Gemini) for a short name from the chat's topic.
// Gets the first message, or the first few if the first is just "hello".
async function nameFor(messages) {
  const text = [].concat(messages).slice(0, 3).join("\n");
  try {
    const res = await api("/api/chat-title", { method: "POST", body: JSON.stringify({ text }) });
    return (await res.json()).title;
  } catch {
    return text.split(/\s+/).slice(0, 6).join(" ");
  }
}

// The name is set when the chat is created, not added afterwards: ADK rejects
// changes to a chat while the agents are still working in it.
async function newSession(title) {
  const res = await api(`/apps/${APP}/users/${USER}/sessions`, {
    method: "POST",
    body: JSON.stringify({ state: titleState(title) }),
  });
  sessionId = (await res.json()).id;
  localStorage.setItem(SESSION_KEY, sessionId);
}

const getSession = async (id) => (await api(`/apps/${APP}/users/${USER}/sessions/${id}`)).json();

async function openChat(id) {
  if (busy) return;
  const session = await getSession(id);
  sessionId = id;
  localStorage.setItem(SESSION_KEY, id);
  log.replaceChildren();
  empty.hidden = false;
  for (const event of session.events || []) {
    const text = textOf(event);
    if (!text) continue;
    if (event.author === "user") addUser(text);
    else if (SPEAKERS.has(event.author)) addBot(text);
  }
  markActive();
  closeMenu();
}

// Re-open the last chat after a page refresh, so a finished search isn't lost.
async function restoreSession() {
  const saved = localStorage.getItem(SESSION_KEY);
  if (!saved) return;
  try {
    await openChat(saved);
  } catch {
    localStorage.removeItem(SESSION_KEY);
    sessionId = null;
  }
}

// ---------- Sidebar: the person's chats ----------

const chatList = $("chat-list");
let chats = []; // { id, title, updated }

async function loadChats() {
  const sessions = await (await api(`/apps/${APP}/users/${USER}/sessions`)).json();
  chats = sessions.map((s) => ({ id: s.id, title: titleOf(s) || NO_TOPIC, updated: s.lastUpdateTime }));
  renderChats();
}

const userMessages = (session) =>
  (session.events || []).filter((e) => e.author === "user").map(textOf).filter(Boolean);

// Saves a name into the chat itself. Only used right after a search finishes,
// when moving the chat to "last used: now" is correct anyway.
async function saveTitle(chat, title) {
  chat.title = title;
  await api(`/apps/${APP}/users/${USER}/sessions/${chat.id}`, {
    method: "PATCH",
    body: JSON.stringify({ stateDelta: titleState(title) }),
  });
}

// A chat that opened with "hello" has no topic yet. Once a search has run,
// try again with the messages so far.
async function renameIfNoTopic(id) {
  const chat = chats.find((c) => c.id === id);
  if (!chat || chat.title !== NO_TOPIC) return;
  const title = await nameFor(userMessages(await getSession(id)));
  if (title !== NO_TOPIC) { await saveTitle(chat, title); renderChats(); }
}

function groupName(seconds) {
  const day = 86400000;
  const startOfToday = new Date().setHours(0, 0, 0, 0);
  const t = seconds * 1000;
  if (t >= startOfToday) return "Today";
  if (t >= startOfToday - day) return "Yesterday";
  if (t >= startOfToday - 6 * day) return "Previous 7 days";
  return "Older";
}

function renderChats() {
  const visible = [...chats].sort((a, b) => b.updated - a.updated);
  chatList.replaceChildren();
  if (!visible.length) {
    chatList.innerHTML = `<p class="chat-list-empty">Your chats will show up here.</p>`;
    return;
  }
  let group = null;
  for (const chat of visible) {
    const name = groupName(chat.updated);
    if (name !== group) {
      group = name;
      const h = document.createElement("h2");
      h.className = "chat-group";
      h.textContent = name;
      chatList.append(h);
    }
    const item = document.createElement("button");
    item.type = "button";
    item.className = "chat-item";
    item.dataset.id = chat.id;
    item.textContent = chat.title || "Naming…";
    item.title = chat.title || "";
    item.onclick = () => openChat(chat.id).catch(console.error);
    chatList.append(item);
  }
  markActive();
}

function markActive() {
  for (const item of chatList.querySelectorAll(".chat-item")) {
    item.setAttribute("aria-current", String(item.dataset.id === sessionId));
    item.disabled = busy && item.dataset.id !== sessionId;
  }
}

function touchChat(id, title) {
  const chat = chats.find((c) => c.id === id);
  if (chat) chat.updated = Date.now() / 1000;
  else chats.push({ id, title, updated: Date.now() / 1000 });
  renderChats();
}

const menuBtn = $("menu"), scrim = $("scrim");
function closeMenu() {
  document.body.classList.remove("menu-open");
  scrim.hidden = true;
  menuBtn.setAttribute("aria-expanded", "false");
}
menuBtn.addEventListener("click", () => {
  document.body.classList.add("menu-open");
  scrim.hidden = false;
  menuBtn.setAttribute("aria-expanded", "true");
});
scrim.addEventListener("click", closeMenu);

function textOf(event) {
  const parts = event.content?.parts || [];
  return parts.filter((p) => p.text && !p.thought).map((p) => p.text).join("").trim();
}

// Streams the pipeline's events (Server-Sent Events from ADK's /run_sse).
async function* runAgent(text) {
  const res = await api("/run_sse", {
    method: "POST",
    body: JSON.stringify({
      appName: APP,
      userId: USER,
      sessionId,
      newMessage: { role: "user", parts: [{ text }] },
      streaming: false,
    }),
  });
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("");
      if (data.trim()) yield JSON.parse(data);
    }
  }
}

// ---------- Sending a message ----------

async function submit(text) {
  text = text.trim();
  if (!text || busy || !USER) return;
  setBusy(true);
  addUser(text);
  input.value = "";
  autosize();

  const progress = addProgress();
  let replied = false;
  try {
    let title;
    if (!sessionId) {
      title = await nameFor(text);
      await newSession(title);
    }
    touchChat(sessionId, title);
    for await (const event of runAgent(text)) {
      const problem = event.error_message || event.errorMessage;
      if (problem) throw new Error(problem);
      if (STEPS[event.author]) progress.step(event.author);
      const reply = textOf(event);
      if (reply && SPEAKERS.has(event.author) && !event.partial) {
        addBot(reply, progress.el);
        replied = true;
      }
    }
    if (!replied) addError("I didn't get an answer back that time.", text);
  } catch (err) {
    console.error(err);
    const offline = err instanceof TypeError;
    addError(
      offline
        ? "I couldn't reach YTStep. Is the server still running?"
        : "Something went wrong on my end while searching. Want to try again?",
      text,
    );
  } finally {
    progress.el.remove();
    setBusy(false);
    if (sessionId) {
      touchChat(sessionId);
      renameIfNoTopic(sessionId).catch(console.error);
    }
    input.focus();
  }
}

function setBusy(value) {
  busy = value;
  markActive();
  $("new-chat").disabled = value;
  send.disabled = value || !input.value.trim();
  input.placeholder = value ? "Working on it…" : "Ask for a video…";
}

// ---------- Drawing messages ----------

function addUser(text) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.textContent = text;
  place(el);
}

function botShell(extraClass = "") {
  const el = document.createElement("div");
  el.className = `msg bot ${extraClass}`;
  el.innerHTML = `<img class="mark" src="logo.svg" alt=""><div class="body"></div>`;
  return el;
}

function addBot(markdown, before = null) {
  const el = botShell();
  el.querySelector(".body").innerHTML = renderReply(markdown);
  place(el, before);
}

function addError(message, retryText) {
  const el = botShell("error");
  const body = el.querySelector(".body");
  body.textContent = message;
  const btn = document.createElement("button");
  btn.className = "retry";
  btn.type = "button";
  btn.textContent = "Try again";
  btn.onclick = () => { el.remove(); log.lastElementChild?.classList.contains("user") && log.lastElementChild.remove(); submit(retryText); };
  body.append(document.createElement("br"), btn);
  place(el);
}

function addProgress() {
  const el = botShell("working");
  const body = el.querySelector(".body");
  body.innerHTML = `
    <p class="status"><b>Thinking…</b></p>
    <div class="chapters" role="progressbar" aria-valuemin="0" aria-valuemax="4" aria-valuenow="0" aria-valuetext="Thinking">
      ${'<div class="chapter"><div class="fill"></div></div>'.repeat(4)}
    </div>`;
  const status = body.querySelector(".status b");
  const bar = body.querySelector(".chapters");
  const chapters = [...bar.children];
  const seen = new Set();
  let current = null;
  place(el);

  return {
    el,
    step(author) {
      if (author === current) return;
      const { n, label, again } = STEPS[author];
      const text = seen.has(author) && again ? again : label;
      seen.add(author);
      current = author;
      chapters.forEach((c, i) => {
        c.classList.toggle("done", i < n);
        c.classList.remove("active");
        if (i === n) { void c.offsetWidth; c.classList.add("active"); } // restart the crawl
      });
      status.textContent = `${text}…`;
      bar.setAttribute("aria-valuenow", n + 1);
      bar.setAttribute("aria-valuetext", text);
      stickToBottom();
    },
  };
}

function place(el, before = null) {
  empty.hidden = true;
  log.insertBefore(el, before);
  stickToBottom();
}

function stickToBottom() {
  requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
}

// ---------- Turning the agent's markdown into safe HTML ----------
// Everything is HTML-escaped first, so nothing the model writes can inject
// markup. Only headings, lists, bold/italic, code and http(s) links come back.

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const VIDEO_ID = /(?:youtube\.com\/watch\?(?:[^\s)]*&)?v=|youtu\.be\/)([\w-]{11})/;
const TIMESTAMP = /(^|[\s(\[])((?:\d{1,2}:)?\d{1,2}:\d{2})(?=$|[\s)\],.;:!?–-])/g;

function toSeconds(ts) {
  return ts.split(":").reduce((total, part) => total * 60 + Number(part), 0);
}

function inline(text, videoId) {
  const saved = [];
  const keep = (html) => `\u0000${saved.push(html) - 1}\u0000`;
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, (_, code) => keep(`<code>${code}</code>`));
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) =>
    keep(`<a href="${url}" target="_blank" rel="noopener">${label}</a>`));
  out = out.replace(/(^|\s)(https?:\/\/[^\s<]+[^\s<.,)])/g, (_, pre, url) =>
    pre + keep(`<a href="${url}" target="_blank" rel="noopener">${url}</a>`));
  if (videoId && !/duration|length|long/i.test(text)) {
    out = out.replace(TIMESTAMP, (_, pre, ts) => pre + keep(
      `<a class="ts" href="https://www.youtube.com/watch?v=${videoId}&amp;t=${toSeconds(ts)}s" target="_blank" rel="noopener" title="Jump to ${ts}">${ts}</a>`));
  }
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*(?!\s)(.+?)\*(?!\w)/g, "$1<em>$2</em>");
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => saved[i]);
}

function renderBlocks(lines, videoId) {
  let html = "", list = null, para = [];
  const flushPara = () => { if (para.length) html += `<p>${inline(para.join(" "), videoId)}</p>`; para = []; };
  const closeList = () => { if (list) html += `</${list}>`; list = null; };

  for (const raw of lines) {
    const line = raw.trim();
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = line.match(/^[*-]\s+(.*)$/);
    const numbered = line.match(/^\d+[.)]\s+(.*)$/);
    if (!line) { flushPara(); closeList(); continue; }
    if (heading) { flushPara(); closeList(); html += `<h3>${inline(heading[1], videoId)}</h3>`; continue; }
    if (bullet || numbered) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      // Keep the model's own numbers, even when bullets sit between numbered items.
      const start = numbered ? ` start="${parseInt(line, 10)}"` : "";
      if (list !== kind) { closeList(); html += `<${kind}${start}>`; list = kind; }
      html += `<li>${inline((bullet || numbered)[1], videoId)}</li>`;
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara();
  closeList();
  return html;
}

// Splits the reply into one section per video. The model lists videos either
// as headings ("### Title") or as top-level numbered items ("1. [Title](url)"),
// so a new section starts at either. Each section that links to a YouTube
// video becomes a card with its own thumbnail, and its timestamps jump to that
// moment in that video.
const SECTION_START = /^(#{1,6}\s|\d+[.)]\s)/;

function splitSections(markdown) {
  const sections = [[]];
  let prevBlank = false;
  for (const line of markdown.split("\n")) {
    const current = sections[sections.length - 1];
    const topLevel = line === line.trimStart() && line.trim() !== "";
    const inVideo = VIDEO_ID.test(current.join("\n"));
    const startsSection = topLevel && SECTION_START.test(line);
    // Plain text after a video's details ("Let me know if…") shouldn't sit inside that video's card.
    const endsVideo = topLevel && inVideo && prevBlank && !/^[*-]\s/.test(line);
    if ((startsSection || endsVideo) && current.some((l) => l.trim())) sections.push([]);
    sections[sections.length - 1].push(line);
    prevBlank = line.trim() === "";
  }
  return sections;
}

function renderReply(markdown) {
  return splitSections(markdown).map((lines) => {
    const id = lines.join("\n").match(VIDEO_ID)?.[1];
    if (!id) return renderBlocks(lines);
    // The card itself separates the videos, so "1. Title" becomes the card's title.
    const first = lines.findIndex((l) => l.trim());
    lines = [...lines];
    lines[first] = lines[first].replace(/^\s*\d+[.)]\s+/, "### ");
    const url = `https://www.youtube.com/watch?v=${id}`;
    return `<article class="video">
      <a class="thumb" href="${url}" target="_blank" rel="noopener" aria-label="Watch on YouTube">
        <img src="https://i.ytimg.com/vi/${id}/hqdefault.jpg" alt="" loading="lazy">
      </a>
      <div>${renderBlocks(lines, id)}</div>
    </article>`;
  }).join("");
}

// ---------- Composer ----------

function autosize() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
  send.disabled = busy || !input.value.trim();
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submit(input.value);
  }
});
form.addEventListener("submit", (e) => { e.preventDefault(); submit(input.value); });

document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => submit(chip.textContent)));

$("new-chat").addEventListener("click", () => {
  if (busy) return;
  localStorage.removeItem(SESSION_KEY);
  sessionId = null;
  log.replaceChildren();
  empty.hidden = false;
  markActive();
  closeMenu();
  input.focus();
});

unlockIfNeeded()
  .then(browserId)
  .then((id) => {
    USER = id;
    document.documentElement.dataset.browser = id; // handy when debugging in dev tools
    return restoreSession();
  })
  .finally(() => {
    loadChats().catch(console.error);
    input.focus();
  });
