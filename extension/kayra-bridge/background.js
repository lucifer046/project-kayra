// ┌────────────────────────────────────────────────────────────────────────┐
// │                    Kayra Bridge — background worker                    │
// └────────────────────────────────────────────────────────────────────────┘
// Connects to the Kayra assistant on THIS computer (ws://127.0.0.1:<port>/kayra) and runs the
// commands it sends: tabs, clicks, typing, reading pages, history, bookmarks, Google Meet.
//
// Security: Kayra accepts the connection only from this extension's fixed ID (its Origin), and
// this worker only ever connects to 127.0.0.1. Nothing is sent anywhere else. Passwords are
// never typed: Kayra refuses password fields on its side and this file refuses them too.

const PORTS = [17893];
const KEEPALIVE_MS = 20000;   // an MV3 worker stays alive while a WebSocket exchanges messages
let socket = null;
let keepalive = null;

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const port = PORTS[0];
  try {
    socket = new WebSocket(`ws://127.0.0.1:${port}/kayra`);
  } catch (e) {
    return;
  }
  socket.onopen = () => {
    send({type: "hello", version: chrome.runtime.getManifest().version});
    clearInterval(keepalive);
    keepalive = setInterval(() => send({type: "ping"}), KEEPALIVE_MS);
  };
  socket.onmessage = async (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    if (!msg || !msg.id || !msg.cmd) return;
    try {
      const result = await run(msg.cmd, msg.args || {});
      send({id: msg.id, ok: true, result});
    } catch (e) {
      send({id: msg.id, ok: false, error: String(e && e.message || e)});
    }
  };
  socket.onclose = () => { clearInterval(keepalive); socket = null; };
  socket.onerror = () => {};
}

function send(obj) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("kayra-reconnect", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "kayra-reconnect") connect(); });
chrome.action.onClicked.addListener(connect);
connect();

// ── helpers ──────────────────────────────────────────────────────────────

async function activeTab() {
  const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  if (!tab) throw new Error("no active tab");
  return tab;
}

function tabInfo(t) {
  return {id: t.id, title: t.title, url: t.url, active: t.active, windowId: t.windowId,
          audible: !!t.audible, muted: !!(t.mutedInfo && t.mutedInfo.muted), pinned: t.pinned,
          groupId: t.groupId === undefined ? -1 : t.groupId, index: t.index};
}

function matches(tab, needle) {
  const n = String(needle || "").toLowerCase().trim();
  if (!n) return false;
  return (tab.title || "").toLowerCase().includes(n) || (tab.url || "").toLowerCase().includes(n);
}

async function findTabs(needle) {
  const tabs = await chrome.tabs.query({});
  return tabs.filter((t) => matches(t, needle));
}

async function targetTab(args) {
  if (args.tabId) return chrome.tabs.get(args.tabId);
  if (args.match) {
    const found = await findTabs(args.match);
    if (!found.length) throw new Error(`no tab matches ${args.match}`);
    return found.find((t) => t.active) || found[0];
  }
  return activeTab();
}

async function inPage(tab, func, args, world) {
  const [res] = await chrome.scripting.executeScript({
    target: {tabId: tab.id}, func, args: args || [], world: world || "ISOLATED"});
  return res ? res.result : null;
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); resolve(true); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(false); }, timeoutMs || 15000);
  });
}

// 1.8.0: run a page function until its answer is "ready", INSIDE the extension: one Kayra
// round trip instead of one per 250 ms poll. The page may still be loading or navigating, so
// a failed injection is retried too. Returns the last answer when time runs out.
async function untilReady(args, func, fargs, ready) {
  const deadline = Date.now() + Math.min(Math.max(args.waitMs || 0, 0), 15000);
  let last = null;
  while (true) {
    try { last = await inPage(await targetTab(args), func, fargs); } catch (e) { last = null; }
    if (ready(last) || Date.now() >= deadline) return last;
    await new Promise((r) => setTimeout(r, 120));
  }
}

// ── commands ─────────────────────────────────────────────────────────────

