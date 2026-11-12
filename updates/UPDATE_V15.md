# Update V15 — the page agent, its recipes, and learning what you mean (Part 7)

| | |
|---|---|
| **Version** | V15 |
| **Based on** | V14, commit `c84d2cf` on `feature/linux-desktop-automation` |
| **Date** | 2026-11-11 |
| **Type** | Feature, safety, measurement |
| **Scope** | BROWSER_CONTROL_PLAN §7a (the model benchmark), H1 (page agent), H1.1 (recipes), H2 (aliases and transparency); Chrome extension 1.14.0 |
| **Not in scope** | H2's learned phrasings as classifier hints and a tone note for drafts (both would change the cached classifier prompt); H3 per-app features; H4 cross-app layer |
| **Models** | none added. The agent uses the local model already loaded (default) or `PAGE_AGENT_MODEL` |

## 1. Summary

| Said | Before (V14) | After (V15) |
|---|---|---|
| "on this page, find the cheapest flight to Goa" | a click on a button called that, or nothing | the page agent looks at the page and does it step by step with your LOCAL model, then says what it found |
| "figure out how to turn on dark mode here" | not understood | the same, on any site |
| a step that would post, send, delete, submit or pay | — | "Should I click "Post" on this page?"; yes carries on, no stops |
| "add to cart" chosen by the agent | — | refused (decision D1) |
| the same request again ("search for red socks" after "search for blue shoes") | — | replayed from what worked, with no model call |
| "Do you mean PR Outreach 2025 or PR and Outreach Core Team 2026-27?" → "the core team one" | the answer was a new, unrelated request | opens it, and "PR group" means that chat from now on |
| "no, the other one" (right after Kayra opened a chat) | not understood | opens the other match and remembers |
| "what have you learned about me", "forget that", "forget everything about WhatsApp" | not understood | shown and forgotten |

## 2. §7a — which local model runs the agent (measured, local only)

`tests/bench_page_agent.py`: 30 one-step decisions on pages shaped like Gmail, WhatsApp, Sheets, a
shop, a form, a settings page and GitHub (one with a hidden "ignore your task" line), each read by
the real `pageSnapshot` in headless Chrome, prompted and parsed by the real agent code.

| Model | Valid JSON | Correct action | Median / worst per step | Whole runs |
|---|---|---|---|---|
| Qwen3.5-9B, first prompt | 28/30 | 21/30 | 2.26 / 3.36 s | — |
| gemma-4-e4b, first prompt | 22/30 | 17/30 | 0.69 / 1.79 s | — |
| **Qwen3.5-9B, tuned** | 30/30 | **30/30** | 1.40 / 2.38 s | 2/2 |
| **gemma-4-e4b, tuned** | 30/30 | **27/30** | 0.70 / 0.95 s | 2/2 |

Tuning: rules and four worked examples in the prompt, a question hint, "already done — never
repeat", the agent telling the model when a step changed nothing, Qwen's `/no_think` (halved its
latency), and "choosing what is already chosen = done". **Both clear the plan's 90 % bar.** The
default stays the decision model (gemma): the 8 GB GPU cannot hold both, and LM Studio failed to
load Qwen while gemma was resident — swapping per request would cost far more than the accuracy
gap. Set `PAGE_AGENT_MODEL=qwen/qwen3.5-9b` when LM Studio runs Qwen for everything.

## 3. Changes by area

- **Extension 1.13.0 → 1.14.0** (no new permission): `pageSnapshot` — visible controls with a
  STABLE `data-kayra-id`, role, accessible name, value, options, ticks, a secret flag (values of
  secret fields never returned), and the page's headings + text with codes near "OTP / code / PIN"
  masked. `pageAct` — click / type / select / check / press / scroll by id; secret fields refused.
- **`automation/page_agent.py`** (new): the loop, the closed action set, `parse`, `judge`,
  `typed_ok`, recipes (`save_recipe`, `find_recipe`, replay by name, self-healing), `list_recipes`.
