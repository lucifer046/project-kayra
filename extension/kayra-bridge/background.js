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
    // ── 1.11.0: search WhatsApp and WAIT for its results; close the open chat ──
    case "chat.search": return inPage(await targetTab(args), pageChatSearch, [args.text || "", args.max || 60, args.settleMs || 4000]);
    case "chat.close": return inPage(await targetTab(args), pageChatClose, []);

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
    case "page.scroll": return inPage(await targetTab(args), pageScroll, [args.direction || "down", args.until || "", args.where || "", args.amount || 1]);
    // ── 1.10.0: one search action for every web app, and its undo ──
    case "page.search": return inPage(await targetTab(args), pageSearch, [args.text || "", !!args.submit]);
    case "page.searchClose": return inPage(await targetTab(args), pageSearchClose, []);
    // ── 1.12.0: forms, the video, the product (Part 6) ──
    case "page.form": return inPage(await targetTab(args), pageForm, [args.op || "list", args]);
    case "page.video": return inPage(await targetTab(args), pageVideo, [args.op || "state", args.value === undefined ? null : args.value]);
    case "page.product": return untilReady(args, pageProduct, [], (r) => r && r.found);
    // ── 1.13.0: ask an AI site, send a file in WhatsApp, read social lists (Part 6b) ──
    case "page.aiChat": return inPage(await targetTab(args), pageAiChat, [args.op || "state", args]);
    case "chat.attach": return inPage(await targetTab(args), pageChatAttach, [args.op || "state", args]);
    // ── 1.14.0: the page agent's eyes and hands (Part 7, H1) ──
    case "page.snapshot": return inPage(await targetTab(args), pageSnapshot, [args.max || 120, args.textMax || 1500]);
    case "page.act": return inPage(await targetTab(args), pageAct, [args.op || "click", args]);
    case "page.social": return untilReady(args, pageSocial, [args.kind || "posts", args.max || 10], (r) => r && r.items && r.items.length);
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
    if (text) document.execCommand("insertText", false, text);
    else if (clear) document.execCommand("delete", false);
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

// 1.10.0: scrolls what the user is LOOKING at. Gmail, WhatsApp, Docs and most web apps never
// scroll the window: the mail, the chat or the list is a box inside the page, and
// window.scrollBy moved nothing while Kayra said "Done". The box is the scrollable ancestor of
// what sits in the middle of the view (or at the left edge for where = "side"), else the
// biggest scrollable box, else the page. `moved` is measured, never assumed.
function pageScroll(direction, until, where, amount) {
  if (until) {
    const want = String(until).toLowerCase();
    const it = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n; while ((n = it.nextNode())) {
      if ((n.textContent || "").toLowerCase().includes(want)) {
        const el = n.parentElement; const r = el.getBoundingClientRect();
        if (r.height > 0) { el.scrollIntoView({block: "center"}); return {found: true}; }
      }
    }
    return {found: false};
  }
  const page = document.scrollingElement || document.documentElement;
  const scrolls = (el) => {
    if (!el || el === document.body || el === document.documentElement || el === page) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 60 || r.bottom <= 0 || r.top >= innerHeight) return false;
    return /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 8;
  };
  const above = (el) => {
    while (el && el !== document.body && el !== document.documentElement) {
      if (scrolls(el)) return el;
      el = el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
    }
    return null;
  };
  const inside = (root) => {
    if (!root) return null;
    let best = null, area = 0;
    for (const el of [root, ...root.querySelectorAll("*")]) {
      if (!scrolls(el)) continue;
      const r = el.getBoundingClientRect(); const a = r.width * Math.min(r.height, innerHeight);
      if (a > area) { best = el; area = a; }
    }
    return best;
  };
  const at = (fx, fy) => above(document.elementFromPoint(innerWidth * fx, innerHeight * fy));
  const side = /^(side|sidebar|list|chats|chat list|menu)$/.test(String(where || ""));
  const pageScrolls = page.scrollHeight > innerHeight + 8;
  const boxes = [];
  if (location.hostname === "web.whatsapp.com") {
    const chats = document.querySelector("#pane-side");
    const main = document.querySelector("#main");
    boxes.push(side || !main ? above(chats) || inside(chats) : inside(main));
  } else if (side) {
    boxes.push(at(0.1, 0.5));
  } else {
    boxes.push(at(0.5, 0.5), at(0.5, 0.3), at(0.65, 0.65));
  }
  if (!pageScrolls) boxes.push(inside(document.body));
  const tried = new Set();
  const step = (direction === "up" ? -1 : 1) * 0.8 * (Number(amount) || 1);
  for (const box of [...boxes.filter(Boolean), null]) {
    if (tried.has(box)) continue;
    tried.add(box);
    if (!box && !pageScrolls) break;
    const get = () => (box ? box.scrollTop : (page.scrollTop || window.scrollY));
    const full = box ? box.scrollHeight : page.scrollHeight;
    const view = box ? box.clientHeight : innerHeight;
    const before = get();
    const top = direction === "top" ? 0 : direction === "bottom" ? full : before + step * view;
    (box || window).scrollTo({top, behavior: "instant"});
    const now = get();
    if (Math.abs(now - before) > 1)
      return {scrolled: true, moved: true, inner: !!box, atEnd: now + view >= full - 2, atStart: now <= 1};
  }
  // Nothing moved: already at that end, or the app draws on a canvas (Sheets) and has no box.
  const any = boxes.some(Boolean) || pageScrolls;
  return {scrolled: any, moved: false, canScroll: any};
}

// 1.10.0: THE search box of the page in front, for every web app. It is found by what it is
// (type=search, role=searchbox, inside role=search, named "search"/"q"), not by one site's
// markup. When the box is hidden behind a magnifier button (YouTube narrow, Drive, Slack),
// that button is pressed first and the box waited for. Never a password or payment field.
async function pageSearch(text, submit) {
  const words = (el) => [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("name"), el.id,
                         el.getAttribute("title"), el.getAttribute("data-placeholder"), el.getAttribute("aria-placeholder"),
                         el.getAttribute("data-tooltip")].filter(Boolean).join(" ").toLowerCase();
  const shown = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 20 && r.height > 8 && s.visibility !== "hidden" && s.display !== "none";
  };
  const score = (el) => {
    const type = (el.type || "").toLowerCase();
    if (["hidden", "submit", "button", "checkbox", "radio", "file", "password"].includes(type)) return 0;
    const w = words(el);
    if (/pass(word|code)|\bpin\b|cvv|card number/.test(w)) return 0;
    let n = 0;
    if (type === "search" || el.getAttribute("role") === "searchbox") n = 5;
    else if (el.closest("[role=search], form[action*='search' i], form[role=search]")) n = 4;
    else if (/\bsearch|\bfind\b/.test(w) || /^(q|query|search_query|k|s)$/.test(el.getAttribute("name") || "")) n = 3;
    if (!n) return 0;
    const r = el.getBoundingClientRect();
    return n + (r.top >= 0 && r.top < innerHeight * 0.45 ? 1 : 0) + (el === document.activeElement ? 1 : 0);
  };
  const best = () => Array.from(document.querySelectorAll(
    "input,textarea,[contenteditable=true],[contenteditable=''],[role=textbox],[role=searchbox],[role=combobox]"))
    .filter(shown).map((el) => [score(el), el]).filter((p) => p[0] > 0).sort((a, b) => b[0] - a[0]).map((p) => p[1])[0] || null;
  let box = best(), opened = false;
  if (!box) {
    const opener = Array.from(document.querySelectorAll("button,[role=button],a[aria-label],[data-tooltip]")).filter(shown)
      .find((b) => /^(open |show )?search\b/.test(words(b) || (b.innerText || "").trim().toLowerCase()));
    if (opener) {
      opener.click(); opened = true;
      const deadline = Date.now() + 1500;
      while (!box && Date.now() < deadline) { await new Promise((r) => setTimeout(r, 80)); box = best(); }
    }
  }
  if (!box) return {searched: false, reason: "no search box"};
  const r = box.getBoundingClientRect();
  const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  for (const type of ["mousedown", "mouseup", "click"]) box.dispatchEvent(new MouseEvent(type, opts));
  // The OLD text is replaced, never added to: the box's own content is selected first (a
  // Range, so it does not depend on the window having the keyboard focus) and what the box
  // holds afterwards is read back, so Kayra knows when a page refused the text.
  const editable = box.isContentEditable || (box.getAttribute("role") === "textbox" && !("value" in box));
  const valueOf = () => String(editable ? box.innerText || "" : box.value || "").replace(/\s+/g, " ").trim();
  const put = () => {
    box.focus();
    if (editable) {
      const range = document.createRange(); range.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      if (valueOf()) document.execCommand("delete", false);
      if (text) document.execCommand("insertText", false, text);
    } else {
      const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(box, text);
      box.dispatchEvent(new Event("input", {bubbles: true}));
      box.dispatchEvent(new Event("change", {bubbles: true}));
    }
  };
  put();
  if (valueOf() !== String(text).replace(/\s+/g, " ").trim()) { await new Promise((r) => setTimeout(r, 120)); put(); }
  const value = valueOf();
  if (submit) {
    const enter = {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true};
    for (const type of ["keydown", "keypress", "keyup"]) box.dispatchEvent(new KeyboardEvent(type, enter));
    if (box.form && box.form.requestSubmit) { try { box.form.requestSubmit(); } catch (e) {} }
  }
  return {searched: true, opened, value, exact: value === String(text).replace(/\s+/g, " ").trim(),
          field: words(box).slice(0, 60) || "search"};
}

