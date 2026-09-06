# Perf baseline — pre "speed/UI/features" effort (2026-09-06)

Captured on g15 (`/tmp/coldstart.py`, `/usr/bin/python3`, `sys.path` → `/opt/tuxthrottle`),
5 runs, before any Phase-1 code change. Commit `058c8c9`.

| Metric | Value (median of 5) |
|---|---|
| `import tuxthrottle` | 0.21 s |
| **`ToolkitApp(root)` ctor (`_build_ui`)** | **2.10 s** |
| nav pages built at startup | 25 |
| total widgets across all page frames | 2049 |
| RSS after build (headless) | ~85.7 MB |

Heaviest page frames by widget count:
Setup Games 386 · Gaming 181 · Performance 175 · Game Tools 154 · Profiles 141 · Power 116

The ~2.1 s ctor is the blocking `_build_ui` cost this effort targets (lazy tab
build + per-tab poll gating). `to_first_map` in the headless harness is
misleading (~0.21 s) because the Window maps before `ToolkitApp` is constructed;
**ctor_s is the metric to compare in Phase 4.**

## After Phases 1–3 (commit 928fd40) — measured on g15

| Metric | Baseline | After | Δ |
|---|---|---|---|
| `ToolkitApp(root)` ctor | 2.10 s | **0.40 s** | −81% |
| widgets built at startup | 2049 | 57 | Dashboard only |
| RSS (headless) | 85.7 MB | 72.4 MB | −15% |
| gated sensor calls / 6 s idle, non-live tab (Keyboard) | ~6–9 (fans+bat+power pollers all ran) | **0** | pollers now follow the visible tab |
| gated sensor calls / 6 s idle, live tab (Power & Limits) | n/a | 4 | `_power_poll` @ 3 s, as intended |

First-open build hitch per tab (drive harness, includes ~120 ms harness sleep):
~220–490 ms; heaviest = Setup Games ~390 ms. One-time per tab per session, no
errors. Slightly above the 200 ms estimate but not split further — acceptable.

`ruff` / `pytest` (206 sandbox / 224 g15) / `mypy` gate all clean;
`verify-install.sh` 32/0; full tab click-through on g15 raised no tracebacks;
CI green on all four jobs across every commit.