async function run(cmd, args) {
  switch (cmd) {
    case "ping": return {version: chrome.runtime.getManifest().version};

    case "tabs.list": return (await chrome.tabs.query({})).map(tabInfo);
    case "tabs.active": return tabInfo(await activeTab());
    case "tabs.activate": {
      const tab = await targetTab(args);
      await chrome.tabs.update(tab.id, {active: true});
      await chrome.windows.update(tab.windowId, {focused: true});
      return tabInfo(tab);
    }
    case "tabs.close": {
      let tabs;
      if (Array.isArray(args.tabIds)) {
        // 1.2.2: close exactly these tabs (by position, duplicates, to the right / left).
        tabs = (await chrome.tabs.query({})).filter((t) => args.tabIds.includes(t.id));
      } else if (args.exceptActive) {
        const keep = await activeTab();
        tabs = (await chrome.tabs.query({windowId: keep.windowId})).filter((t) => t.id !== keep.id && !t.pinned);
      } else if (args.match) {
        tabs = await findTabs(args.match);
        if (!args.all && tabs.length > 1) {
          return {closed: 0, ambiguous: tabs.slice(0, 5).map(tabInfo)};
        }
      } else {
        tabs = [await activeTab()];
      }
      await chrome.tabs.remove(tabs.map((t) => t.id));
      return {closed: tabs.length, titles: tabs.map((t) => t.title).slice(0, 5)};
    }
    case "tabs.open": {
      // where: "tab" (default) | "window" | "incognito" | "next" (right of the active tab).
      const where = args.where || "tab";
      let tab = null;
      if (where === "window" || where === "incognito") {
        const win = await chrome.windows.create({url: args.url, focused: true, incognito: where === "incognito"});
        tab = win.tabs && win.tabs[0];
        if (!tab) return {id: null, windowId: win.id, url: args.url, incognito: where === "incognito"};
      } else {
        const opts = {url: args.url, active: args.active !== false};
        if (where === "next") {
          const cur = await activeTab();
          Object.assign(opts, {index: cur.index + 1, windowId: cur.windowId, openerTabId: cur.id});
        } else {
          // Never into a web-app (--app) window: a tab belongs in a normal browser window.
          try {
            const normal = await chrome.windows.getLastFocused({windowTypes: ["normal"]});
            if (normal && normal.id !== undefined) opts.windowId = normal.id;
          } catch (e) { /* no normal window: Chrome makes one */ }
        }
        tab = await chrome.tabs.create(opts);
        if (args.active !== false) await chrome.windows.update(tab.windowId, {focused: true});
      }
      if (args.wait) await waitForLoad(tab.id, args.timeoutMs);
      return tabInfo(await chrome.tabs.get(tab.id));
    }
    case "tabs.navigate": {
      const tab = await targetTab(args);
      await chrome.tabs.update(tab.id, {url: args.url, active: true});
      if (args.wait) await waitForLoad(tab.id, args.timeoutMs);
      return tabInfo(await chrome.tabs.get(tab.id));
    }
    case "tabs.reload": { const t = await targetTab(args); await chrome.tabs.reload(t.id, {bypassCache: !!args.hard}); return tabInfo(t); }
    case "tabs.back": { const t = await targetTab(args); await chrome.tabs.goBack(t.id); return tabInfo(t); }
    case "tabs.forward": { const t = await targetTab(args); await chrome.tabs.goForward(t.id); return tabInfo(t); }
    case "tabs.duplicate": { const t = await targetTab(args); return tabInfo(await chrome.tabs.duplicate(t.id)); }
    case "tabs.pin": { const t = await targetTab(args); await chrome.tabs.update(t.id, {pinned: !t.pinned}); return {pinned: !t.pinned}; }
    case "tabs.mute": {
      const t = await targetTab(args);
      const muted = args.on === undefined ? !(t.mutedInfo && t.mutedInfo.muted) : !!args.on;
      await chrome.tabs.update(t.id, {muted});
      return {muted};
    }
    case "tabs.restore": {
      // 1.3.0: a specific closed tab or window by its session id ("reopen the YouTube tab I closed").
      const s = args.sessionId ? await chrome.sessions.restore(args.sessionId) : await chrome.sessions.restore();
      return {restored: !!s, title: s && s.tab ? s.tab.title : (s && s.window ? "a window" : "")};
    }
    // ── 1.3.0 ──────────────────────────────────────────────────────────
    case "tabs.recentlyClosed": {
      const items = await chrome.sessions.getRecentlyClosed({maxResults: Math.min(args.max || 10, 25)});
      return items.map((it) => it.tab
        ? {sessionId: it.tab.sessionId, kind: "tab", title: it.tab.title, url: it.tab.url}
        : {sessionId: it.window.sessionId, kind: "window", title: `a window with ${it.window.tabs.length} tabs`,
           url: ""});
    }
    case "tabs.muteOthers": {
      const keep = await activeTab();
      const others = (await chrome.tabs.query({})).filter((t) => t.id !== keep.id);
      let changed = 0;
      for (const t of others) {
        const muted = !!(t.mutedInfo && t.mutedInfo.muted);
        if (muted !== !!args.on) { await chrome.tabs.update(t.id, {muted: !!args.on}); changed += 1; }
      }
      return {changed};
    }
    case "tabs.discard": {
      // Background tabs only: never the active tab of any window, never one playing sound.
      const tabs = (await chrome.tabs.query({active: false, audible: false, discarded: false}))
        .filter((t) => !t.pinned || args.includePinned);
      let discarded = 0;
      for (const t of tabs) { try { await chrome.tabs.discard(t.id); discarded += 1; } catch (e) { /* busy tab */ } }
      return {discarded};
    }
    case "tabs.zoom": {
      const t = await targetTab(args);
      if (args.factor) await chrome.tabs.setZoom(t.id, Math.max(0.25, Math.min(5, args.factor)));
      return {factor: await chrome.tabs.getZoom(t.id)};
    }

    // ── 1.5.0: tab groups (B4), windows (B5), result lists (C3), find with highlight (C4) ──
    case "groups.list": {
      const groups = await chrome.tabGroups.query({});
      const tabs = await chrome.tabs.query({});
      return groups.map((g) => ({id: g.id, title: g.title || "", color: g.color, collapsed: g.collapsed,
                                 windowId: g.windowId, tabs: tabs.filter((t) => t.groupId === g.id).length}));
    }
    case "groups.create": {
      // tabIds -> a NEW group (or `groupId` to add them to an existing one), then title/colour.
      const opts = {tabIds: args.tabIds};
      if (args.groupId !== undefined && args.groupId !== null) opts.groupId = args.groupId;
      const groupId = await chrome.tabs.group(opts);
      const props = {};
      if (args.title !== undefined) props.title = String(args.title);
      if (args.color) props.color = args.color;
      if (args.collapsed !== undefined) props.collapsed = !!args.collapsed;
      if (Object.keys(props).length) await chrome.tabGroups.update(groupId, props);
      return {groupId, tabs: args.tabIds.length};
    }
    case "groups.update": {
      const props = {};
      if (args.title !== undefined) props.title = String(args.title);
      if (args.color) props.color = args.color;
      if (args.collapsed !== undefined) props.collapsed = !!args.collapsed;
      const g = await chrome.tabGroups.update(args.groupId, props);
      return {id: g.id, title: g.title, color: g.color, collapsed: g.collapsed};
    }
    case "groups.ungroup": {
      let ids = args.tabIds;
      if (!ids) ids = (await chrome.tabs.query({})).filter((t) => t.groupId === args.groupId).map((t) => t.id);
      if (ids.length) await chrome.tabs.ungroup(ids);
      return {ungrouped: ids.length};
    }
    case "windows.detach": {
      // "move this tab to a new window": the tab leaves its window and gets one of its own.
      const t = await targetTab(args);
      const win = await chrome.windows.create({tabId: t.id, focused: true});
      return {windowId: win.id, title: t.title};
    }
    case "windows.merge": {
      // "merge my Chrome windows": every tab of every other NORMAL window into the focused one.
      const into = await chrome.windows.getLastFocused({windowTypes: ["normal"]});
      const all = await chrome.windows.getAll({populate: true, windowTypes: ["normal"]});
      let moved = 0, windows = 0;
      for (const w of all) {
        if (w.id === into.id || w.incognito !== into.incognito) continue;
        const ids = w.tabs.map((t) => t.id);
        if (!ids.length) continue;
        await chrome.tabs.move(ids, {windowId: into.id, index: -1});
        moved += ids.length; windows += 1;
      }
      return {moved, windows};
    }
    case "windows.count": {
      const all = await chrome.windows.getAll({windowTypes: ["normal"]});
      return {count: all.length};
    }
    case "page.results": return untilReady(args, pageResults, [args.nth || 0, args.kind || ""], (r) => r && r.count);
    case "page.highlight": return inPage(await targetTab(args), pageHighlight, [args.text || "", args.step || 0, !!args.clear]);

    // ── 1.6.0: comments/reviews (C6), transcript (C6), selection (C8), bookmarks (C9),
    //    history (C10), downloads (C11) ──
    case "page.comments": return untilReady(args, pageComments, [args.max || 12], (r) => r && r.comments && r.comments.length);
    case "page.transcript": {
      if (args.waitMs) {                       // 1.8.0: open the panel, then wait for its text
        const first = await inPage(await targetTab(args), pageTranscript, [true]);
        if (!first || !first.supported || first.text) return first;
        return untilReady(args, pageTranscript, [false], (r) => r && r.text);
      }
      return inPage(await targetTab(args), pageTranscript, [!!args.open]);
    }
    case "page.selection": return inPage(await targetTab(args), () => String(window.getSelection() || "").slice(0, 6000), []);
    case "bookmarks.add": {
      const t = await targetTab(args);
      let parentId;
      if (args.folder) {
        const want = String(args.folder).toLowerCase();
        const folders = [];
        const walk = (nodes) => nodes.forEach((n) => {
          if (!n.url && (n.title || "").toLowerCase() === want) folders.push(n);
          if (n.children) walk(n.children);
        });
        walk(await chrome.bookmarks.getTree());
        if (folders.length) parentId = folders[0].id;
        else parentId = (await chrome.bookmarks.create({title: args.folder})).id;
      }
      const existing = (await chrome.bookmarks.search({url: t.url})) || [];
      if (existing.length && !args.folder) return {added: false, already: true, title: existing[0].title};
      const b = await chrome.bookmarks.create(Object.assign({title: t.title, url: t.url}, parentId ? {parentId} : {}));
      return {added: true, title: b.title, folder: args.folder || ""};
    }
    case "bookmarks.remove": {
      // Exact ids only: Kayra asks the user first and passes what it listed.
      for (const id of args.ids || []) await chrome.bookmarks.remove(String(id));
      return {removed: (args.ids || []).length};
    }
    case "bookmarks.find": {
      const items = await chrome.bookmarks.search(args.text || "");
      return items.filter((b) => b.url).slice(0, args.max || 10).map((b) => ({id: b.id, title: b.title, url: b.url}));
    }
    case "history.deleteRange": {
      await chrome.history.deleteRange({startTime: args.startTime, endTime: args.endTime || Date.now()});
      return {deleted: true};
    }
    case "downloads.list": {
      const items = await chrome.downloads.search({orderBy: ["-startTime"], limit: Math.min(args.max || 5, 20)});
      return items.map((d) => ({id: d.id, filename: d.filename, url: d.finalUrl || d.url, state: d.state,
                               paused: d.paused, exists: d.exists, bytes: d.bytesReceived, total: d.totalBytes,
                               mime: d.mime, danger: d.danger}));
    }
    case "downloads.show": { chrome.downloads.show(args.id); return {shown: true}; }
    case "downloads.pause": { await chrome.downloads.pause(args.id); return {paused: true}; }
    case "downloads.resume": { await chrome.downloads.resume(args.id); return {resumed: true}; }
    case "downloads.cancel": { await chrome.downloads.cancel(args.id); return {cancelled: true}; }

    // ── 1.7.0: Google Workspace (Part 4) ──
    case "page.docList": return untilReady(args, pageDocList, [args.max || 40], (r) => r && r.length);
    case "page.fetchText": {
      // A Sheet's CSV or a Doc's text, fetched INSIDE the Google page with the user's own
      // session. Only docs.google.com addresses: this is not a general fetch.
      if (!/^https:\/\/docs\.google\.com\//.test(String(args.url || ""))) throw new Error("only docs.google.com");
      return inPage(await targetTab(args), pageFetchText, [args.url, args.max || 2000000]);
    }
    case "page.focus": return inPage(await targetTab(args), (sel) => {
      const el = document.querySelector(sel);
      if (!el) return {focused: false};
      el.focus(); if (el.select) el.select();
      return {focused: true};
    }, [args.selector]);
    case "calendar.next": return untilReady(args, pageCalendarNext, [!!args.open], (r) => r && (args.open ? r.meet : r.events));

    // ── 1.9.0: WhatsApp Web (Part 5, H0.4) — one site function each, read-only except open ──
    case "chat.list": return untilReady(args, pageChatList, [args.max || 60], (r) => r && r.ready);
    case "chat.open": return inPage(await targetTab(args), pageChatOpen, [args.name || ""]);
    case "chat.messages": return untilReady(args, pageChatMessages, [args.limit || 30], (r) => r && r.chat);
    case "chat.composer": return inPage(await targetTab(args), pageChatComposer, []);

    case "page.text": return inPage(await targetTab(args), pageText, [args.max || 15000]);
    case "page.click": return inPage(await targetTab(args), pageClick, [args.text, args.nth || 1]);
    case "gmail.open": return inPage(await targetTab(args), gmailOpen, [args.which || "first", args.nth || 1, args.query || ""]);
    case "page.waitFor": {
      // Readiness instead of fixed sleeps: every condition given must hold.
      const tab = await targetTab(args);
      const deadline = Date.now() + (args.timeoutMs || 8000);
      while (Date.now() < deadline) {
        const t = await chrome.tabs.get(tab.id);
        let ok = true;
        if (args.urlIncludes && !(t.url || "").includes(args.urlIncludes)) ok = false;
        if (ok && args.titleIncludes && !(t.title || "").includes(args.titleIncludes)) ok = false;
        if (ok && (args.text || args.selector)) {
          // The page waits on ITS OWN changes (a MutationObserver) and answers the moment the
          // condition holds, instead of being re-injected and re-read every 250 ms.
          const left = Math.min(2000, Math.max(100, deadline - Date.now()));
          try { ok = !!(await inPage(t, pageWaitFor, [args.text || "", args.selector || "", left])); }
          catch (e) { ok = false; }      // the page is still navigating
        }
        if (ok) return {ok: true, title: t.title, url: t.url};
        await new Promise((r) => setTimeout(r, 120));
      }
      return {ok: false, timedOut: true};
    }
    case "runtime.reload": setTimeout(() => chrome.runtime.reload(), 200); return {reloading: true};
    case "page.type": return inPage(await targetTab(args), pageType, [args.field || "", args.text || "", !!args.submit, !!args.clear]);
    case "page.scroll": return inPage(await targetTab(args), pageScroll, [args.direction || "down", args.until || ""]);
    case "page.find": return inPage(await targetTab(args), pageFind, [args.text]);
    case "page.controls": return inPage(await targetTab(args), pageControls, [args.limit || 60]);
    case "page.setMain": return inPage(await targetTab(args), (k, v) => { window[k] = v; return true; }, [args.key, args.value], "MAIN");

    case "history.search": {
      const since = Date.now() - (args.days || 7) * 86400000;
      const items = await chrome.history.search({text: args.text || "", startTime: since, maxResults: args.max || 10});
      return items.map((h) => ({title: h.title, url: h.url, lastVisit: h.lastVisitTime}));
    }
    case "bookmarks.search": {
      const items = await chrome.bookmarks.search(args.text || "");
      return items.filter((b) => b.url).slice(0, args.max || 10).map((b) => ({title: b.title, url: b.url}));
    }
  }
  throw new Error(`unknown command ${cmd}`);
}

// ── page functions (serialised into the page; must be self-contained) ───

function pageHas(text, selector) {
  if (selector && !document.querySelector(selector)) return false;
  if (text && !(document.body && (document.body.innerText || "").toLowerCase().includes(String(text).toLowerCase()))) return false;
  return true;
}

function pageWaitFor(text, selector, timeoutMs) {
  // Resolves true as soon as `selector` matches and `text` is visible, false at the timeout.
  // The selector is tested first (cheap); the visible-text read forces layout, so it runs at
  // most once per 150 ms however fast the page is mutating.
  const want = String(text || "").toLowerCase();
  const holds = () => {
    if (selector && !document.querySelector(selector)) return false;
    if (want && !(document.body && (document.body.innerText || "").toLowerCase().includes(want))) return false;
    return true;
  };
  if (holds()) return true;
  return new Promise((resolve) => {
    let timer = null, done = false;
    const finish = (value) => {
      if (done) return;
      done = true; observer.disconnect(); clearTimeout(timer); clearTimeout(limit); resolve(value);
    };
    const check = () => { timer = null; if (holds()) finish(true); };
    const observer = new MutationObserver(() => { if (!timer) timer = setTimeout(check, 150); });
    observer.observe(document.documentElement, {childList: true, subtree: true, characterData: true, attributes: !!selector});
    const limit = setTimeout(() => finish(holds()), timeoutMs || 2000);
  });
}

function pageText(max) {
  const main = document.querySelector("main,[role=main]") || document.body;
  const text = (main ? main.innerText : document.body.innerText) || "";
  return {title: document.title, url: location.href, text: text.replace(/\n{3,}/g, "\n\n").slice(0, max)};
}

function pageClick(text, nth) {
  // Injected functions are serialised on their own, so helpers must live inside them.
  //
  // MATCHING, best first (the first tier with any hit wins):
  //   3   exact accessible name               "Sent" == "Sent"
  //   2.5 same words ignoring plurals         "all mails" == "All Mail"
  //   2   name starts with the words          "more" -> "More labels"
  //   1.5 every spoken word is in the name    "load more" ~ "Show more messages" (load=show)
  //   1   the words appear inside the name    "reply" in "Reply all"
  // and, when nothing named matches, a VISIBLE TEXT fallback: the smallest element whose own
  // text is the phrase, clicked through its nearest clickable ancestor (Gmail's spans).
  const SYN = {load: "show", view: "show", mails: "mail", emails: "mail", email: "mail",
               settings: "setting", options: "option", button: "", link: "", tab: "", icon: ""};
  const norm = (s) => String(s || "").toLowerCase().replace(/\(.*?\)/g, " ").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const stem = (w) => (SYN[w] !== undefined ? SYN[w] : w).replace(/(ies)$/, "y").replace(/(?<=..)(es|s)$/, "");
  const words = (s) => norm(s).split(" ").map(stem).filter(Boolean);
  const want = norm(text);
  const wantWords = words(text);
  if (!want || !wantWords.length) return {clicked: false, reason: "nothing to click"};
  const sel = "button,a,[role=button],[role=link],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox]," +
    "[role=tab],[role=option],[role=checkbox],[role=switch],[role=radio],[role=treeitem],[role=row]," +
    "input[type=button],input[type=submit],input[type=checkbox],input[type=radio],summary,label," +
    "[aria-label],[data-tooltip],[jsname][tabindex],[tabindex='0'],[jsaction*='click']";
  const all = [];
  const walk = (root) => {
    root.querySelectorAll(sel).forEach((el) => all.push(el));
    root.querySelectorAll("*").forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); });
  };
  walk(document);
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none" && parseFloat(st.opacity || "1") > 0.05;
  };
  const scoreName = (raw) => {
    const n = norm(raw); if (!n) return 0;
    if (n === want) return 3;
    const nw = words(raw);
    if (nw.join(" ") === wantWords.join(" ")) return 2.5;
    if (n.startsWith(want + " ") || nw.slice(0, wantWords.length).join(" ") === wantWords.join(" ")) return 2;
    if (nw.length <= 8 && wantWords.every((w) => nw.includes(w))) return 1.5;
    if ((" " + n + " ").includes(" " + want + " ")) return 1;
    return 0;
  };
  // ORDER MATTERS FOR COST. Names are scored first and visibility is checked only for the
  // few elements that matched: `getComputedStyle` + a layout read on every candidate of a
  // Gmail- or YouTube-sized page was most of the time a click took. `innerText` also forces
  // layout, so it is read only for elements small enough to be a control at all.
  const scored = [];
  for (const el of all) {
    if (el.matches("input[type=password]")) continue;
    const raw = el.textContent || "";
    const text = raw.length <= 400 ? (el.innerText || "").trim() : "";
    const names = [el.getAttribute("aria-label"), el.getAttribute("data-tooltip"), el.getAttribute("title"),
                   el.value, text.length <= 80 ? text : "", el.getAttribute("alt")].filter(Boolean);
    let score = 0, label = names[0] || text;
    for (const n of names) { const sc = scoreName(n); if (sc > score) { score = sc; label = n; } }
    if (score && visible(el)) scored.push({el, score, label});
  }
  let top = [];
  if (scored.length) {
    const best = Math.max(...scored.map((s) => s.score));
    top = scored.filter((s) => s.score === best);
    // Innermost element wins among equals: a button inside a labelled container is the target.
    top = top.filter((s) => !top.some((o) => o !== s && s.el.contains(o.el)));
  } else {
    // Visible-text fallback.
    const it = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = it.nextNode())) {
      if (scoreName(node.textContent) < 2.5) continue;
      const el = node.parentElement;
      if (!el || !visible(el)) continue;
      const target = el.closest("a,button,[role],[tabindex],[jsaction]") || el;
      if (!top.some((t) => t.el === target)) top.push({el: target, score: 0.5, label: node.textContent.trim()});
    }
  }
  if (!top.length) return {clicked: false, reason: "not found"};
  const index = nth === -1 ? top.length - 1 : Math.min(Math.max(1, nth) - 1, top.length - 1);
  const pick = top[index];
  pick.el.scrollIntoView({block: "center", inline: "center"});
  const r = pick.el.getBoundingClientRect();
  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, button: 0};
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
    pick.el.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, opts));
  }
  pick.el.click();
  return {clicked: true, label: String(pick.label).trim().slice(0, 80), matches: top.length,
          exact: pick.score >= 2.5};
}