// 1.10.0: "close the search" — the search box is emptied, its own Cancel / Clear / Close
// button pressed, Escape sent, and the focus given back (on WhatsApp: to the message box of
// the chat that was open), so the app looks the way it did before the search.
async function pageSearchClose() {
  const words = (el) => [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("name"), el.id,
                         el.getAttribute("title"), el.getAttribute("data-placeholder"), el.getAttribute("aria-placeholder"),
                         el.getAttribute("data-tooltip")].filter(Boolean).join(" ").toLowerCase();
  const shown = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && s.visibility !== "hidden" && s.display !== "none";
  };
  const isSearch = (el) => {
    const type = (el.type || "").toLowerCase();
    if (["hidden", "submit", "button", "checkbox", "radio", "file", "password"].includes(type)) return false;
    return type === "search" || el.getAttribute("role") === "searchbox" ||
      !!el.closest("[role=search], form[action*='search' i], form[role=search]") ||
      /\bsearch|\bfind\b/.test(words(el)) || /^(q|query|search_query|k|s)$/.test(el.getAttribute("name") || "");
  };
  const valueOf = (el) => String("value" in el && !el.isContentEditable ? el.value : el.innerText || "").trim();
  const boxes = Array.from(document.querySelectorAll(
    "input,textarea,[contenteditable=true],[contenteditable=''],[role=textbox],[role=searchbox],[role=combobox]"))
    .filter(shown).filter(isSearch);
  const box = boxes.find((el) => el === document.activeElement) || boxes.find(valueOf) || boxes[0];
  if (!box) return {closed: false, reason: "no search box"};
  const had = valueOf(box);
  const focused = box === document.activeElement;
  box.focus();
  if (box.isContentEditable || (box.getAttribute("role") === "textbox" && !("value" in box))) {
    if (had) {
      const range = document.createRange(); range.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      document.execCommand("delete", false);
    }
  } else if (had) {
    const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(box, "");
    box.dispatchEvent(new Event("input", {bubbles: true}));
    box.dispatchEvent(new Event("change", {bubbles: true}));
  }
  const whatsapp = location.hostname === "web.whatsapp.com";
  let scope = box.closest("[role=search], form") || box;
  for (let i = 0; i < 4 && scope.parentElement && !scope.matches("[role=search], form"); i++) scope = scope.parentElement;
  const button = Array.from(scope.querySelectorAll("button,[role=button]")).filter(shown).find((b) => {
    const w = words(b) || (b.innerText || "").trim().toLowerCase();
    return /^(cancel|close|clear|exit)( the)? search\b|^(cancel|clear|close)$/.test(w) || (whatsapp && /^back$|chat list/.test(w));
  });
  if (button) button.click();
  const esc = {key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true};
  box.dispatchEvent(new KeyboardEvent("keydown", esc));
  box.dispatchEvent(new KeyboardEvent("keyup", esc));
  // What the box holds a moment later is the truth: a page that refused the script keeps its
  // text, and Kayra then uses the real keyboard on the (still focused) box.
  await new Promise((r) => setTimeout(r, 150));
  const left = box.isConnected ? valueOf(box) : "";
  if (left) return {closed: false, had, left, button: !!button};
  box.blur();
  if (whatsapp) {
    const composer = document.querySelector("footer [contenteditable=true], #main [contenteditable=true][role=textbox]");
    if (composer) composer.focus();
  }
  return {closed: !!(had || focused || button), had, left: "", button: !!button};
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
    const pinned = !!row.querySelector("[data-icon*='pin' i], [aria-label*='pinned' i]");
    chats.push({name, preview: preview === name ? "" : preview.slice(0, 160), unread, when, pinned});
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

// 1.11.0: WhatsApp's own search, typed AND waited for. The old flow typed and read the list
// in the same instant, before WhatsApp had filtered it: a chat that was not on screen was
// "not found" on the first try and found on the second (the audit log, 2026-11-11). The list
// is read until it has changed and then held still, or WhatsApp says nothing matches.
// text = "" empties the search and waits for the normal list to come back.
async function pageChatSearch(text, max, settleMs) {
  const pane = () => document.querySelector("#pane-side, [aria-label='Chat list'], [aria-label*='Chat list' i], [aria-label*='Search results' i]");
  const read = () => {
    const side = pane(); const chats = [];
    if (!side) return chats;
    const rows = Array.from(side.querySelectorAll("[role=listitem], [role=row]"))
      .filter((r) => !r.parentElement.closest("[role=listitem], [role=row]"));
    for (const row of rows) {
      const titled = Array.from(row.querySelectorAll("span[title], div[title]")).filter((e) => e.getAttribute("title").trim());
      if (!titled.length) continue;
      const name = titled[0].getAttribute("title").trim();
      if (chats.some((c) => c.name === name)) continue;        // a chat AND a message hit in it: once
      const badge = row.querySelector("[aria-label*='unread message' i], [aria-label*='unread' i]");
      chats.push({name, preview: "", when: "", unread: badge ? parseInt((badge.getAttribute("aria-label").match(/\d+/) || ["1"])[0], 10) : 0,
                  pinned: !!row.querySelector("[data-icon*='pin' i], [aria-label*='pinned' i]")});
      if (chats.length >= max) break;
    }
    return chats;
  };
  const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 20 && r.height > 8; };
  const label = (el) => [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("title"),
                         el.getAttribute("data-placeholder"), el.getAttribute("aria-placeholder")].filter(Boolean).join(" ").toLowerCase();
  const fields = Array.from(document.querySelectorAll("input,[contenteditable=true],[contenteditable=''],[role=textbox],[role=searchbox]"))
    .filter(shown).filter((el) => !el.closest("#main, footer"));
  const box = fields.find((el) => /\bsearch/.test(label(el))) || fields.find((el) => el.closest("#side"));
  if (!pane()) return {ready: false, searched: false, chats: []};
  if (!box) return {ready: true, searched: false, chats: read(), reason: "no search box"};
  const editable = box.isContentEditable || !("value" in box);
  const valueOf = () => String(editable ? box.innerText || "" : box.value || "").replace(/\s+/g, " ").trim();
  const want = String(text).replace(/\s+/g, " ").trim();
  const none = () => /no (chats|results|contacts|messages)[^.\n]{0,60}found|no results/i
    .test(((document.querySelector("#side") || pane()).innerText || "").slice(0, 4000));
  // What the list SAYS: its names, or "nothing found". A stale "nothing found" from the search
  // before is the state to move away from, exactly like a stale list.
  const sig = (list) => (list.length ? list.map((c) => c.name).join("|") : none() ? "<none>" : "");
  const before = sig(read());
  const put = () => {
    box.focus();
    if (editable) {
      const range = document.createRange(); range.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      if (valueOf()) document.execCommand("delete", false);
      if (want) document.execCommand("insertText", false, want);
    } else {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(box, want);
      box.dispatchEvent(new Event("input", {bubbles: true}));
    }
  };
  const same = valueOf() === want;
  if (!same) { put(); if (valueOf() !== want) { await new Promise((r) => setTimeout(r, 150)); put(); } }
  const typed = valueOf() === want;
  const start = Date.now(); const limit = Math.min(Math.max(settleMs || 4000, 500), 8000);
  let last = null, since = start, chats = read(), empty = false;
  while (typed && !same && Date.now() - start < limit) {
    await new Promise((r) => setTimeout(r, 110));
    chats = read();
    const now = sig(chats);
    const words = ((document.querySelector("#side") || pane()).innerText || "").slice(0, 4000);
    if (/looking for|searching/i.test(words)) { last = null; since = Date.now(); continue; }
    empty = now === "<none>";
    if (now !== last) { last = now; since = Date.now(); continue; }
    const still = Date.now() - since;
    if (now !== before && still >= 350) break;                    // filtered, and holding still
    if (now === before && Date.now() - start >= 1600 && still >= 350) break;   // the same list IS the answer
  }
  return {ready: true, searched: true, typed, empty, text: valueOf(), chats};
}

