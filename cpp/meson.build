project('tuxthrottle', 'cpp', version: '0.1.0', default_options: ['buildtype=release', 'cpp_std=c++20', 'warning_level=2', 'optimization=3', 'b_ndebug=if-release', 'strip=true'])

# Same GCC tuning as lestrix (neutral on speed here, measured with scripts/bench-variants.sh); only where the compiler accepts it.
cxx = meson.get_compiler('cpp')
add_project_arguments(cxx.get_supported_arguments('-freorder-blocks-algorithm=simple', '-fvect-cost-model=unlimited'), language: 'cpp')

# Core engine: C++ standard library + libc only. No JSON library, no test framework.
core_src = files(
  'src/json.cpp', 'src/util.cpp', 'src/distro.cpp', 'src/kargs.cpp', 'src/bootcfg.cpp', 'src/bench.cpp', 'src/sensors.cpp', 'src/userenv.cpp', 'src/profiles.cpp', 'src/control.cpp', 'src/status.cpp', 'src/powerd.cpp', 'src/safety.cpp', 'src/hwtools.cpp', 'src/vram.cpp', 'src/desktop.cpp', 'src/devices.cpp', 'src/engine.cpp', 'src/drivehealth.cpp', 'src/steam.cpp', 'src/formats.cpp', 'src/launchers.cpp', 'src/gamecfg.cpp', 'src/steamperf.cpp', 'src/mangohud.cpp', 'src/compat.cpp', 'src/diag.cpp', 'src/gpudrivers.cpp', 'src/hotkey.cpp', 'src/launchbuild.cpp', 'src/mhedit.cpp', 'src/dbus.cpp', 'src/clients.cpp', 'src/ui/png.cpp',
)
thread_dep = dependency('threads')
core = static_library('ttcore', core_src, include_directories: include_directories('src'), dependencies: thread_dep)
core_dep = declare_dependency(link_with: core, include_directories: include_directories('src'), dependencies: thread_dep)

ctl_exe = executable('tuxthrottlectl', 'src/main.cpp', dependencies: core_dep, install: true)
sdl_dep = dependency('sdl2')
ft_dep = dependency('freetype2')
fc_dep = dependency('fontconfig')
gui_src = files('src/gui_main.cpp', 'src/ui/gfx.cpp', 'src/ui/ui.cpp', 'src/ui/icon.cpp', 'src/app/app.cpp', 'src/app/navicons.cpp', 'src/app/widgets.cpp', 'src/app/page_dashboard.cpp', 'src/app/page_category.cpp', 'src/app/page_updates.cpp', 'src/app/page_profiles.cpp', 'src/app/page_drives.cpp', 'src/app/page_fans.cpp', 'src/app/page_hardware.cpp', 'src/app/page_vram.cpp', 'src/app/page_keyboard.cpp', 'src/app/page_games.cpp', 'src/app/page_gametools.cpp', 'src/app/page_mangohud.cpp', 'src/app/page_proton.cpp', 'src/app/page_support.cpp')
gui_exe = executable('tuxthrottle', gui_src, dependencies: [core_dep, sdl_dep, ft_dep, fc_dep], install: true)
tray_src = files('src/tray/tray_main.cpp', 'src/ui/gfx.cpp', 'src/ui/ui.cpp', 'src/ui/icon.cpp')
executable('tuxthrottle-tray', tray_src, dependencies: [core_dep, sdl_dep, ft_dep, fc_dep], install: true)
executable('tuxthrottled', 'src/daemon_main.cpp', dependencies: core_dep, install: true)

test_exe = executable('tt_tests', ['tests/run.cpp', 'tests/test_extra.cpp'], dependencies: core_dep)
test('core', test_exe, args: [meson.project_source_root() + '/../config'], env: {'TT_TEST_TMP': meson.current_build_dir()})

smoke = find_program('bash')
test('smoke', smoke, args: [meson.project_source_root() + '/tests/smoke.sh', meson.current_build_dir(), meson.project_source_root()],
     depends: [ctl_exe, gui_exe], timeout: 900, env: {'TT_TEST_TMP': meson.current_build_dir()})