function gmailOpen(which, nth, query) {
  // Gmail's message list: one <tr class="zA"> per conversation ("zE" = unread). Sender in an
  // element carrying an `email` attribute, subject in `.bog`, full timestamp in the date
  // cell's `title`. Opening = the same mouse sequence a click on the subject produces.
  const rows = Array.from(document.querySelectorAll("tr.zA")).filter((r) => {
    const b = r.getBoundingClientRect(); return b.width > 0 && b.height > 0;
  });
  if (!rows.length) return {opened: false, reason: "no emails visible"};
  const info = (r) => {
    const who = r.querySelector("[email]");
    const dateEl = r.querySelector(".xW span[title], td.xW [title], .apt [title]");
    const title = dateEl ? dateEl.getAttribute("title") : "";
    let ts = Date.parse(String(title).replace(/^[A-Za-z]{3},\s*/, "").replace(/\s+at\s+/, " "));
    return {name: who ? (who.getAttribute("name") || who.innerText) : "", email: who ? who.getAttribute("email") : "",
            subject: ((r.querySelector(".bog") || {}).innerText || "").trim(), title, ts: isNaN(ts) ? 0 : ts,
            unread: r.classList.contains("zE")};
  };
  let pool = rows.map((r, i) => ({r, i, info: info(r)}));
  const q = String(query || "").toLowerCase().trim();
  if (q) pool = pool.filter((p) => (p.info.name + " " + p.info.email + " " + p.info.subject).toLowerCase().includes(q));
  if (which === "unread") pool = pool.filter((p) => p.info.unread);
  if (!pool.length) return {opened: false, reason: q ? "no email matches " + q : "no matching email"};
  let pick;
  if (which === "latest") pick = pool.slice().sort((a, b) => b.info.ts - a.info.ts || a.i - b.i)[0];
  else if (which === "oldest") pick = pool.slice().sort((a, b) => a.info.ts - b.info.ts)[0];
  else if (which === "last") pick = pool[pool.length - 1];
  else pick = pool[Math.min(Math.max(1, nth || 1) - 1, pool.length - 1)];
  const target = pick.r.querySelector(".bog, .xT, .y6") || pick.r;
  target.scrollIntoView({block: "center"});
  const b = target.getBoundingClientRect();
  const opts = {bubbles: true, cancelable: true, view: window, clientX: b.x + 4, clientY: b.y + b.height / 2, button: 0};
  for (const type of ["mousedown", "mouseup", "click"]) target.dispatchEvent(new MouseEvent(type, opts));
  return {opened: true, from: pick.info.name || pick.info.email, subject: pick.info.subject, date: pick.info.title,
          count: pool.length};
}