// 1.11.0: "close the chat" — Escape, the way WhatsApp's own shortcut does it.
async function pageChatClose() {
  const main = () => document.querySelector("#main");
  if (!main()) return {closed: false, open: false};
  const esc = {key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true};
  const target = document.activeElement && document.activeElement !== document.body ? document.activeElement : main();
  target.dispatchEvent(new KeyboardEvent("keydown", esc));
  target.dispatchEvent(new KeyboardEvent("keyup", esc));
  await new Promise((r) => setTimeout(r, 200));
  return {closed: !main(), open: !!main()};
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

// ── 1.12.0: Part 6 — forms (D1–D5), video controls (F1), product facts (F3) ──────────────
//
// ONE self-contained form function (page functions are serialised alone, so the helpers live
// inside it). Every field it lists is tagged data-kayra-field=<n>; "fill" and "choose" act on
// those tags, so what was previewed is exactly what is filled. Password, card, CVV, OTP and
// PIN fields are never listed as fillable and never filled. Submit never presses a button that
// pays, buys, orders or checks out (decision D1: Kayra only searches and opens products).
async function pageForm(op, args) {
  args = args || {};
  const norm = (s) => String(s || "").toLowerCase().replace(/\(.*?\)|\*/g, " ").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none";
  };
  const textOf = (id) => { const e = id && document.getElementById(id); return e ? e.innerText || e.textContent : ""; };
  const question = (el) => {
    // Google Forms and most form kits: the question's heading sits in the item around the input.
    const item = el.closest("[role=listitem], fieldset, [role=radiogroup], [role=group], .form-group, .field, .form-field");
    if (!item) return "";
    const h = item.querySelector("legend, [role=heading], label, .label, .form-label");
    return h && !h.contains(el) ? h.innerText || h.textContent : "";
  };
  const hintsOf = (el) => {
    const parts = [];
    const lb = el.getAttribute("aria-labelledby");
    if (lb) parts.push(lb.split(/\s+/).map(textOf).join(" "));
    parts.push(el.getAttribute("aria-label"));
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) parts.push(l.innerText || l.textContent); }
    const wrap = el.closest("label"); if (wrap && wrap !== el) parts.push(wrap.innerText || wrap.textContent);
    parts.push(el.placeholder, el.getAttribute("data-placeholder"), el.title, question(el), el.name, el.id);
    return parts.map(clean).filter(Boolean);
  };
  const SENSITIVE = /pass ?(word|code|phrase)|\bcvv\b|\bcvc\b|card ?(number|no)|credit card|debit card|expiry|\botp\b|one time (password|code)|\bpin\b(?! ?code)|security code|\bssn\b|aadhaa?r|\bpan\b(?! ?card holder)/;
  const sensitive = (el, hints) => (el.type || "").toLowerCase() === "password" ||
    /^cc-|one-time-code|current-password|new-password/.test(el.getAttribute("autocomplete") || "") ||
    SENSITIVE.test(norm(hints.join(" ")));
  const optionLabel = (el) => clean((hintsOf(el)[0]) || el.value);
  const radioGroupLabel = (r) => {
    const group = r.closest("[role=radiogroup], fieldset");
    if (group) {
      const lb = group.getAttribute("aria-labelledby");
      const name = clean((lb && lb.split(/\s+/).map(textOf).join(" ")) || group.getAttribute("aria-label") ||
        ((group.querySelector("legend") || {}).innerText) || question(group));
      if (name) return name;
    }
    return clean(question(r) || r.name);
  };

  // ── the field list ──
  const fields = [];
  const tag = (el, i) => el.setAttribute("data-kayra-field", String(i));
  document.querySelectorAll("[data-kayra-field]").forEach((el) => el.removeAttribute("data-kayra-field"));
  const SKIP_TYPES = ["hidden", "submit", "button", "reset", "image", "file", "search", "range", "color"];
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const type = el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : (el.type || "text").toLowerCase();
    if (SKIP_TYPES.includes(type) || type === "radio") continue;
    if (!visible(el) && !(type === "checkbox" && el.closest("label") && visible(el.closest("label")))) continue;
    if (el.disabled || el.readOnly) continue;
    const hints = hintsOf(el);
    const field = {i: fields.length, type, label: hints[0] || "", hints: hints.join(" | ").slice(0, 300),
                   autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(), sensitive: sensitive(el, hints),
                   required: !!(el.required || el.getAttribute("aria-required") === "true")};
    if (type === "select") {
      field.options = Array.from(el.options).filter((o) => o.value !== "" || o.index > 0).map((o) => clean(o.text)).slice(0, 60);
      field.filled = el.selectedIndex > 0 && el.value !== "";
    } else if (type === "checkbox") {
      field.filled = el.checked; field.checked = el.checked;
    } else {
      field.filled = !!el.value;
    }
    tag(el, field.i); fields.push(field);
  }
  // Radio buttons are ONE field per group, its options the buttons' labels.
  const groups = new Map();
  for (const r of document.querySelectorAll("input[type=radio], [role=radio]")) {
    const vis = visible(r) || (r.closest("label") && visible(r.closest("label")));
    if (!vis) continue;
    const key = r.name || (r.closest("[role=radiogroup], fieldset") || r.parentElement);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  for (const [, radios] of groups) {
    const label = radioGroupLabel(radios[0]);
    const field = {i: fields.length, type: "radio", label, hints: label, autocomplete: "", sensitive: false,
                   options: radios.map((r) => clean(r.getAttribute("aria-label") || r.getAttribute("data-value") || optionLabel(r))),
                   filled: radios.some((r) => r.checked || r.getAttribute("aria-checked") === "true")};
    radios.forEach((r) => tag(r, field.i)); fields.push(field);
  }
  // ARIA checkboxes and Google Forms' drop-downs (role=listbox, options carry data-value).
  for (const el of document.querySelectorAll("[role=checkbox]:not(input), [role=switch]:not(input)")) {
    if (!visible(el)) continue;
    const label = clean(el.getAttribute("aria-label") || el.innerText || question(el));
    const on = el.getAttribute("aria-checked") === "true";
    tag(el, fields.length);
    fields.push({i: fields.length, type: "checkbox", label, hints: label, autocomplete: "", sensitive: false, filled: on, checked: on});
  }
  for (const el of document.querySelectorAll("[role=listbox], [role=combobox]:not(input)")) {
    if (!visible(el) || el.closest("[data-kayra-field]")) continue;
    const hints = hintsOf(el);
    const options = Array.from(el.querySelectorAll("[role=option]")).map((o) => clean(o.getAttribute("data-value") || o.innerText))
      .filter((o) => o && !/^choose$|^select$/i.test(o));
    const selected = el.querySelector("[role=option][aria-selected=true]");
    tag(el, fields.length);
    fields.push({i: fields.length, type: "listbox", label: hints[0] || "", hints: hints.join(" | ").slice(0, 300), autocomplete: "",
                 sensitive: false, options: options.slice(0, 60),
                 filled: !!(selected && clean(selected.getAttribute("data-value") || selected.innerText) && !/^choose$/i.test(clean(selected.innerText)))});
  }
  if (op === "list") return {url: location.href, title: document.title, fields: fields.slice(0, args.max || 60)};

  // ── acting on fields ──
  const byIndex = (i) => Array.from(document.querySelectorAll(`[data-kayra-field="${i}"]`));
  const rank = (want, have) => {
    const w = norm(want), h = norm(have);
    if (!w || !h) return 0;
    if (w === h) return 4;
    if (h.startsWith(w) || w.startsWith(h)) return 3;
    if (h.split(" ").includes(w)) return 2.5;
    const words = w.split(" ");
    if (words.every((x) => h.includes(x))) return 2;
    if (h.includes(w)) return 1.5;
    return 0;
  };
  const best = (want, items, nameOf) => {
    let top = null, score = 0;
    for (const it of items) { const s = rank(want, nameOf(it)); if (s > score) { top = it; score = s; } }
    return top;
  };
  const setValue = (el, value) => {
    el.focus();
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype :
      el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
    el.dispatchEvent(new Event("blur", {bubbles: true}));
  };
  const waitFor = async (find, ms) => {
    const end = Date.now() + (ms || 2000);
    while (Date.now() < end) { const x = find(); if (x) return x; await new Promise((r) => setTimeout(r, 80)); }
    return find();
  };
  const fillOne = async (field, value) => {
    const els = byIndex(field.i);
    if (!els.length) return "gone";
    if (field.sensitive || els.some((el) => sensitive(el, hintsOf(el)))) return "sensitive";
    const el = els[0];
    if (field.type === "select") {
      const opt = best(value, Array.from(el.options), (o) => o.text) || Array.from(el.options).find((o) => norm(o.value) === norm(value));
      if (!opt) return "no option";
      setValue(el, opt.value);
      return "";
    }
    if (field.type === "radio") {
      const r = best(value, els, (x) => x.getAttribute("aria-label") || x.getAttribute("data-value") || optionLabel(x));
      if (!r) return "no option";
      r.click();
      return (r.checked || r.getAttribute("aria-checked") === "true") ? "" : "not taken";
    }
    if (field.type === "checkbox") {
      const want = !!value && !/^(false|no|off|0)$/i.test(String(value));
      const on = () => el.checked === true || el.getAttribute("aria-checked") === "true";
      if (on() !== want) el.click();
      return on() === want ? "" : "not taken";
    }
    if (field.type === "listbox") {
      el.click();
      const opt = await waitFor(() => best(value, Array.from(document.querySelectorAll("[role=option]")).filter(visible),
        (o) => o.getAttribute("data-value") || o.innerText), 2000);
      if (!opt) { document.body.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true})); return "no option"; }
      opt.click();
      return "";
    }
    setValue(el, String(value));
    return norm(el.value) === norm(value) || el.type === "date" ? "" : "not taken";
  };

  if (op === "fill") {
    const filled = [], failed = [];
    for (const item of args.items || []) {
      const field = fields[item.i];
      if (!field) { failed.push({i: item.i, reason: "gone"}); continue; }
      const why = await fillOne(field, item.value);
      (why ? failed.push({i: item.i, label: field.label, reason: why}) : filled.push({i: item.i, label: field.label}));
    }
    return {filled, failed};
  }

  if (op === "choose") {
    // "select India in the country dropdown", "choose Male for gender", "tick I agree",
    // "untick subscribe me", "set the date of birth to 2003-05-04".
    const kinds = args.kind === "check" ? ["checkbox", "radio"] : args.kind === "date" ? ["date", "text", "datetime-local", "month"] :
      ["select", "listbox", "radio"];
    let pool = fields.filter((f) => kinds.includes(f.type) && !f.sensitive);
    if (args.kind === "check") {
      // A checkbox is named by its own label; a radio option by the option's label.
      const boxes = pool.filter((f) => f.type === "checkbox");
      const box = best(args.field, boxes, (f) => f.label);
      if (box) {
        const was = !!box.checked;
        const want = args.state === null || args.state === undefined ? !was : !!args.state;
        const why = await fillOne(box, want);
        return {done: !why, label: box.label, was, now: why ? was : want, reason: why || undefined};
      }
      const radio = pool.filter((f) => f.type === "radio").find((f) => best(args.field, f.options.map((o) => ({o})), (x) => x.o));
      if (radio && args.state !== false) {
        const opt = best(args.field, radio.options.map((o) => ({o})), (x) => x.o).o;
        const why = await fillOne(radio, opt);
        return {done: !why, label: opt, group: radio.label, now: true, reason: why || undefined};
      }
      return {done: false, reason: "not found", options: boxes.map((f) => f.label).filter(Boolean).slice(0, 12)};
    }
    if (args.field) pool = pool.filter((f) => rank(args.field, f.hints) > 0).sort((a, b) => rank(args.field, b.hints) - rank(args.field, a.hints));
    let field = pool[0];
    if (args.kind !== "date" && args.option) {
      // No field named, or the named one lacks the option: the field that HAS it.
      const having = pool.find((f) => (f.options || []).some((o) => rank(args.option, o) >= 2));
      if (having && (!field || !(field.options || []).some((o) => rank(args.option, o) >= 2))) field = having;
    }
    if (!field) return {done: false, reason: "field not found", fields: fields.filter((f) => kinds.includes(f.type)).map((f) => f.label).filter(Boolean).slice(0, 12)};
    const why = await fillOne(field, args.option);
    return {done: !why, field: field.label, option: args.option, reason: why || undefined, options: why ? (field.options || []).slice(0, 12) : undefined};
  }

  if (op === "submit") {
    const named = (el) => clean(el.getAttribute("aria-label") || el.innerText || el.value || el.textContent);
    const buttons = Array.from(document.querySelectorAll("button, input[type=submit], [role=button]")).filter(visible);
    const PAYS = /\b(pay|buy|order|checkout|check out|purchase|place order|add to cart|add to bag|add to basket)\b/i;
    const active = document.activeElement && document.activeElement.form;
    const form = active || Array.from(document.forms).find((f) => f.querySelector("[data-kayra-field]") && visible(f)) ||
      Array.from(document.forms).find(visible);
    let button = buttons.find((b) => /^(submit|send|next|continue|register|sign up|apply|save|done|finish)$/i.test(named(b)) && (!form || form.contains(b))) ||
      (form && buttons.find((b) => form.contains(b) && ((b.type || "").toLowerCase() === "submit" || (b.tagName === "BUTTON" && !b.getAttribute("type"))))) ||
      buttons.find((b) => /\bsubmit\b/i.test(named(b)));
    if (!button && !form) return {submitted: false, reason: "no form"};
    const label = button ? named(button) : "submit";
    if (PAYS.test(label)) return {submitted: false, reason: "payment", label};
    if (form && form.checkValidity && !form.checkValidity()) {
      // The browser would refuse to send it: say which fields are missing instead of "Submitted".
      const bad = Array.from(form.elements).filter((el) => el.willValidate && !el.checkValidity());
      return {submitted: false, reason: "invalid", label,
              missing: bad.map((el) => clean(hintsOf(el)[0] || el.name || "a field")).slice(0, 6)};
    }
    if (args.dry) return {submitted: false, dry: true, label, empty: fields.filter((f) => f.required && !f.filled).map((f) => f.label)};
    if (button) button.click();
    else if (form.requestSubmit) form.requestSubmit();
    return {submitted: true, label};
  }

  if (op === "file") {
    // D5: the file input (often hidden behind an "Upload" button). Arming installs ONE trusted-
    // key hook: the real Space key Kayra presses next gives the page a user activation, and
    // only inside that handler may input.click() open the file chooser.
    const inputs = Array.from(document.querySelectorAll("input[type=file]")).filter((el) => !el.disabled);
    if (!inputs.length) return {found: false};
    const nameOf = (el) => {
      const hints = hintsOf(el);
      const near = el.closest("label, [role=button], button, div");
      return clean([hints.join(" "), near && near !== el ? (near.innerText || "") : ""].join(" ")).slice(0, 120);
    };
    const input = (args.field && best(args.field, inputs, nameOf)) || inputs.find(visible) || inputs[0];
    document.querySelectorAll("input[data-kayra-file]").forEach((el) => el.removeAttribute("data-kayra-file"));
    input.setAttribute("data-kayra-file", "1");
    if (args.arm) {
      const until = Date.now() + 8000;
      const hook = (e) => {
        if (!e.isTrusted || e.key !== " " || Date.now() > until) return;
        e.preventDefault(); e.stopImmediatePropagation();
        window.removeEventListener("keydown", hook, true);
        input.click();
      };
      window.addEventListener("keydown", hook, true);
      setTimeout(() => window.removeEventListener("keydown", hook, true), 8000);
    }
    return {found: true, label: nameOf(input), accept: input.accept || "", multiple: !!input.multiple, count: inputs.length,
            armed: !!args.arm, files: Array.from(input.files || []).map((f) => f.name)};
  }
  if (op === "fileState") {
    const input = document.querySelector("input[data-kayra-file]");
    return {files: input ? Array.from(input.files || []).map((f) => f.name) : []};
  }
  return {error: "unknown op"};
}

