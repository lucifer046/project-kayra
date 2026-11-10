# Updates

The version history of Kayra's major changes. One detailed report per update, numbered in
order. Newest last.

| Version | Date | Title | Type |
|---|---|---|---|
| [V1](UPDATE_V1.md) | 2026-09-10 (recorded 2026-11-09) | Baseline: the repository as cloned (commit `929371b`) | Baseline snapshot |
| [V2](UPDATE_V2.md) | 2026-11-09 | Cross-platform architecture, Linux/Omarchy support, dependency modernization | Major feature, reliability, dependencies, docs |
| [V3](UPDATE_V3.md) | 2026-11-10 | Tuning pass: fast speech model, fewer model calls, readiness waits, short CLAUDE.md | Performance, maintenance |

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