function pageType(field, text, submit, clear) {
  const norm = (s) => String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const want = norm(field);
  const inputs = Array.from(document.querySelectorAll("input,textarea,[contenteditable=true],[contenteditable=''],[role=textbox],[role=combobox]"))
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; })
    .filter((el) => !["hidden", "submit", "button", "checkbox", "radio", "file"].includes((el.type || "").toLowerCase()));
  const labelOf = (el) => {
    const parts = [el.getAttribute("aria-label"), el.placeholder, el.name, el.id, el.title,
                   el.getAttribute("data-placeholder")];
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) parts.push(l.innerText); }
    const wrap = el.closest("label"); if (wrap) parts.push(wrap.innerText);
    return parts.filter(Boolean).map(norm);
  };
  let target = null;
  if (want) {
    target = inputs.find((el) => labelOf(el).some((n) => n === want)) ||
             inputs.find((el) => labelOf(el).some((n) => n.includes(want)));
  } else {
    target = inputs.includes(document.activeElement) ? document.activeElement : inputs[0];
  }
  if (!target) return {typed: false, reason: "field not found"};
  if ((target.type || "").toLowerCase() === "password" || /pass(word|code)|\bpin\b|cvv|card number/.test(labelOf(target).join(" ")))
    return {typed: false, reason: "password or payment field — Kayra never types into those"};
  target.focus();
  if (target.isContentEditable || target.getAttribute("role") === "textbox" && !("value" in target)) {
    if (clear) document.execCommand("selectAll", false);
    document.execCommand("insertText", false, text);
  } else {
    const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(target, clear ? text : (target.value || "") + text);
    target.dispatchEvent(new Event("input", {bubbles: true}));
    target.dispatchEvent(new Event("change", {bubbles: true}));
  }
  if (submit) {
    const enter = {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true};
    target.dispatchEvent(new KeyboardEvent("keydown", enter));
    target.dispatchEvent(new KeyboardEvent("keyup", enter));
    if (target.form && target.form.requestSubmit) { try { target.form.requestSubmit(); } catch (e) {} }
  }
  return {typed: true, field: labelOf(target)[0] || "field"};
}