// F1: the video on the page — speed, seek, captions, skip the ad, next, like, subscribe,
// watch later. The <video> element where the web allows it (speed and seeking work on any
// site); YouTube's own buttons for the rest, found by class and accessible name.
async function pageVideo(op, value) {
  const visible = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const area = (el) => { const r = el.getBoundingClientRect(); return r.width * r.height; };
  const v = Array.from(document.querySelectorAll("video")).filter(visible).sort((a, b) => area(b) - area(a))[0] ||
    document.querySelector("video");
  const yt = /(^|\.)youtube\.com$/.test(location.hostname);
  const first = (sel) => Array.from(document.querySelectorAll(sel)).find(visible) || null;
  const pressed = (b) => b && (b.getAttribute("aria-pressed") === "true");
  const ad = !!(yt && document.querySelector(".ad-showing, .ytp-ad-player-overlay, .ytp-ad-player-overlay-layout"));
  const title = () => {
    const h = yt && document.querySelector("ytd-watch-metadata h1, h1.ytd-watch-metadata, #title h1");
    return ((h && h.textContent) || document.title || "").replace(/\s+/g, " ").replace(/ - YouTube$/, "").trim();
  };
  const state = () => v ? {video: true, paused: v.paused, time: Math.round(v.currentTime || 0),
    duration: isFinite(v.duration) ? Math.round(v.duration) : 0, rate: v.playbackRate, muted: v.muted, ad, title: title(),
    captions: yt ? pressed(first(".ytp-subtitles-button")) : Array.from(v.textTracks || []).some((t) => t.mode === "showing")}
    : {video: false, title: title()};
  const wait = async (find, ms) => {
    const end = Date.now() + (ms || 2500);
    while (Date.now() < end) { const x = find(); if (x) return x; await new Promise((r) => setTimeout(r, 100)); }
    return find();
  };
  if (op === "state") return state();
  if (!v && !["like", "subscribe", "watchLater", "next"].includes(op)) return {video: false};
  switch (op) {
    case "play": { try { await v.play(); } catch (e) { return {...state(), blocked: true}; } return state(); }
    case "pause": v.pause(); return state();
    case "rate": v.playbackRate = Math.min(4, Math.max(0.25, Number(value) || 1)); return state();
    case "seek": v.currentTime = Math.min(Math.max(0, (v.currentTime || 0) + Number(value || 0)), isFinite(v.duration) ? v.duration - 0.5 : 1e9); return state();
    case "seekTo": v.currentTime = Math.min(Math.max(0, Number(value || 0)), isFinite(v.duration) ? v.duration - 0.5 : 1e9); return state();
    case "mute": v.muted = !!value; return state();
    case "blur": if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); return state();
    case "skipAd": {
      const skip = first(".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, button[id^='skip-button'], .ytp-ad-skip-button-container button");
      if (skip) { skip.click(); return {skipped: true, ad: true}; }
      const left = (first(".ytp-preview-ad, .ytp-ad-preview-container, .ytp-preview-ad__text") || {}).textContent || "";
      return {skipped: false, ad, wait: left.replace(/\s+/g, " ").trim()};
    }
    case "captions": {
      if (yt) {
        const b = first(".ytp-subtitles-button");
        if (!b) return {available: false};
        if (pressed(b) !== !!value) b.click();
        return {available: true, captions: pressed(b)};
      }
      const tracks = Array.from(v.textTracks || []).filter((t) => /captions|subtitles/.test(t.kind));
      if (!tracks.length) return {available: false};
      tracks.forEach((t, i) => { t.mode = value && i === 0 ? "showing" : "hidden"; });
      return {available: true, captions: !!value};
    }
    case "next": {
      const b = yt && first(".ytp-next-button");
      if (!b) return {next: false};
      b.click(); return {next: true};
    }
    case "like": {
      const b = yt && first("like-button-view-model button, #segmented-like-button button, ytd-toggle-button-renderer#like-button button, button[aria-label^='like this video' i]");
      if (!b) return {available: false};
      const was = pressed(b);
      if (was !== !!value) b.click();
      return {available: true, was, liked: pressed(b) || (!was && !!value)};
    }
    case "subscribe": {
      const b = yt && first("#subscribe-button button, ytd-subscribe-button-renderer button, yt-subscribe-button-view-model button");
      if (!b) return {available: false};
      const subscribed = /subscribed/i.test((b.getAttribute("aria-label") || "") + " " + (b.textContent || "")) &&
        !/^subscribe to/i.test(b.getAttribute("aria-label") || "");
      if (subscribed) return {available: true, already: true, subscribed: true};
      b.click();
      return {available: true, subscribed: true};
    }
    case "watchLater": {
      if (!yt) return {available: false};
      const save = first("button[aria-label='Save to playlist'], button[aria-label^='Save' i]") ||
        Array.from(document.querySelectorAll("ytd-watch-metadata button, #actions button")).filter(visible)
          .find((b) => /^save$/i.test((b.textContent || "").trim()));
      if (!save) return {available: false};
      save.click();
      const box = await wait(() => Array.from(document.querySelectorAll("[role=checkbox], tp-yt-paper-checkbox, toggleable-list-item-view-model, yt-list-item-view-model"))
        .filter(visible).find((el) => /watch later/i.test(el.getAttribute("aria-label") || el.textContent || "")), 3000);
      if (!box) return {available: false, opened: true};
      const on = () => box.getAttribute("aria-checked") === "true" || box.getAttribute("aria-pressed") === "true" || box.hasAttribute("checked");
      const already = on();
      if (!already) box.click();
      document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", keyCode: 27, bubbles: true}));
      return {available: true, already, saved: true};
    }
  }
  return {error: "unknown op"};
}

