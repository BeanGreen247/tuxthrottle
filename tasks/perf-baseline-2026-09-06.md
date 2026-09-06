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

Idle CPU / syscall baseline: to capture in Phase 4 via a real launched instance
(`pidstat` / `strace -fc` on a non-live tab).