function pageScroll(direction, until) {
  const step = (direction === "up" ? -1 : 1) * window.innerHeight * 0.8;
  if (direction === "top") { window.scrollTo(0, 0); return {scrolled: true}; }
  if (direction === "bottom") { window.scrollTo(0, document.body.scrollHeight); return {scrolled: true}; }
  if (!until) { window.scrollBy(0, step); return {scrolled: true}; }
  const want = String(until).toLowerCase();
  const walker = () => {
    const it = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n; while ((n = it.nextNode())) {
      if ((n.textContent || "").toLowerCase().includes(want)) {
        const el = n.parentElement; const r = el.getBoundingClientRect();
        if (r.height > 0) { el.scrollIntoView({block: "center"}); return true; }
      }
    }
    return false;
  };
  return {found: walker()};
}

function pageFind(text) {
  const want = String(text || "").toLowerCase();
  const body = (document.body.innerText || "").toLowerCase();
  let count = 0, at = body.indexOf(want);
  while (want && at !== -1) { count++; at = body.indexOf(want, at + want.length); }
  return {count};
}

// Part 5 (H0.4): WhatsApp Web, by what a screen reader announces first (roles, titles,
// aria-labels), with the older class names only as a fallback. Rows are in WhatsApp's own
// order — most recent first — which the matcher uses to break ties.
function pageChatList(max) {
  const side = document.querySelector("#pane-side, [aria-label='Chat list'], [aria-label*='Chat list' i]");
  if (!side) return {ready: false, chats: []};
  const rows = Array.from(side.querySelectorAll("[role=listitem], [role=row]"))
    .filter((r) => !r.parentElement.closest("[role=listitem], [role=row]"));   // outermost rows only
  const chats = [];
  for (const row of rows) {
    const titled = Array.from(row.querySelectorAll("span[title], div[title]")).filter((e) => e.getAttribute("title").trim());
    if (!titled.length) continue;
    const name = titled[0].getAttribute("title").trim();
    const preview = titled.length > 1 ? titled[titled.length - 1].getAttribute("title").trim() : "";
    const badge = row.querySelector("[aria-label*='unread message' i], [aria-label*='unread' i]");
    const unread = badge ? parseInt((badge.getAttribute("aria-label").match(/\d+/) || ["1"])[0], 10) : 0;
    const when = Array.from(row.querySelectorAll("div, span")).map((e) => (e.childElementCount ? "" : e.textContent.trim()))
      .find((t) => /^(\d{1,2}:\d{2}(\s?[AP]M)?|yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\/\d{1,2}\/\d{2,4})$/i.test(t)) || "";
    chats.push({name, preview: preview === name ? "" : preview.slice(0, 160), unread, when});
    if (chats.length >= max) break;
  }
  return {ready: true, chats};
}