// F3: the product on a shop page — name, price, rating — from the page's own structured data
// (schema.org Product), then Amazon's and Flipkart's markup, then the Open Graph tags.
function pageProduct() {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const out = {found: false};
  const take = (node) => {
    if (!node || typeof node !== "object") return false;
    if (Array.isArray(node)) return node.some(take);
    if (node["@graph"]) return take(node["@graph"]);
    const type = [].concat(node["@type"] || []).join(" ");
    if (!/\bProduct\b/.test(type)) return false;
    out.name = clean(node.name);
    const offer = [].concat(node.offers || [])[0] || {};
    out.price = String(offer.price || offer.lowPrice || (offer.priceSpecification || {}).price || "");
    out.currency = offer.priceCurrency || "";
    out.availability = String(offer.availability || "").replace(/^https?:\/\/schema\.org\//, "");
    const rating = node.aggregateRating || {};
    out.rating = String(rating.ratingValue || "");
    out.ratings = String(rating.reviewCount || rating.ratingCount || "");
    out.brand = clean((node.brand && (node.brand.name || node.brand)) || "");
    out.found = !!out.name;
    return out.found;
  };
  for (const s of document.querySelectorAll("script[type='application/ld+json']")) {
    try { if (take(JSON.parse(s.textContent))) break; } catch (e) {}
  }
  const q = (sel) => { const el = document.querySelector(sel); return el ? clean(el.getAttribute("content") || el.textContent) : ""; };
  const host = location.hostname;
  if (/(^|\.)amazon\./.test(host)) {
    out.name = out.name || q("#productTitle");
    out.price = out.price || q("#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #apex_desktop .a-price .a-offscreen, .a-price .a-offscreen");
    out.rating = out.rating || ((document.querySelector("#acrPopover") || {}).title || "").replace(/ out of.*/, "");
    out.ratings = out.ratings || q("#acrCustomerReviewText").replace(/ ratings?/i, "");
    out.availability = out.availability || q("#availability span");
  } else if (/flipkart\.com$/.test(host)) {
    out.name = out.name || q("h1 span, h1");
    out.price = out.price || q("div.Nx9bqj.CxhGGd, div.Nx9bqj, div._30jeq3");
    out.rating = out.rating || q("div.XQDdHH, div._3LWZlK");
    out.ratings = out.ratings || q("span.Wphh3N span span, span._2_R_DZ span span").replace(/ ratings?.*/i, "");
  }
  out.name = out.name || q("meta[property='og:title']");
  out.price = out.price || q("meta[property='product:price:amount'], meta[itemprop=price], [itemprop=price]");
  out.currency = out.currency || q("meta[property='product:price:currency'], meta[itemprop=priceCurrency]");
  out.found = !!(out.name && (out.price || out.rating));
  out.url = location.href;
  return out;
}

// 1.13.0 (F4): ChatGPT, Claude and Gemini in a tab. ONE function for the three, by role
// first: the prompt box is the visible editor whose name says prompt / message / ask, the
// answers are the assistant turns, and "still writing" is a visible Stop button (or Claude's
// data-is-streaming). ops: state | ask {text} | wait {after, waitMs, settleMs} | read.
// `wait` waits INSIDE the page until there is a new answer that has stopped growing.
async function pageAiChat(op, args) {
  args = args || {};
  const host = location.hostname;
  const site = /chatgpt\.com$|chat\.openai\.com$/.test(host) ? "chatgpt" : /claude\.ai$/.test(host) ? "claude"
    : /gemini\.google\.com$/.test(host) ? "gemini" : "";
  const clean = (s) => String(s || "").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  const visible = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none"; };
  const nameOf = (el) => [el.getAttribute("aria-label"), el.getAttribute("placeholder"), el.getAttribute("data-placeholder"),
    el.getAttribute("aria-placeholder"), el.getAttribute("title"), el.getAttribute("data-testid"), el.id].filter(Boolean).join(" ").toLowerCase();
  const SITE_BOX = {chatgpt: "#prompt-textarea, [data-testid=prompt-textarea]",
                    claude: "div.ProseMirror[contenteditable=true], [aria-label*='prompt' i][contenteditable=true]",
                    gemini: "rich-textarea [contenteditable=true], .ql-editor[contenteditable=true]"};
  const composer = () => {
    const own = SITE_BOX[site] ? Array.from(document.querySelectorAll(SITE_BOX[site])).filter(visible) : [];
    if (own.length) return own[own.length - 1];
    const all = Array.from(document.querySelectorAll("textarea, [contenteditable=true], [contenteditable=''], [role=textbox]"))
      .filter(visible).filter((el) => !el.closest("nav, aside, header"));
    return all.find((el) => /prompt|message|ask|chat|reply|talk|anything/.test(nameOf(el))) || all[all.length - 1] || null;
  };
  const SITE_ANSWER = {chatgpt: "[data-message-author-role=assistant]",
                       claude: "[data-is-streaming], .font-claude-response, [data-testid=assistant-message]",
                       gemini: "model-response, message-content, [data-response-index]"};
  const answers = () => {
    const sel = SITE_ANSWER[site] || "[data-message-author-role=assistant], [data-role=assistant], .assistant-message";
    return Array.from(document.querySelectorAll(sel)).filter((el) => !el.parentElement.closest(sel));
  };
  const textOf = (el) => {
    const copy = el.cloneNode(true);
    copy.querySelectorAll("pre").forEach((p) => p.replaceWith(document.createTextNode("\n[code on screen]\n")));
    copy.querySelectorAll("button, [role=button], svg, style, script, .sr-only").forEach((b) => b.remove());
    return clean(copy.innerText || copy.textContent).slice(0, 12000);
  };
  const stopButton = () => Array.from(document.querySelectorAll("button, [role=button]")).filter(visible)
    .find((b) => /^stop\b|stop (generating|streaming|response)/.test(nameOf(b)) || b.getAttribute("data-testid") === "stop-button");
  const busy = () => !!stopButton() || !!document.querySelector("[data-is-streaming=true]");
  const signedOut = () => !composer() && Array.from(document.querySelectorAll("a, button")).filter(visible)
    .some((b) => /^(log ?in|sign ?in)$/i.test((b.innerText || b.getAttribute("aria-label") || "").trim()));
  const state = () => {
    const list = answers(); const box = composer();
    return {site, ready: !!box, signedOut: signedOut(), count: list.length, busy: busy(),
            text: box ? clean(box.isContentEditable || !("value" in box) ? box.innerText : box.value) : "",
            last: list.length ? textOf(list[list.length - 1]) : "", title: document.title};
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (op === "state") return state();
  if (op === "read") { const s = state(); return {...s, found: s.count > 0}; }
  if (op === "ask") {
    const box = composer();
    if (!box) return {...state(), sent: false, reason: signedOut() ? "signed out" : "no prompt box"};
    const before = answers().length;
    const want = clean(args.text || "");
    box.focus();
    const editable = box.isContentEditable || !("value" in box);
    if (editable) {
      const range = document.createRange(); range.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      document.execCommand("insertText", false, want);
    } else {
      const proto = box.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(box, want);
      box.dispatchEvent(new Event("input", {bubbles: true}));
    }
    const typed = () => clean(editable ? box.innerText : box.value) === want;
    if (!typed()) return {...state(), sent: false, reason: "could not type"};
    // The send button turns on a moment after the text arrives: wait for it, never press a
    // disabled one; Enter in the box is the fallback every one of these sites accepts.
    let button = null;
    for (let i = 0; i < 20 && !button; i++) {
      button = Array.from(document.querySelectorAll("button, [role=button]")).filter(visible)
        .find((b) => (b.getAttribute("data-testid") === "send-button" || /^send( prompt| message)?$|^submit$/.test(nameOf(b).trim()))
                     && !b.disabled && b.getAttribute("aria-disabled") !== "true");
      if (!button) await sleep(100);
    }
    if (button) button.click();
    else {
      const enter = {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true};
      box.dispatchEvent(new KeyboardEvent("keydown", enter));
      box.dispatchEvent(new KeyboardEvent("keyup", enter));
    }
    // Sent = the box emptied or the reply started.
    let sent = false;
    for (let i = 0; i < 30 && !sent; i++) {
      await sleep(100);
      sent = !typed() || busy() || answers().length > before;
    }
    return {...state(), sent, before, pressed: button ? "button" : "enter"};
  }
  if (op === "wait") {
    const after = Number(args.after || 0);
    const limit = Math.min(Math.max(Number(args.waitMs || 15000), 500), 25000);
    const settle = Math.min(Math.max(Number(args.settleMs || 1500), 300), 5000);
    const start = Date.now(); let last = null, since = start;
    while (Date.now() - start < limit) {
      const list = answers();
      const text = list.length > after ? textOf(list[list.length - 1]) : "";
      if (text !== last) { last = text; since = Date.now(); }
      if (text && !busy() && Date.now() - since >= settle) return {...state(), done: true};
      await sleep(200);
    }
    return {...state(), done: false};
  }
  return {error: "unknown op"};
}

// 1.13.0 (F5): a file into the OPEN WhatsApp chat. `arm` opens the Attach menu when its file
// inputs are not there yet, picks the input for the kind (photos / videos, else document) and
// installs the same one-shot TRUSTED-Space hook as page.form: the real key Kayra presses is the
// user gesture that lets input.click() open the chooser. `state` reads the preview WhatsApp
// shows before sending; `send` presses the preview's own Send.
async function pageChatAttach(op, args) {
  args = args || {};
  const visible = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const nameOf = (el) => [el.getAttribute("aria-label"), el.getAttribute("title"), el.getAttribute("data-icon"),
    (el.querySelector("[data-icon]") || {getAttribute: () => ""}).getAttribute("data-icon")].filter(Boolean).join(" ").toLowerCase();
  const head = document.querySelector("#main header span[title], #main header [dir=auto]");
  const chat = head ? (head.getAttribute("title") || head.textContent).trim() : "";
  const preview = () => {
    // The preview covers the conversation: a Send button that is NOT the composer's, plus the file names.
    const sends = Array.from(document.querySelectorAll("[role=button], button, span[data-icon]"))
      .filter(visible).filter((b) => /^send$|wds-ic-send|^send\b/.test(nameOf(b).trim()) && !b.closest("footer"));
    const names = Array.from(document.querySelectorAll("[data-kayra-preview], [title]"))
      .filter(visible).filter((e) => !e.closest("#pane-side, #side, header, footer"))
      .map((e) => (e.getAttribute("title") || "").trim()).filter((t) => /\.[a-z0-9]{2,5}$/i.test(t));
    return {open: sends.length > 0, files: Array.from(new Set(names)), send: sends[0] || null};
  };
  if (op === "state") { const p = preview(); return {chat, preview: p.open, files: p.files}; }
  if (op === "send") {
    const p = preview();
    if (!p.send) return {chat, sent: false, reason: "no preview"};
    const target = p.send.closest("[role=button], button") || p.send;
    target.click();
    for (let i = 0; i < 30 && preview().open; i++) await new Promise((r) => setTimeout(r, 100));
    return {chat, sent: !preview().open};
  }
  if (op !== "arm") return {error: "unknown op"};
  if (!document.querySelector("#main")) return {found: false, chat: "", reason: "no chat open"};
  const inputs = () => Array.from(document.querySelectorAll("#main input[type=file], #app input[type=file], body input[type=file]"))
    .filter((el) => !el.disabled);
  if (!inputs().length) {
    const attach = Array.from(document.querySelectorAll("#main [role=button], #main button, footer [role=button], footer button"))
      .filter(visible).find((b) => /^attach\b|attach|plus|clip/.test(nameOf(b)));
    if (!attach) return {found: false, chat, reason: "no attach button"};
    attach.click();
    for (let i = 0; i < 20 && !inputs().length; i++) await new Promise((r) => setTimeout(r, 100));
  }
  const list = inputs();
  if (!list.length) return {found: false, chat, reason: "no file input"};
  const media = (el) => /image|video/.test(el.accept || "");
  const labelOf = (el) => ((el.closest("li, [role=button], [role=menuitem], div") || el).innerText || "").trim().toLowerCase();
  const input = args.kind === "media" ? (list.find(media) || list[0])
    : (list.find((el) => /document/.test(labelOf(el))) || list.find((el) => !media(el)) || list[0]);
  const until = Date.now() + 8000;
  const hook = (e) => {
    if (!e.isTrusted || e.key !== " " || Date.now() > until) return;
    e.preventDefault(); e.stopImmediatePropagation();
    window.removeEventListener("keydown", hook, true);
    input.click();
  };
  window.addEventListener("keydown", hook, true);
  setTimeout(() => window.removeEventListener("keydown", hook, true), 8000);
  return {found: true, armed: true, chat, kind: media(input) ? "media" : "document", accept: input.accept || ""};
}

// 1.13.0 (F6): the list on a Reddit / LinkedIn / GitHub page, as {title, meta, url}. Reddit's
// posts are custom elements that carry their facts as attributes; LinkedIn and GitHub are read
// by landmark and role with a generic fallback (articles / list items in <main>).
function pageSocial(kind, max) {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const host = location.hostname;
  const items = [];
  const push = (title, meta, url) => {
    title = clean(title).slice(0, 220);
    if (!title || items.some((i) => i.title === title)) return;
    items.push({title, meta: clean(meta).slice(0, 160), url: url || ""});
  };
  if (/(^|\.)reddit\.com$/.test(host)) {
    for (const p of document.querySelectorAll("shreddit-post")) {
      if (p.hasAttribute("promoted") || p.getAttribute("post-type") === "ad") continue;
      const score = p.getAttribute("score"); const comments = p.getAttribute("comment-count");
      push(p.getAttribute("post-title"), [p.getAttribute("subreddit-prefixed-name"),
        score ? `${score} points` : "", comments ? `${comments} comments` : ""].filter(Boolean).join(", "),
        p.getAttribute("permalink") ? new URL(p.getAttribute("permalink"), location.origin || ("https://" + host)).href : "");
      if (items.length >= max) break;
    }
    if (!items.length) {
      for (const a of document.querySelectorAll(".thing:not(.promoted) a.title, article h3, a[slot=title]")) {
        push(a.innerText, "", a.href || ""); if (items.length >= max) break;
      }
    }
  } else if (/github\.com$/.test(host) && kind === "notifications") {
    for (const row of document.querySelectorAll(".notifications-list-item, [data-notification-id]")) {
      const t = row.querySelector(".markdown-title, a[href*='/notifications/'], a");
      const repo = row.querySelector(".f6, .text-small, [data-testid=repository-name]");
      push(t ? t.innerText : row.innerText.split("\n")[0], repo ? repo.innerText : "", t && t.href);
      if (items.length >= max) break;
    }
  } else if (/linkedin\.com$/.test(host)) {
    const sel = kind === "messages" ? ".msg-conversation-listitem, [data-view-name*='conversation'], li[class*='conversation']"
      : kind === "notifications" ? "article, .nt-card, [data-view-name*='notification'], section li"
      : "[data-urn*='activity'], .feed-shared-update-v2, [data-id*='activity'], main article";
    for (const el of document.querySelectorAll(sel)) {
      if (!visible(el) && items.length) continue;
      const lines = (el.innerText || "").split("\n").map(clean).filter(Boolean);
      if (!lines.length || /^(promoted|sponsored)$/i.test(lines[0])) continue;
      if (kind === "messages") push(lines[0], lines.slice(1).find((l) => l.length > 3 && !/^\d{1,2}:\d{2}|^[A-Z][a-z]{2} \d/.test(l)) || "", "");
      else { const body = lines.find((l) => l.length > 25) || lines[0]; push(body, body !== lines[0] ? lines[0] : "", ""); }
      if (items.length >= max) break;
    }
  }
  if (!items.length) {
    const main = document.querySelector("main, [role=main]") || document.body;
    for (const el of main.querySelectorAll("article, [role=article], li")) {
      if (!visible(el) || el.closest("nav, header, footer, aside")) continue;
      const first = (el.innerText || "").split("\n").map(clean).find((l) => l.length > 15);
      const link = el.querySelector("a[href]");
      if (first) push(first, "", link ? link.href : "");
      if (items.length >= max) break;
    }
  }
  return {site: host, kind, items, title: document.title, url: location.href};
}

// 1.14.0 (H1): what the page agent SEES. Every visible thing a person can act on, with a stable
// id (`data-kayra-id`, kept across snapshots so "click 7" still means the same element), its role,
// the name a screen reader announces, its value, and whether it is a secret field. Secret values
// are never returned; codes near "OTP / code / PIN" in the text are masked. Plus a little of the
// page's own text (headings first) so the model knows where it is.
function pageSnapshot(max, textMax) {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none"; };
  const SEL = "a[href],button,input,textarea,select,summary,[contenteditable=true],[contenteditable=''],[role=button],[role=link]," +
    "[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=textbox],[role=combobox]," +
    "[role=searchbox],[role=listitem][tabindex],[role=row][tabindex],[role=gridcell][tabindex]";
  const textOf = (id) => { const e = id && document.getElementById(id); return e ? e.innerText || e.textContent : ""; };
  const nameOf = (el) => {
    const lb = el.getAttribute("aria-labelledby");
    let n = el.getAttribute("aria-label") || (lb && lb.split(/\s+/).map(textOf).join(" ")) || "";
    if (!n && el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) n = l.innerText; }
    if (!n) { const wrap = el.closest("label"); if (wrap && wrap !== el) n = wrap.innerText; }
    n = n || el.getAttribute("title") || el.getAttribute("placeholder") || el.getAttribute("data-placeholder") ||
      el.getAttribute("alt") || (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) ? "" : el.innerText) || el.value || el.name || "";
    return clean(n).slice(0, 80);
  };
  const roleOf = (el) => el.getAttribute("role") || ({A: "link", BUTTON: "button", SELECT: "select", TEXTAREA: "textbox", SUMMARY: "button"}[el.tagName]
    || (el.tagName === "INPUT" ? ({checkbox: "checkbox", radio: "radio", submit: "button", button: "button", search: "searchbox"}[(el.type || "").toLowerCase()] || "textbox") : "")
    || (el.isContentEditable ? "textbox" : "element"));
  const SECRET = /pass ?(word|code|phrase)|\bcvv\b|\bcvc\b|card ?(number|no)|\botp\b|one.time|\bpin\b|security code|aadhaa?r/i;
  window.__kayraNext = window.__kayraNext || 1;
  const items = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (!visible(el) || el.closest("[aria-hidden=true]")) continue;
    if (el.parentElement && el.parentElement.closest("a[href],button,[role=button],[role=link]") && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) continue;
    let id = el.getAttribute("data-kayra-id");
    if (!id) { id = String(window.__kayraNext++); el.setAttribute("data-kayra-id", id); }
    const name = nameOf(el);
    const sensitive = (el.type || "").toLowerCase() === "password" || /^cc-|one-time-code|password/.test(el.getAttribute("autocomplete") || "") || SECRET.test(name);
    const value = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) ? (el.type === "checkbox" || el.type === "radio" ? "" : el.value)
      : el.isContentEditable ? el.innerText : "";
    const item = {id: Number(id), role: roleOf(el), name};
    if (value) item.value = sensitive ? "(hidden)" : clean(value).slice(0, 60);
    if (sensitive) item.sensitive = true;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") item.disabled = true;
    const checked = el.checked !== undefined && (el.type === "checkbox" || el.type === "radio") ? el.checked : el.getAttribute("aria-checked");
    if (checked === true || checked === "true") item.checked = true;
    if (el.tagName === "SELECT") item.options = Array.from(el.options).map((o) => clean(o.text)).slice(0, 12);
    if (!name && !item.value && item.role !== "textbox") continue;
    items.push(item);
    if (items.length >= max) break;
  }
  const heads = Array.from(document.querySelectorAll("h1,h2,h3,[role=heading]")).filter(visible).map((h) => clean(h.innerText)).filter(Boolean).slice(0, 8);
  const main = document.querySelector("main,[role=main]") || document.body;
  let text = clean([heads.join(" | "), main.innerText].join("\n")).slice(0, textMax);
  text = text.replace(/((?:otp|code|pin|password)[^.\n]{0,24}?)\b\d{4,8}\b/gi, "$1••••");
  return {url: location.href, title: document.title, items, text};
}