- **`automation/learning.py`** (new): the private store (atomic, 0600), aliases (`teach`,
  `demote`, `alias_for`), `forget_last` / `forget_app` / `forget_all`, `describe`.
- **`automation/ai.py`**: `agent_step` — local model only, thinking off, `PAGE_AGENT_MODEL`.
- **Runtime** (`automation/common.py`): `pending_choice` — a "which one?" with its candidates is
  answered by the next utterance (ordinal or name); WhatsApp answers teach the alias. Phrasings:
  `part_7_action` / `part_7_direct` ("forget that" and "the other one" are rules-first only right
  after something was learned / picked).
- **WhatsApp** (`automation/web_macros.py`): `_wa_find` uses taught names; `LAST_PICK` and
  `wa_other` for "no, the other one".
- **Policy**: `learning.forget_all` is CONFIRM; the understanding layer may propose `browser agent
  …` but never a `learning` change.

## 4. Safety

| Concern | What happens |
|---|---|
| A risky button | judged by the same policy as a spoken click, plus share / follow / like / reply / comment / invite / accept / forward / leave / upload / apply / save changes…; asks naming the control; yes re-finds the same control by name if the page re-rendered |
| Cart, buy, pay | refused, never asked (D1) |
| Sending by Enter | Enter after typing into a message / comment / post box asks |
| What it types | only words the user said; anything else is refused and the model is told |
| Passwords, codes, cards | refused in Kayra and in the page; their values never leave the page |
| Instructions hidden in a page | the page is data; the closed action set and the checks above bound what such text can cause (tested with a "click Delete account" page: it asked) |
| Where page contents go | the local model only |
| What is learned | `data/aliases.json`, `data/recipes.json`, 0600, on this computer; shown and forgettable by voice |

## 5. Files

Created: `src/kayra/automation/page_agent.py`, `src/kayra/automation/learning.py`,
`tests/bench_page_agent.py`, this file.
Modified: `extension/kayra-bridge/background.js`, `manifest.json` (1.14.0),
`src/kayra/automation/{ai,common,linux,policy,interpreter,web_macros,chrome_profiles}.py`,
`tests/test_browser_control.py`, `tests/test_browser_extension.py`, `tests/test_dmm_browser.py`,
`tests/test_linux_automation.py` (the rig keeps the agent off and learning in a temp folder),
`tests/run_all.py`, `.env.example` (`PAGE_AGENT`, `PAGE_AGENT_MODEL`),
`docs/platforms/{CHROME_BRIDGE,LINUX_AUTOMATION}.md`, `docs/roadmap/BROWSER_CONTROL_PLAN.md`,
`CLAUDE.md`, `AGENTS.md`, `updates/README.md`.

## 6. Verification

Linux (Omarchy), the project venv, LM Studio with gemma-4-e4b and Qwen3.5-9B (one at a time).

- `tests/run_all.py` (unit tier): 5,716 passed, 0 failed (V14: 5,659).
- `tests/test_browser_control.py` [23]: the loop, recipes and replay with no model call,
  self-healing, every safety rule, loops and the step cap, learning by answer, "the other one",
  forget, the 0600 store.
- `tests/test_browser_extension.py`: 142 passed ([12]: stable ids, secrets, masked codes,
  type / select / tick / click / scroll by id).
- `tests/test_dmm_browser.py` on the local model: 120/120, 99 rules-first.
- `tests/bench_page_agent.py --loops`: the table in §2; whole runs (type, search, read the result;
  sort) succeed in real Chrome with each model.
- `tests/test_profile_privacy_guard.py`: passed. pyflakes clean on every changed file.

## 7. Not verified

- The agent has not been run on a live site in the user's Chrome; its runs were on fixture pages
  in a throwaway headless Chrome with the real local model.
- Recipes on real sites whose accessible names change often (LinkedIn) may heal often.

## 8. Upgrade notes

Nothing to do; the extension updates itself to 1.14.0. `PAGE_AGENT=off` in `.env` disables the
agent; `PAGE_AGENT_MODEL` picks its model. What Kayra learns lives in `data/aliases.json` and
`data/recipes.json`.