function pageChatOpen(name) {
  const side = document.querySelector("#pane-side, [aria-label='Chat list'], [aria-label*='Chat list' i]");
  if (!side) return {opened: false, reason: "no chat list"};
  const target = Array.from(side.querySelectorAll("span[title], div[title]"))
    .find((e) => e.getAttribute("title").trim() === name);
  if (!target) return {opened: false, reason: "not in the list"};
  const row = target.closest("[role=listitem], [role=row]") || target;
  const r = row.getBoundingClientRect();
  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
    target.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, opts));
  }
  target.click();
  return {opened: true, name};
}

function pageChatMessages(limit) {
  const main = document.querySelector("#main");
  if (!main) return {chat: "", messages: []};
  const head = main.querySelector("header span[title], header [dir=auto]");
  const chat = head ? (head.getAttribute("title") || head.textContent).trim() : "";
  const out = [];
  for (const el of main.querySelectorAll("[data-pre-plain-text]")) {
    const meta = el.getAttribute("data-pre-plain-text") || "";          // "[10:21, 11/10/2026] Ana: "
    const m = meta.match(/^\[([^,\]]+),\s*([^\]]+)\]\s*(.*?):\s*$/);
    const text = (el.querySelector(".selectable-text, [dir]") || el).innerText.replace(/\s+/g, " ").trim();
    if (!text) continue;
    out.push({time: m ? m[1].trim() : "", date: m ? m[2].trim() : "", from: m ? m[3].trim() : "",
              text: text.slice(0, 1000), out: !!el.closest(".message-out, [class*='message-out']")});
  }
  return {chat, messages: out.slice(-limit)};
}

function pageChatComposer() {
  const box = document.querySelector("footer [contenteditable=true], #main [contenteditable=true][role=textbox]");
  const head = document.querySelector("#main header span[title], #main header [dir=auto]");
  return {found: !!box, text: box ? box.innerText.trim() : "",
          chat: head ? (head.getAttribute("title") || head.textContent).trim() : ""};
}

// Part 4: the files listed on a Docs / Sheets / Slides / Forms home page or a Drive page, in
// page order: {id, title, url, kind}. Read only while Kayra is acting; never stored.
function pageDocList(max) {
  const kinds = {spreadsheets: "sheet", document: "doc", presentation: "slides", forms: "form"};
  const out = []; const seen = new Set();
  const add = (id, title, url, kind) => {
    title = String(title || "").replace(/\s+/g, " ").trim();
    if (!id || seen.has(id) || !title) return;
    seen.add(id); out.push({id, title: title.slice(0, 150), url, kind: kind || "file"});
  };
  for (const a of document.querySelectorAll("a[href]")) {
    const m = a.href.match(/docs\.google\.com\/(spreadsheets|document|presentation|forms)\/d\/([\w-]{20,})/);
    if (m) add(m[2], a.getAttribute("aria-label") || a.title || a.innerText, a.href.split("#")[0], kinds[m[1]]);
  }
  const path = location.pathname;
  const here = Object.keys(kinds).find((k) => path.startsWith("/" + k));
  for (const el of document.querySelectorAll("[data-id]")) {
    const id = el.getAttribute("data-id");
    if (!/^[\w-]{25,}$/.test(id)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const t = el.querySelector(".docs-homescreen-list-item-title-value, [data-tooltip], [aria-label]");
    const title = (t && (t.innerText || t.getAttribute("data-tooltip") || t.getAttribute("aria-label")))
      || (el.getAttribute("aria-label") || (el.innerText || "").split("\n")[0]);
    const url = location.hostname === "drive.google.com" || !here
      ? `https://drive.google.com/open?id=${id}`
      : `https://docs.google.com/${here}/d/${id}/edit`;
    add(id, title, url, here ? kinds[here] : "file");
    if (out.length >= max) break;
  }
  return out.slice(0, max);
}

// Part 4: text from a docs.google.com address with the page's own session (same origin).
async function pageFetchText(url, max) {
  const response = await fetch(url, {credentials: "include"});
  if (!response.ok) return {ok: false, status: response.status};
  const text = await response.text();
  return {ok: true, text: text.slice(0, max), truncated: text.length > max};
}

// Part 4 (E6): the next event in Calendar's agenda/schedule view. open = click it, so its
// details (and the Meet link) are shown; Kayra then calls again to read the link.
function pageCalendarNext(open) {
  // Only a VISIBLE link: Calendar keeps other events' details in the page, hidden.
  const meet = Array.from(document.querySelectorAll("a[href^='https://meet.google.com/']"))
    .filter((a) => { const r = a.getBoundingClientRect(); return r.width > 2 && r.height > 2; })
    .map((a) => a.href).find((h) => /meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/.test(h));
  const chips = Array.from(document.querySelectorAll("[data-eventid], [data-eventchip]"))
    .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; });
  const first = chips[0];
  const title = first ? (first.innerText || first.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim() : "";
  if (open && first && !meet) first.click();
  return {title: title.slice(0, 200), meet: meet || "", events: chips.length};
}

