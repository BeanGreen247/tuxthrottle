%global appdir  /opt/tuxthrottle
%global _vpath_srcdir cpp
%global gittag   %{?_gittag}%{!?_gittag:main}

Name:           tuxthrottle
Version:        %{?_version}%{!?_version:26.10.25}
Release:        %{?_release}%{!?_release:1}%{?dist}
Summary:        Gaming, power and thermal control panel for the Dell G15 5515 (Ryzen) on Nobara/KDE

License:        MIT
URL:            https://github.com/BeanGreen247/tuxthrottle
Source0:        %{url}/archive/%{gittag}/%{name}-%{gittag}.tar.gz

BuildRequires:  gcc-c++
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  pkgconfig(sdl2)
BuildRequires:  pkgconfig(freetype2)
BuildRequires:  pkgconfig(fontconfig)

Requires:       SDL2
Requires:       freetype
Requires:       fontconfig
Recommends:     rsms-inter-fonts
# used by helpers / tweaks at runtime (best-effort; the app degrades without them)
Recommends:     openrgb
Recommends:     ryzenadj
Recommends:     libsmbios
Recommends:     polkit
Recommends:     gamemode
Recommends:     smartmontools
Recommends:     ntfsprogs
Recommends:     cronie

# The in-app "tweaks" (systemd units, sudoers, kwriteconfig6 …) stay strictly
# opt-in and are applied from the GUI / tuxthrottlectl apply-tweak - never from %post.

%description
TuxThrottle is a native (C++, SDL2) control-panel GUI plus a system-tray monitor, a background
daemon (tuxthrottled) and a G-key listener that apply hardware-specific tweaks,
drivers and gaming setup to the Dell G15 5515 Ryzen Edition (Ryzen 7 5800H +
RTX 3050 Ti Mobile) running Nobara Linux on KDE Plasma 6 / Wayland.

What it controls:
 - CPU power limits (ryzenadj STAPM/fast/slow) with presets, and an opt-in
   Ryzen Curve Optimizer undervolt gated behind a stress-test-and-auto-revert
   harness.
 - GPU: NVIDIA board power limit where the firmware allows it, a graphics-clock
   lock that works when it does not, hybrid-graphics mode via EnvyControl,
   NVIDIA Dynamic Boost and shader-cache tuning.
 - Cooling: thermal profile, additive fan boost, a closed-loop custom fan curve
   (up to 10 points) run by the daemon, manual PWM behind a warning, and
   automatic recovery from the G15 firmware fan-stall bug.
 - Battery: charge-limit threshold (sysfs or Dell libsmbios), a health page
   (wear %, cycles), and standard/express charge speed.
 - Display: panel refresh-rate switching (e.g. 144<->60 Hz).
 - Automation: AC/battery auto-switch, per-game auto-profiles, a time-of-day
   schedule, and named full-state profiles with automatic snapshot/rollback;
   thermal-event desktop notifications; state re-applied after resume/boot.
 - KDE Plasma 6 desktop tweaks (animations, compositor, screen edges, panel
   flush, Meta-key, KWallet, allow-tearing, and more).
 - Setup Games: click-through per-title setup (GTA V Online) plus Proton prefix
   relocation and save-game vault tools.
 - A headless CLI (tuxthrottlectl) and a control socket the GUI, CLI and
   optional waybar / Plasma / MangoHud clients all write through.

Every check/apply command targets that one board; run "tuxthrottle --report" to
see what applies on the running machine. The in-app tweaks are strictly opt-in
and are never applied from %post.

%prep
%autosetup -n %{name}-%{gittag}

%build
%meson -Doptimization=3 -Db_lto=true
%meson_build

%install
%meson_install
install -d %{buildroot}%{appdir}
cp -a config models assets clients polkit python/osu_lazer_tools tuxthrottle_*.sh %{buildroot}%{appdir}/
# only the osu! performance setup from the osu_lazer_tools subtree
find %{buildroot}%{appdir}/osu_lazer_tools -mindepth 1 -maxdepth 1 ! -name osu-lazer-launcher.sh \
    ! -name osu_lazer_diag.py ! -name osu_lazer_settings.py ! -name LICENSE -exec rm -rf {} +
install -m 0644 README.md LICENSE %{buildroot}%{appdir}/ 2>/dev/null || :
printf '%%s\n' "%{version}-%{release}" > %{buildroot}%{appdir}/.version

# desktop entry + icons
install -d %{buildroot}%{_datadir}/applications
sed 's#__TOOLKIT_DIR__#%{appdir}#g' assets/tuxthrottle.desktop \
    > %{buildroot}%{_datadir}/applications/tuxthrottle.desktop
for s in 16 24 32 48 64 128 256 512; do
    if [ -f assets/icon-${s}.png ]; then
        install -Dm0644 assets/icon-${s}.png \
            %{buildroot}%{_datadir}/icons/hicolor/${s}x${s}/apps/tuxthrottle.png
    fi
done
install -Dm0644 assets/icon.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/tuxthrottle.svg 2>/dev/null || :

%files
%license LICENSE
%doc README.md
%{appdir}
%{_bindir}/tuxthrottle
%{_bindir}/tuxthrottlectl
%{_bindir}/tuxthrottle-tray
%{_bindir}/tuxthrottled
%{_datadir}/applications/tuxthrottle.desktop
%{_datadir}/icons/hicolor/*/apps/tuxthrottle.*

%changelog
* Sun Aug 31 2026 BeanGreen247 <mozdrent@gmail.com> - 0.1.0-1
- Initial RPM packaging: GUI + tuxthrottlectl + tray + hotkey listener + daemon.
- Tweaks remain opt-in and are applied from the app, never from %%post.
