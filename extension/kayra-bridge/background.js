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
          audible: !!t.audible, muted: !!(t.mutedInfo && t.mutedInfo.muted), pinned: t.pinned};
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
      if (args.exceptActive) {
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
    case "tabs.reload": { const t = await targetTab(args); await chrome.tabs.reload(t.id); return tabInfo(t); }
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
    case "tabs.restore": { const s = await chrome.sessions.restore(); return {restored: !!s}; }

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