// C6: comments or reviews, site-aware, the visible ones in page order. On YouTube the comments
// load only once scrolled to, so the section is scrolled into view and an empty list means
// "not loaded yet" (Kayra polls again).
function pageComments(max) {
  const host = location.hostname;
  let selector = "[itemprop=reviewBody], [data-hook=reviewText], [data-hook=review-body], .comment-body, .comment p, " +
                 "[class*=comment-content], [class*=review-text], [class*=reviewText]";
  let kind = "comments";
  if (/youtube\.com$/.test(host)) {
    const section = document.querySelector("ytd-comments, #comments");
    if (section) section.scrollIntoView({block: "start"});
    selector = "ytd-comment-thread-renderer #content-text";
  } else if (/reddit\.com$/.test(host)) {
    selector = "shreddit-comment div[slot=comment], div[data-testid=comment] p";
  } else if (/(^|\.)amazon\./.test(host)) {
    // Live 2026-11-10: review text is data-hook="reviewText" (review-body is gone), and the
    // reviews load only once the section is scrolled to.
    const section = document.querySelector("#customerReviews, #reviewsMedley, #cm-cr-dp-review-list");
    if (section) section.scrollIntoView({block: "start"});
    selector = "[data-hook=reviewText], [data-hook=review-body], [data-hook=review-collapsed]"; kind = "reviews";
  } else if (/flipkart\.com$/.test(host)) {
    selector = "div.t-ZTKy, div.ZmyHeo, div[class*=review] p"; kind = "reviews";
  } else if (/news\.ycombinator\.com$/.test(host)) {
    selector = ".commtext";
  } else if (/github\.com$/.test(host)) {
    selector = ".comment-body, .js-comment-body";
  } else if (document.querySelector("[itemprop=reviewBody], [data-hook=review-body]")) {
    kind = "reviews";
  }
  const seen = new Set(); const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    if (text.length < 3 || seen.has(text)) continue;
    seen.add(text); out.push(text.slice(0, 600));
    if (out.length >= max) break;
  }
  return {kind, site: host, comments: out};
}

// C6: a YouTube video's transcript. open = press "...more" and "Show transcript" first; the
// panel then loads asynchronously, so Kayra calls again without open until segments appear.
function pageTranscript(open) {
  if (!/youtube\.com$/.test(location.hostname)) return {supported: false};
  const segments = () => Array.from(document.querySelectorAll(
    "ytd-transcript-segment-renderer .segment-text, ytd-transcript-segment-renderer yt-formatted-string"))
    .map((el) => (el.innerText || "").trim()).filter(Boolean);
  let have = segments();
  if (!have.length && open) {
    const expand = document.querySelector("#description-inline-expander #expand, tp-yt-paper-button#expand");
    if (expand) expand.click();
    // The VISIBLE <button> (live: two renderers + two buttons match, two of them hidden).
    const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
    const buttons = Array.from(document.querySelectorAll("button, ytd-button-renderer, [role=button]"))
      .filter((b) => /show transcript/i.test(b.innerText || b.getAttribute("aria-label") || ""));
    const show = buttons.find((b) => b.tagName === "BUTTON" && visible(b)) || buttons.find(visible);
    if (show) show.click();
    have = segments();
    return {supported: true, opened: !!show, text: have.join(" ").slice(0, 20000)};
  }
  const title = (document.querySelector("h1.ytd-watch-metadata, h1 yt-formatted-string") || {}).innerText || document.title;
  const description = ((document.querySelector("#description-inline-expander, #description") || {}).innerText || "").slice(0, 3000);
  return {supported: true, title, description, text: have.join(" ").slice(0, 20000)};
}

