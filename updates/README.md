# Updates

The version history of Kayra's major changes. One detailed report per update, numbered in
order. Newest last.

| Version | Date | Title | Type |
|---|---|---|---|
| [V1](UPDATE_V1.md) | 2026-09-10 (recorded 2026-11-09) | Baseline: the repository as cloned (commit `929371b`) | Baseline snapshot |
| [V2](UPDATE_V2.md) | 2026-11-09 | Cross-platform architecture, Linux/Omarchy support, dependency modernization | Major feature, reliability, dependencies, docs |
| [V3](UPDATE_V3.md) | 2026-11-10 | Tuning pass: fast speech model, fewer model calls, readiness waits, short CLAUDE.md | Performance, maintenance |
| [V4](UPDATE_V4.md) | 2026-11-10 | Claude-first cloud models, keys in the keyring, redesigned GPU-composited UI, browser tabs (Part 2a/2b) | Major feature, fixes, security, performance |
| [V5](UPDATE_V5.md) | 2026-11-10 | Meetings and screen sharing: four misroutes fixed, share the named window/monitor/tab (Part 2M) | Bug fixes, feature, test hygiene |
| [V6](UPDATE_V6.md) | 2026-11-10 | Tabs and windows finished: close by site, tab groups, windows, the Nth result, find with highlight (Part 2c) | Feature |
| [V7](UPDATE_V7.md) | 2026-11-10 | About the page, comments, video summaries, translate, bookmarks, history, downloads, "it", search here (Part 3) | Feature, bug fix |
| [V8](UPDATE_V8.md) | 2026-11-10 | Google Workspace by voice: Sheets, Docs, Slides, Drive, Gmail, Calendar, Meet, Keep, Forms, Classroom (Part 4) | Feature |
| [V9](UPDATE_V9.md) | 2026-11-10 | Tuning pass: rules before the classifier, waits inside the browser, relevant text, spoken names, site lookups fixed on live sites | Performance, accuracy, fixes |
| [V10](UPDATE_V10.md) | 2026-11-10 | WhatsApp by voice, the app in front, and a semantic name matcher (Part 5) | Feature, privacy, fixes |
| [V11](UPDATE_V11.md) | 2026-11-11 | Fix round: scrolling inside web apps, one search action + "close the search", WhatsApp chats by loose name from anywhere, file names said the short way | Fixes, tuning |
| [V12](UPDATE_V12.md) | 2026-11-11 | Forms with your saved details, drop-downs, ticks, dates, submit, file upload; video controls; search any site, sort and filter shops, product facts; shopping stops before the cart (Part 6a) | Feature, privacy |
| [V13](UPDATE_V13.md) | 2026-11-11 | Ask ChatGPT / Claude / Gemini, WhatsApp replies and files, Reddit / LinkedIn / GitHub, several steps in one sentence (Part 6b) | Feature, safety |
| [V14](UPDATE_V14.md) | 2026-11-11 | Open Chrome straight into a profile, read from Chrome itself: by name, account, email or domain (Part 5P) | Feature, privacy |
| [V15](UPDATE_V15.md) | 2026-11-11 | The page agent (local model, asks before acting for you), recipes replayed without a model, learning what your names mean; §7a model benchmark (Part 7) | Feature, safety, measurement |
| [V16](UPDATE_V16.md) | 2026-11-11 | WhatsApp mute / pin / react / reply / forward / call, in-call Meet controls and a share warning, one inbox across apps (Parts 8–10) | Feature, safety, privacy |

## Adding an update

For every major feature, security fix, dependency change or breaking change, add
`UPDATE_V<n>.md` (next number) and a row above. Each report covers:

1. Header table: version, base version/commit, date, type, scope, what was out of scope.
2. Summary and a before/after table.
3. Changes by area — what, where, and **why**.
4. Bug and security fixes: symptom, effect, fix.
5. Dependency and configuration changes.
6. Files created / modified / deleted, with reasons.
7. Verification: what was run, on which OS and Python, with results.
8. Known limitations and what was **not** verified.
9. Upgrade notes for an existing install.