// 1.14.0 (H1): the page agent's hands — ONE action on the element with that id. Secret fields
// are refused here as well as in Kayra. ops: click | type {text, clear} | select {text} | check
// {state} | press {key} | scroll {direction}.
async function pageAct(op, args) {
  args = args || {};
  const el = args.id !== undefined && args.id !== null ? document.querySelector(`[data-kayra-id="${Number(args.id)}"]`) : null;
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  if (op === "scroll") {
    const amount = Math.round(window.innerHeight * 0.8) * (args.direction === "up" ? -1 : 1);
    const before = window.scrollY; window.scrollBy(0, amount);
    if (window.scrollY === before) {
      const box = Array.from(document.querySelectorAll("*")).find((b) => b.scrollHeight > b.clientHeight + 40 &&
        /(auto|scroll)/.test(getComputedStyle(b).overflowY) && b.clientHeight > 150);
      if (box) { const t = box.scrollTop; box.scrollTop += amount; return {ok: box.scrollTop !== t}; }
    }
    return {ok: window.scrollY !== before};
  }
  if (op === "press") {
    const target = el || document.activeElement || document.body;
    const key = String(args.key || "Enter");
    const code = {Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38}[key] || 0;
    for (const type of ["keydown", "keyup"]) target.dispatchEvent(new KeyboardEvent(type, {key, code: key, keyCode: code, which: code, bubbles: true, cancelable: true}));
    if (key === "Enter" && target.form && target.form.requestSubmit && target.tagName === "INPUT") { try { target.form.requestSubmit(); } catch (e) {} }
    return {ok: true, key};
  }
  if (!el) return {ok: false, reason: "no such element"};
  const name = clean(el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("placeholder") || "").slice(0, 80);
  const secret = (el.type || "").toLowerCase() === "password" || /^cc-|one-time-code|password/.test(el.getAttribute("autocomplete") || "") ||
    /pass ?(word|code)|\bcvv\b|card ?number|\botp\b|\bpin\b/i.test(name + " " + (el.getAttribute("placeholder") || ""));
  el.scrollIntoView({block: "center"});
  if (op === "click") {
    const r = el.getBoundingClientRect();
    const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) el.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, opts));
    el.click();
    return {ok: true, name};
  }
  if (op === "type") {
    if (secret) return {ok: false, reason: "secret field", name};
    el.focus();
    if (el.isContentEditable || !("value" in el)) {
      if (args.clear !== false) { const range = document.createRange(); range.selectNodeContents(el); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range); }
      document.execCommand("insertText", false, String(args.text || ""));
    } else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, (args.clear === false ? el.value : "") + String(args.text || ""));
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
    }
    const now = el.isContentEditable || !("value" in el) ? el.innerText : el.value;
    return {ok: clean(now).includes(clean(args.text || "")), name};
  }
  if (op === "select") {
    if (el.tagName !== "SELECT") return {ok: false, reason: "not a drop-down", name};
    const want = clean(args.text).toLowerCase();
    const opt = Array.from(el.options).find((o) => clean(o.text).toLowerCase() === want) || Array.from(el.options).find((o) => clean(o.text).toLowerCase().includes(want));
    if (!opt) return {ok: false, reason: "no option", options: Array.from(el.options).map((o) => clean(o.text)).slice(0, 12), name};
    el.value = opt.value; el.dispatchEvent(new Event("change", {bubbles: true}));
    return {ok: true, name, option: clean(opt.text)};
  }
  if (op === "check") {
    const now = el.checked !== undefined ? el.checked : el.getAttribute("aria-checked") === "true";
    if (now !== (args.state !== false)) el.click();
    return {ok: true, name, checked: el.checked !== undefined ? el.checked : el.getAttribute("aria-checked") === "true"};
  }
  return {ok: false, reason: "unknown op"};
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