// C3: the result list of the page, site-aware. nth = 0 lists them; nth = N (or -1 = last)
// returns that one. Only links with an http(s) address; duplicates by address dropped.
function pageResults(nth, kind) {
  const host = location.hostname;
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  const pick = (selector, linkOf) => Array.from(document.querySelectorAll(selector))
    .map((el) => { const a = linkOf ? linkOf(el) : el; return a && a.href ? {a, label: (el.innerText || a.innerText || a.getAttribute("aria-label") || a.title || "").trim()} : null; })
    .filter((x) => x && visible(x.a));
  let items = [];
  let site = "page";
  // A bot check ("unusual traffic", a captcha) is not a result list: say so instead of
  // offering "Why did this happen?" as the first result (seen live on Google, 2026-11-10).
  if (/\/sorry\/|captcha|validateCaptcha/i.test(location.pathname + location.search) ||
      document.querySelector("form[action*='captcha' i], iframe[src*='recaptcha'], #captcha-form")) {
    return {site: "blocked", count: 0, blocked: true, results: [], result: null};
  }
  if (/(^|\.)google\./.test(host) && location.pathname.startsWith("/search")) {
    site = "google"; items = pick("#search a h3, #rso a h3", (h) => h.closest("a"));
  } else if (/youtube\.com$/.test(host)) {
    site = "youtube";
    // Search results (a#video-title), the home grid, and the newer "lockup" cards YouTube uses
    // for Up next (seen live 2026-11-10: 20 lockups, no compact renderers). Never an ad slot.
    const lockups = () => pick("yt-lockup-view-model a[href*='/watch?v=']:has(h3), yt-lockup-view-model h3 a[href*='/watch?v='], yt-lockup-view-model a[href*='/watch?v=']")
      .filter((x) => !x.a.closest("ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer"))   // live: card 1 was an ad
      .map((x) => { const card = x.a.closest("yt-lockup-view-model"); const h = card && card.querySelector("h3");
                    return {a: x.a, label: (h && h.textContent.trim()) || x.label}; });
    const titled = () => pick("ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link, a#video-title")
      .filter((x) => !x.a.closest("ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer"));
    // On a video page the Up next cards are the list (live: a shelf of 4 other links there
    // labelled "1:03" won before); on search and home, the titled results.
    items = location.pathname.startsWith("/watch") ? (lockups().length ? lockups() : titled())
                                                   : (titled().length ? titled() : lockups());
    // A bare duration is not a title.
    items = items.filter((x) => !/^\d{1,2}(:\d{2}){1,2}$/.test(x.label.trim()));
  } else if (/(^|\.)amazon\./.test(host)) {
    site = "amazon";
    // Sponsored listings are skipped: "open the first result" means the first REAL result
    // (live 2026-11-10: the first two were /sspa/ ads). The title link is now an <a> around <h2>.
    items = pick("div[data-component-type='s-search-result'] a:has(h2), div[data-component-type='s-search-result'] h2 a")
      .filter((x) => !/\/sspa\//.test(x.a.getAttribute("href") || "") &&
        !x.a.closest("div[data-component-type='s-search-result']").querySelector(".puis-sponsored-label-text, .s-sponsored-label-text"));
  } else if (/flipkart\.com$/.test(host)) {
    site = "flipkart"; items = pick("a[href*='/p/']");
  } else if (/github\.com$/.test(host)) {
    site = "github"; items = pick("[data-testid='results-list'] .search-title a, .repo-list-item a.v-align-middle, div.search-title a");
  } else if (/reddit\.com$/.test(host)) {
    site = "reddit"; items = pick("a[data-testid='post-title'], shreddit-post a[slot='title'], a[slot='full-post-link']");
  } else if (/duckduckgo\.com$/.test(host)) {
    site = "duckduckgo"; items = pick("a[data-testid='result-title-a'], h2 a.result__a");
  } else if (/bing\.com$/.test(host)) {
    site = "bing"; items = pick("#b_results li.b_algo h2 a");     // organic only (live: 10 of 12 h2 links)
  } else if (/wikipedia\.org$/.test(host)) {
    site = "wikipedia"; items = pick(".mw-search-result-heading a");
  }
  if (!items.length) {
    // Generic: links with real text inside the main content, not the header, nav or footer.
    // When the page lists several HEADING links (a result list, a blog index, news), those are
    // the results — help and footnote links in between are not (live: Wikipedia search).
    const root = document.querySelector("main, [role=main], article, #content") || document.body;
    const content = pick("a[href]").filter(({a, label}) => root.contains(a) && label.length >= 12 &&
      !a.closest("nav, header, footer, [role=navigation], [role=banner], [role=contentinfo], aside"));
    const headed = content.filter(({a}) => a.closest("h1, h2, h3, h4") || a.querySelector("h1, h2, h3, h4"));
    items = headed.length >= 3 ? headed : content;
    if (headed.length >= 3) site = "headings";
  }
  const seen = new Set(); const list = [];
  for (const it of items) {
    if (!/^https?:/.test(it.a.href)) continue;
    const key = it.a.href.split("#")[0];
    if (seen.has(key)) continue;
    seen.add(key); list.push({title: it.label.replace(/\s+/g, " ").slice(0, 120), url: it.a.href});
  }
  if (!nth) return {site, count: list.length, results: list.slice(0, 10)};
  const index = nth > 0 ? nth - 1 : list.length + nth;
  if (index < 0 || index >= list.length) return {site, count: list.length, result: null};
  return {site, count: list.length, result: list[index]};
}

// C4: find on the page WITH highlight, and step through the matches. Marks are <mark
// data-kayra-find>; the current one gets an outline and is scrolled to. clear = unwrap them.
function pageHighlight(text, step, clear) {
  const unwrap = () => {
    document.querySelectorAll("mark[data-kayra-find]").forEach((m) => {
      const parent = m.parentNode;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    });
  };
  const show = (state) => {
    const marks = document.querySelectorAll("mark[data-kayra-find]");
    marks.forEach((m, i) => { m.style.outline = i === state.i ? "2px solid #ff6d00" : "none";
                              m.style.background = i === state.i ? "#ffb74d" : "#fff176"; });
    if (marks[state.i]) marks[state.i].scrollIntoView({block: "center", behavior: "instant"});
    return {count: marks.length, index: marks.length ? state.i + 1 : 0, text: state.text};
  };
  if (clear) { unwrap(); window.__kayraFind = null; return {cleared: true}; }
  if (!text && step) {
    const state = window.__kayraFind;
    const count = document.querySelectorAll("mark[data-kayra-find]").length;
    if (!state || !count) return {count: 0, index: 0, text: ""};
    state.i = (state.i + step + count) % count;
    return show(state);
  }
  unwrap();
  const want = String(text || "").toLowerCase();
  if (!want) return {count: 0, index: 0, text: ""};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      const p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA|INPUT)$/.test(p.tagName)) return NodeFilter.FILTER_REJECT;
      if (p.closest("[contenteditable=true]")) return NodeFilter.FILTER_REJECT;
      return n.nodeValue.toLowerCase().includes(want) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    }});
  const nodes = [];
  while (walker.nextNode() && nodes.length < 300) nodes.push(walker.currentNode);
  let made = 0;
  for (const node of nodes) {
    let current = node;
    while (current && made < 500) {
      const at = current.nodeValue.toLowerCase().indexOf(want);
      if (at === -1) break;
      const match = current.splitText(at);
      const rest = match.splitText(want.length);
      const mark = document.createElement("mark");
      mark.setAttribute("data-kayra-find", "");
      match.parentNode.replaceChild(mark, match);
      mark.appendChild(match);
      made += 1;
      current = rest;
    }
  }
  window.__kayraFind = {i: 0, text: String(text)};
  return show(window.__kayraFind);
}

function pageControls(limit) {
  const _kayraCandidates = () => {
    const sel = "button,a,[role=button],[role=link],[role=menuitem],[role=menuitemradio],[role=tab]," +
      "[role=option],[role=checkbox],[role=switch],[role=radio],input[type=button],input[type=submit]," +
      "input[type=checkbox],input[type=radio],summary,label,[aria-label],[data-tooltip],[jsname][tabindex]";
    const out = [];
    const walk = (root) => {
      root.querySelectorAll(sel).forEach((el) => out.push(el));
      root.querySelectorAll("*").forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); });
    };
    walk(document);
    return out;
    };
  const seen = new Set(); const out = [];
  for (const el of _kayraCandidates()) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const name = (el.getAttribute("aria-label") || el.getAttribute("data-tooltip") || el.innerText || "").trim().replace(/\s+/g, " ");
    if (!name || name.length > 60 || seen.has(name)) continue;
    seen.add(name); out.push({name, pressed: el.getAttribute("aria-pressed")});
    if (out.length >= limit) break;
  }
  return out;
}
