# Architectural Blueprint

The structural rules of the codebase in one place — what may depend on what, where each kind of
code belongs, and how to extend it without breaking the invariants. (This replaces the old
root-level `blueprint.md`, which described a `modules/` layout that no longer exists.)

## Dependency graph

```
                      ┌────────────┐
                      │   run.py   │  stdlib → re-exec → kayra.app / kayra.ui
                      └─────┬──────┘
                            ▼
   ┌──────────┐      ┌────────────┐
   │ kayra.ui │ ───► │ kayra.app  │  orchestration
   └──────────┘      └─────┬──────┘
                           ▼
 ┌───────────────┬─────────┴─────┬───────────┬──────────┬──────────────┐
 │ intelligence  │     input     │  output   │ services │  automation  │  capabilities
 └───────┬───────┴───────┬───────┴─────┬─────┴────┬─────┴──────┬───────┘  (automation ↛ input)
         └───────────────┴──────┬──────┴──────────┴────────────┘
                                ▼
                   ┌────────────────────────┐
                   │ kayra.core, kayra.utils│  leaf utilities
                   └───────────┬────────────┘
                               ▼
                     ┌───────────────────┐
                     │  kayra.platforms  │  OS boundary — stdlib only
                     └───────────────────┘
```

Arrows point from importer to imported. Nothing points upward. AST checks in the test suites
enforce the important edges (core is a leaf, platforms is a leaf, automation never imports
input, UI views never import engines, gesture never touches voice state).

## Where code goes

| You are adding… | It belongs in… |
|---|---|
| A question about the OS (window, display, file manager, input injection, accessibility) | `kayra/platforms/{windows,linux,fallback}.py` + façade |
| A filesystem location | `core/paths.py` |
| A configuration key | `.env.example` (documented) + read via `core/config.env()` |
| A spoken command about Kayra itself | `core/voice_control.py` (local, exact match) |
| A new kind of task the DMM can emit | `llm_engine.funcs` + few-shot examples + a handler (and the matrix test) |
| A model provider | `intelligence/provider_router.py` chains + client construction in `llm_engine` |
| A long-running capability | `services/` with explicit start/stop wired into `bootstrap`/`request_shutdown` |
| Desktop control | `automation/` — **only when the automation phase is opened** |
| A screen or widget | `ui/views/` composing `ui/components/`; colours only in `ui/theme/` |
| A log line | `core/logbus` with an existing `Subsystem` constant |

## Extension recipes

**New platform capability**
1. Add `def capability(...)` to `platforms/windows.py`, `linux.py` and `fallback.py`.
2. Expose it in `platforms/__init__.py` (wrap in try/except, return an "unknown" value).
3. Call it from the shared module; delete that module's `sys.platform` branch.
4. Test with synthetic inputs in `tests/test_platforms.py`.

**New OS (e.g. macOS)**
1. `platforms/macos.py` exporting the full contract (start by copying `fallback.py`).
2. Map `FAMILY_MACOS` in `platforms/__init__.py::_BACKENDS`.
3. `core/hardware.py`: add the OS/GPU readers (sysctl/IOKit reads, no subprocess on hot paths).
4. `requirements.txt` markers, `setup.py` system checks, `docs/platforms/MACOS.md`.

**New Linux compositor**
Implement its IPC next to `hyprland.py` and branch inside `linux.py` on
`detect.current().desktop`; keep the Hyprland path first because Omarchy is the primary target.

## Invariants (summary — details in `AGENTS.md` and `CLAUDE.md`)

- One process, one STT browser session, one TTS queue, one LLM engine, one runtime state.
- One authority per concern; no second retry loop, logger format, shutdown path or path resolver.
- Absent capability → reported as unavailable, never guessed.
- Destructive actions are confirmation-gated and target-explicit.
- No import-time side effects; no `shell=True`; no killing by process name.
