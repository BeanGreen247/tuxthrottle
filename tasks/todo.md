# TuxThrottle — todo

## Done (2026-08-30)
- Backlog phases 1-8: CPU TDP (ryzenadj), battery limit (sysfs + libsmbios),
  NVIDIA power limit, GameMode bridge, closed-loop fan curve + AC/battery
  auto-switch, tuxthrottlectl, hybrid-GPU switch, CPU/iGPU/dGPU clock gauges.
- Polish: README/CLAUDE.md, dnf-metadata age, RAPL auto-hide.
- Scrollable nav rail (256px) + pinned About/Report a Bug + About page + screenshot.
- Tier 1: tuxthrottle_profiles.py (named profiles + auto snapshot/rollback),
  Profiles tab, tuxthrottlectl profile|snapshot|rollback, snapshot-before-Apply,
  StateResume tweak, Dashboard history sparklines + session CSV.
- Tier 2: per-game auto-profiles (GameProfileController in the daemon + editor),
  unified resume re-assert (StateResume).
- KDE (Desktop GUI Tweaks) category — 7 Plasma-6 toggles.
- tests/ (31 pytest) + .github/workflows/ci.yml.
- install.sh dependency hardening.
- KDE (Desktop GUI Tweaks): 9 toggles, all verified LIVE on g15 (2026-08-30).
  Fixed 3 that were applied-but-no-op: KdeScreenEdgesOff (added [Effect-*]
  BorderActivate=9 hot corners), KdeAnimationsOff (unloadEffect via D-Bus),
  KdeActivitiesRecentOff (real ResourceScoringEnabled plugin key). qdbus6
  doesn't exist on Nobara -> qdbus-qt6/dbus-send. See memory tuxthrottle-kde-tweaks.
- README + CLAUDE.md updated (Profiles tab, KDE section, new helper files, KDE gotchas).

## Tier 3 — deferred (full plan: ~/tuxthrottle-tier3-followups-2026-08-31.md)
- [ ] COPR / RPM packaging
- [ ] thermal-event notifications (in the daemon)
- [ ] Ryzen Curve Optimizer undervolt + stress-test/auto-revert harness
- [ ] multi-model DMI gating (needs another machine's --collect bundle)
- [ ] KDE Plasmoid / waybar client (reads tuxthrottlectl --json)
- [ ] single-writer control plane: powerd control socket, then D-Bus + polkit

## Deferred (own branch, needs kbd hardware)
- [ ] delete dead rainbow_wave/gradient_wave/_Sdk/_stream_wave from tuxthrottle_kbd.py
