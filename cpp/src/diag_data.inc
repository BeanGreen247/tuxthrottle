// GENERATED from the Python diag/hwprobe command tables - do not edit by hand.
#pragma once

struct DebugCmd { const char* title; const char* cmd; int maxlines; };  // cmd null = section divider, "@name" = built-in
static const DebugCmd kDebugCmds[] = {
    {R"TTCMD(── SYSTEM ──)TTCMD", nullptr, 0},
    {R"TTCMD(OS)TTCMD", R"TTCMD(cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|VARIANT|ID|BUILD)' )TTCMD", 12},
    {R"TTCMD(Kernel / cmdline)TTCMD", R"TTCMD(uname -a; echo; cat /proc/cmdline)TTCMD", 6},
    {R"TTCMD(Firmware / DMI)TTCMD", R"TTCMD(for f in sys_vendor product_name product_sku board_name board_version bios_vendor bios_version bios_date chassis_type; do printf '%-16s %s\n' "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null)"; done)TTCMD", 16},
    {R"TTCMD(Desktop session)TTCMD", R"TTCMD(for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do t=$(loginctl show-session "$s" -p Type --value 2>/dev/null); case "$t" in wayland|x11) loginctl show-session "$s" -p Name -p Type -p Desktop -p Active -p Remote 2>/dev/null; break;; esac; done; echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-} XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}")TTCMD", 10},
    {R"TTCMD(Uptime / load)TTCMD", R"TTCMD(uptime)TTCMD", 3},
    {R"TTCMD(── CPU / MEMORY ──)TTCMD", nullptr, 0},
    {R"TTCMD(CPU)TTCMD", R"TTCMD(lscpu 2>/dev/null | grep -E 'Model name|^CPU\(s\)|Thread|Core|Socket|CPU max|Vendor'; echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null) epp: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)")TTCMD", 16},
    {R"TTCMD(Memory / zram)TTCMD", R"TTCMD(free -h; echo; zramctl 2>/dev/null; swapon --show 2>/dev/null)TTCMD", 14},
    {R"TTCMD(── GPU ──)TTCMD", nullptr, 0},
    {R"TTCMD(PCI display devices)TTCMD", R"TTCMD(lspci -nnk 2>/dev/null | grep -iA3 -E 'vga compatible|3d controller|display controller')TTCMD", 24},
    {R"TTCMD(NVIDIA)TTCMD", R"TTCMD(nvidia-smi 2>/dev/null | head -18 || echo '(nvidia-smi unavailable - driver missing or dGPU runtime-suspended)')TTCMD", 20},
    {R"TTCMD(NVIDIA runtime PM)TTCMD", R"TTCMD(for d in /sys/bus/pci/devices/*; do [ "$(cat $d/vendor 2>/dev/null)" = 0x10de ] && echo "$(basename $d)  class=$(cat $d/class 2>/dev/null)  power=$(cat $d/power/runtime_status 2>/dev/null)"; done)TTCMD", 6},
    {R"TTCMD(AMD iGPU)TTCMD", R"TTCMD(for c in /sys/class/drm/card[0-9]*/device; do [ "$(cat $c/vendor 2>/dev/null)" = 0x1002 ] && { echo "$c"; echo " dpm: $(cat $c/power_dpm_force_performance_level 2>/dev/null)"; cat $c/pp_dpm_sclk 2>/dev/null; }; done)TTCMD", 20},
    {R"TTCMD(Mesa / GL)TTCMD", R"TTCMD(glxinfo -B 2>/dev/null | grep -E 'OpenGL renderer|OpenGL version|Device:|Video memory' || echo '(glxinfo not installed)')TTCMD", 10},
    {R"TTCMD(── THERMAL / POWER ──)TTCMD", nullptr, 0},
    {R"TTCMD(platform_profile)TTCMD", R"TTCMD(echo "current: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null)"; echo "choices: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)")TTCMD", 4},
    {R"TTCMD(power-profiles-daemon)TTCMD", R"TTCMD(powerprofilesctl get 2>/dev/null; echo '---'; powerprofilesctl 2>/dev/null | head -24)TTCMD", 26},
    {R"TTCMD(Fans / hwmon)TTCMD", R"TTCMD(@fans)TTCMD", 0},
    {R"TTCMD(sensors)TTCMD", R"TTCMD(sensors 2>/dev/null || echo '(lm_sensors not installed)')TTCMD", 45},
    {R"TTCMD(RAPL (CPU power))TTCMD", R"TTCMD(ls -l /sys/class/powercap/*/energy_uj 2>/dev/null; (head -c1 /sys/class/powercap/intel-rapl:0/energy_uj >/dev/null 2>&1 && echo 'RAPL readable') || echo 'RAPL NOT readable without root (kernel side-channel mitigation)')TTCMD", 10},
    {R"TTCMD(── KEYBOARD / HOTKEYS / MEDIA KEYS ──)TTCMD", nullptr, 0},
    {R"TTCMD(Loaded modules)TTCMD", R"TTCMD(lsmod | grep -E '^(dell|alienware|i8k|sparse_keymap|hid_|nvidia|amdgpu)' | sort)TTCMD", 30},
    {R"TTCMD(Alienware USB LED controller)TTCMD", R"TTCMD(lsusb 2>/dev/null | grep -iE '187c:|alienware' || echo '(187c:0550 AW-ELC not seen on USB)')TTCMD", 4},
    {R"TTCMD(HID devices)TTCMD", R"TTCMD(for h in /sys/bus/hid/devices/*; do [ -e "$h" ] || continue; printf '%-24s %s\n' "$(basename $h)" "$(cat $h/input/input*/name 2>/dev/null | head -1)"; done)TTCMD", 20},
    {R"TTCMD(input devices (evdev + KEY capability bitmaps))TTCMD", R"TTCMD(cat /proc/bus/input/devices)TTCMD", 140},
    {R"TTCMD(event device names)TTCMD", R"TTCMD(for e in /dev/input/event*; do printf '%-22s %s\n' "$e" "$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null)"; done)TTCMD", 30},
    {R"TTCMD(Dell WMI / hotkey / media-key devices)TTCMD", R"TTCMD(for e in /dev/input/event*; do n=$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null); case "$n" in *WMI*|*wireless\ hotkey*|*Wireless\ hotkey*|*Translated\ Set\ 2*|*Video\ Bus*) echo "$e  $n";; esac; done)TTCMD", 15},
    {R"TTCMD(Fn-Lock / G-key note)TTCMD", R"TTCMD(echo 'G-key = KEY_PERFORMANCE(701) on "AT Translated Set 2 keyboard" when Fn-Lock OFF, KEY_F9 when ON. Media keys (vol/mute) come via "Dell WMI hotkeys". Fn is an EC key and never reaches evdev.')TTCMD", 4},
    {R"TTCMD(input group membership)TTCMD", R"TTCMD(u=$(logname 2>/dev/null || echo "${SUDO_USER:-}"); echo "desktop user: $u"; id "$u" 2>/dev/null; getent group input; id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx input && echo 'OK: user is in the input group' || echo 'WARN: user NOT in input group (the G-key HotkeyListener needs it)')TTCMD", 8},
    {R"TTCMD(── RGB KEYBOARD (OpenRGB) ──)TTCMD", nullptr, 0},
    {R"TTCMD(OpenRGB)TTCMD", R"TTCMD(openrgb --version 2>/dev/null | head -1 || echo '(openrgb not installed)')TTCMD", 4},
    {R"TTCMD(OpenRGB devices)TTCMD", R"TTCMD(openrgb --noautoconnect -l 2>/dev/null | grep -vE '<[a-z]|i2c|SMBus|help.openrgb' | head -40)TTCMD", 45},
    {R"TTCMD(kbd services)TTCMD", R"TTCMD(for s in tuxthrottle-openrgb.service tuxthrottle-kbd.service; do printf '%-26s enabled=%-9s active=%s\n' "$s" "$(systemctl is-enabled $s 2>/dev/null)" "$(systemctl is-active $s 2>/dev/null)"; done)TTCMD", 6},
    {R"TTCMD(kbd saved state)TTCMD", R"TTCMD(u=$(logname 2>/dev/null || echo "${SUDO_USER:-$USER}"); h=$(getent passwd "$u" | cut -d: -f6); cat "$h/.config/tuxthrottle/kbd.json" 2>/dev/null || echo '(no kbd.json - colour not saved / KbdBacklightFix not used)')TTCMD", 24},
    {R"TTCMD(── TWEAK SERVICES / SUDOERS ──)TTCMD", nullptr, 0},
    {R"TTCMD(tuxthrottle units)TTCMD", R"TTCMD(systemctl list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey' ; systemctl --user list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey')TTCMD", 12},
    {R"TTCMD(sudoers drop-ins)TTCMD", R"TTCMD(ls -l /etc/sudoers.d/ 2>/dev/null | grep -E 'tuxthrottle|gamemode|claude' || echo '(none)')TTCMD", 8},
    {R"TTCMD(── PACKAGES ──)TTCMD", nullptr, 0},
    {R"TTCMD(Kernels installed)TTCMD", R"TTCMD(rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort -V)TTCMD", 10},
    {R"TTCMD(NVIDIA packages)TTCMD", R"TTCMD(rpm -qa 2>/dev/null | grep -iE 'nvidia|akmod-nvidia|cuda' | sort || echo '(no NVIDIA packages - driver may be from a -NV image or missing)')TTCMD", 12},
    {R"TTCMD(Relevant packages)TTCMD", R"TTCMD(rpm -q openrgb gamemode mangohud goverlay gamescope vkbasalt lm_sensors nobara-updater tlp auto-cpufreq 2>&1 | sed 's/ is not installed/  - NOT installed/')TTCMD", 14},
    {R"TTCMD(Update tooling)TTCMD", R"TTCMD(dnf --version 2>/dev/null | head -1; command -v nobara-sync >/dev/null && echo 'nobara-sync: present'; command -v flatpak >/dev/null && flatpak --version; command -v fwupdmgr >/dev/null && echo 'fwupd: present')TTCMD", 6},
    {R"TTCMD(── LOGS ──)TTCMD", nullptr, 0},
    {R"TTCMD(dmesg (filtered, deduped))TTCMD", R"TTCMD(dmesg 2>/dev/null | grep -iE 'dell_|dell-|alienware|aw-elc|187c:0550|hid-generic 0003:187C|i8042|firmware bug|thermal (throttl|event)|MCE|hardware error|(nvidia|amdgpu|nouveau).*(error|fail|warn|timed? ?out|reset|hang|fault|Xid)|platform.?profile|pstate' | grep -viE 'Mode Validation Warning|Unknown Status failed|Console: switching|fbcon' | sed -E 's/^\[[0-9. ]+\] //' | awk '!seen[$0]++' | tail -40 || echo '(dmesg not readable - run the toolkit with sudo, or kernel.dmesg_restrict=1)')TTCMD", 42},
    {R"TTCMD(journal errors (this boot))TTCMD", R"TTCMD(journalctl -b -p err --no-pager 2>/dev/null | grep -viE 'Module lib.*from rpm|^ *Module |drkonqi|KCrash|Stack trace|^ *#[0-9]+ +0x|libQt6|libKF6|libc\.so|__libc_start' | awk '!seen[$0]++' | tail -35 || echo '(journalctl unavailable)')TTCMD", 37},
    {R"TTCMD(journal - kbd / fan / gpu units (this boot))TTCMD", R"TTCMD(journalctl -b --no-pager -u 'tuxthrottle-*' -u 'tuxthrottle-*.service' 2>/dev/null | tail -25; journalctl -b --no-pager 2>/dev/null | grep -iE 'openrgb\[|dell_smm|alienware_wmi|nvidia-persistenced|(nvidia|amdgpu).*(Xid|GPU has fallen|ring .* timeout)' | grep -viE 'audit\[|sudo\[|Mode Validation' | awk '!seen[$0]++' | tail -20 || echo '(none)')TTCMD", 40},
};

struct BundleFile { const char* name; const char* cmd; };  // cmd "@name" = built-in
static const BundleFile kBundleFiles[] = {
    {"model-scaffold.json", R"TTCMD(@model_scaffold)TTCMD"},
    {"dmi.txt", R"TTCMD(grep -r . /sys/class/dmi/id/ 2>/dev/null | sed 's#/sys/class/dmi/id/##' | grep -viE 'uevid|modalias'; echo; echo '# dmidecode (root)'; dmidecode -t 0 -t 1 -t 2 -t 3 -t 11 2>/dev/null || echo '(dmidecode needs root)')TTCMD"},
    {"kernel.txt", R"TTCMD(uname -a; echo; echo '# cmdline'; cat /proc/cmdline; echo; echo '# os-release'; cat /etc/os-release; echo; echo '# virt'; systemd-detect-virt 2>/dev/null)TTCMD"},
    {"cpu.txt", R"TTCMD(lscpu 2>/dev/null; echo; echo '# /proc/cpuinfo (cpu0)'; awk '/^$/{exit} {print}' /proc/cpuinfo; echo; echo '# amd_pstate'; grep -rH . /sys/devices/system/cpu/amd_pstate/ 2>/dev/null; echo; echo '# ryzenadj -i'; ryzenadj -i 2>/dev/null || echo '(ryzenadj not installed / not AMD)')TTCMD"},
    {"lspci.txt", R"TTCMD(lspci -nnvvv 2>/dev/null || lspci -nnk 2>/dev/null || echo '(lspci missing)')TTCMD"},
    {"lsusb.txt", R"TTCMD(lsusb -t 2>/dev/null; echo; lsusb 2>/dev/null; echo '=== verbose ==='; lsusb -v 2>/dev/null)TTCMD"},
    {"modules.txt", R"TTCMD(lsmod; echo; for m in dell_laptop dell_wmi dell_smbios dell_smm_hwmon alienware_wmi hid_generic i8k sparse_keymap; do echo "=== modinfo $m ==="; modinfo $m 2>/dev/null | grep -E '^(filename|description|parm|alias):'; done)TTCMD"},
    {"input-devices.txt", R"TTCMD(cat /proc/bus/input/devices)TTCMD"},
    {"key-capabilities.txt", R"TTCMD(@key_caps)TTCMD"},
    {"evdev-udev.txt", R"TTCMD(for e in /dev/input/event*; do echo "=== $e ==="; udevadm info -q all -n $e 2>/dev/null; echo; done)TTCMD"},
    {"hwmon.txt", R"TTCMD(for h in /sys/class/hwmon/hwmon*; do echo "### $h  name=$(cat $h/name 2>/dev/null)"; for f in $h/*; do [ -f "$f" ] || continue; printf '  %-26s %s\n' "$(basename $f)" "$(head -c 160 "$f" 2>/dev/null | tr -d '\n')"; done; echo; done)TTCMD"},
    {"thermal-power.txt", R"TTCMD(echo '# platform_profile'; for f in /sys/firmware/acpi/platform_profile*; do echo "$f = $(cat $f 2>/dev/null)"; done; echo; echo '# powercap'; grep -rH . /sys/class/powercap/*/name /sys/class/powercap/*/*_range_uj 2>/dev/null; echo; echo '# power-profiles-daemon'; powerprofilesctl 2>/dev/null; echo; echo '# lm_sensors'; sensors 2>/dev/null; echo; sensors -j 2>/dev/null)TTCMD"},
    {"battery.txt", R"TTCMD(echo '# power_supply sysfs'; grep -rH . /sys/class/power_supply/*/ 2>/dev/null | grep -viE 'uevent|modalias'; echo; echo '# upower'; timeout 4 upower -d 2>/dev/null; echo; echo '# smbios-battery-ctl'; timeout 4 smbios-battery-ctl --get-charging-cfg 2>/dev/null || echo '(libsmbios not installed / not a Dell)')TTCMD"},
    {"vendor-platform.txt", R"TTCMD(echo '# /sys/devices/platform vendor interfaces'; for d in /sys/devices/platform/*wmi* /sys/devices/platform/*-laptop /sys/devices/platform/*_laptop /sys/devices/platform/alienware-wmi* /sys/devices/platform/dell-laptop; do [ -d "$d" ] || continue; echo "### $d"; grep -rH . "$d" 2>/dev/null | grep -viE 'uevent|modalias|power/' | head -80; echo; done; echo '# /sys/class/leds'; for l in /sys/class/leds/*; do echo "$(basename $l): brightness=$(cat $l/brightness 2>/dev/null) max=$(cat $l/max_brightness 2>/dev/null)"; done; echo; echo '# module parameters'; for m in dell_laptop dell_smm_hwmon alienware_wmi asus_nb_wmi hp_wmi ideapad_laptop thinkpad_acpi; do [ -d /sys/module/$m/parameters ] || continue; echo "=== $m ==="; grep -rH . /sys/module/$m/parameters/ 2>/dev/null; done)TTCMD"},
    {"firmware.txt", R"TTCMD(echo '# fwupd devices'; timeout 6 fwupdmgr get-devices --no-authenticate-modules 2>/dev/null || timeout 6 fwupdmgr get-devices 2>/dev/null || echo '(fwupd not installed / timed out reaching the daemon)')TTCMD"},
    {"acpi.txt", R"TTCMD(ls -l /sys/firmware/acpi/tables/ 2>/dev/null; echo; command -v acpidump >/dev/null && echo 'acpidump present - run: sudo acpidump -b (attach the DSDT.dat)'; command -v acpi_listen >/dev/null && echo 'acpi_listen present - run it and press Fn/media keys to capture ACPI events')TTCMD"},
    {"dsdt.b64", R"TTCMD(echo '# base64 of the ACPI DSDT + SSDTs - decode with:  base64 -d dsdt.b64 > acpi.bin ; iasl -d acpi.bin'; for t in /sys/firmware/acpi/tables/DSDT /sys/firmware/acpi/tables/SSDT*; do [ -r "$t" ] || continue; echo "=== $(basename $t) ==="; base64 "$t" 2>/dev/null; echo; done || echo '(ACPI tables need root to read)')TTCMD"},
    {"display.txt", R"TTCMD(@display)TTCMD"},
    {"drm-gpu.txt", R"TTCMD(for c in /sys/class/drm/card[0-9]*; do echo "### $c"; cat $c/device/uevent 2>/dev/null; echo " runtime_status=$(cat $c/device/power/runtime_status 2>/dev/null)"; echo; done; echo '=== nvidia-smi -q ==='; timeout 8 nvidia-smi -q 2>/dev/null; echo; echo '=== nvidia-smi -q -d SUPPORTED_CLOCKS ==='; timeout 8 nvidia-smi -q -d SUPPORTED_CLOCKS 2>/dev/null | head -50; echo; echo '=== glxinfo / vulkaninfo ==='; timeout 4 glxinfo -B 2>/dev/null | grep -E 'renderer|OpenGL version|Device'; timeout 4 vulkaninfo --summary 2>/dev/null | head -40)TTCMD"},
    {"openrgb.txt", R"TTCMD(openrgb --version 2>/dev/null; echo; openrgb --noautoconnect -l --verbose 2>/dev/null | grep -vE 'i2c|SMBus|help.openrgb' || openrgb --noautoconnect -l 2>/dev/null)TTCMD"},
    {"smbios-tokens.txt", R"TTCMD(timeout 6 smbios-token-ctl 2>/dev/null | head -400 || echo '(libsmbios / smbios-token-ctl not installed - needed for Dell battery / USB / thermal tokens)')TTCMD"},
    {"dmesg-full.txt", R"TTCMD(dmesg 2>/dev/null || echo '(dmesg needs root / kernel.dmesg_restrict=1)')TTCMD"},
    {"journal-boot-tail.txt", R"TTCMD(journalctl -b --no-pager 2>/dev/null | tail -3000 || echo '(journalctl unavailable)')TTCMD"},
    {"system-summary.json", R"TTCMD(@system_summary)TTCMD"},
    {"tuxthrottle-capabilities.json", R"TTCMD(@capabilities)TTCMD"},
    {"gpu-stack.txt", R"TTCMD(echo '# display/3D controllers'; lspci -nnk -d ::0300 2>/dev/null; lspci -nnk -d ::0302 2>/dev/null; lspci -nnk -d ::0380 2>/dev/null; echo; echo '# boot_vga / runtime pm per PCI GPU'; for d in /sys/bus/pci/devices/*; do c=$(cat $d/class 2>/dev/null); case $c in 0x03*) echo "$(basename $d) class=$c vendor=$(cat $d/vendor) device=$(cat $d/device) boot_vga=$(cat $d/boot_vga 2>/dev/null) driver=$(basename $(readlink $d/driver) 2>/dev/null) runtime=$(cat $d/power/runtime_status 2>/dev/null) control=$(cat $d/power/control 2>/dev/null)";; esac; done; echo; echo '# hybrid/MUX switchers'; prime-select query 2>&1; envycontrol --query 2>&1; supergfxctl -g 2>&1; switcherooctl list 2>&1; ubuntu-drivers devices 2>&1 | head -30; echo; echo '# vendor MUX / dGPU controls'; grep -rH . /sys/devices/platform/*/gpu_mux_mode /sys/devices/platform/*/dgpu_disable /sys/devices/platform/*/egpu_enable /sys/devices/platform/*/gpu_mode 2>/dev/null; ls /sys/class/firmware-attributes 2>/dev/null; echo; echo '# secure boot / signing'; mokutil --sb-state 2>&1; mokutil --list-enrolled 2>/dev/null | grep -E 'Subject:|Issuer:' | head -10; echo; echo '# installed GPU driver packages'; (rpm -qa 2>/dev/null | grep -iE 'nvidia|akmod|mesa-vulkan|amdgpu|xorg-x11-drv' | sort; dpkg -l 2>/dev/null | grep -iE 'nvidia|mesa-vulkan|amdgpu' | awk '{print $2, $3}'; pacman -Qs 'nvidia|vulkan|mesa|amdgpu' 2>/dev/null | grep '^local' ; qlist -IRv 2>/dev/null | grep -iE 'nvidia|mesa|amdgpu') | head -120; echo; echo '# nvidia module params / modprobe / env'; cat /proc/driver/nvidia/version 2>/dev/null; cat /proc/driver/nvidia/params 2>/dev/null | head -60; grep -rHv '^#' /etc/modprobe.d/ /etc/environment.d/ /etc/X11/xorg.conf.d/ 2>/dev/null | head -80; echo; echo '# which GPU does a PRIME offload land on'; timeout 8 glxinfo -B 2>/dev/null | grep -E 'renderer|vendor'; DRI_PRIME=1 timeout 8 glxinfo -B 2>/dev/null | grep -E 'renderer'; __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia timeout 8 glxinfo -B 2>/dev/null | grep -E 'renderer'; timeout 8 vulkaninfo --summary 2>/dev/null | grep -E 'GPU|deviceName|driverName|apiVersion|driverInfo'; timeout 6 vainfo 2>&1 | head -8; timeout 6 clinfo -l 2>&1 | head -10)TTCMD"},
    {"session.txt", R"TTCMD(loginctl show-session $(loginctl 2>/dev/null | awk '/seat0/{print $1; exit}') 2>/dev/null | grep -E 'Type|Desktop|Class|Remote'; echo; env | grep -E '^(XDG_|WAYLAND|DISPLAY|KWIN|MESA|__GL|__NV|DRI_|VK_|LIBVA|GBM)' | sort; echo; ps -eo comm | grep -E '^(kwin|mutter|gnome-shell|sway|Hyprland|plasmashell|Xorg|Xwayland|weston|labwc|picom|xfwm4|openbox|i3|dwm)' | sort -u)TTCMD"},
    {"audio.txt", R"TTCMD(aplay -l 2>&1; echo; cat /proc/asound/cards 2>/dev/null; echo; for c in /proc/asound/card*/codec#*; do [ -f "$c" ] && { echo "=== $c ==="; head -12 "$c"; }; done; echo; wpctl status 2>&1 | head -40; pactl info 2>&1 | head -12; echo; lsmod | grep -E 'snd_|sof')TTCMD"},
    {"network.txt", R"TTCMD(lspci -nnk -d ::0200 2>/dev/null; lspci -nnk -d ::0280 2>/dev/null; echo; ip -br link 2>&1; echo; iw dev 2>&1 | head -20; iw phy 2>&1 | grep -E 'Wiphy|Band|HE|VHT|EHT' | head -20; echo; rfkill list 2>&1; echo; lsmod | grep -E 'iwl|ath|mt79|rtw|rtl|brcm|r8169|r8125|igc|e1000'; echo; nmcli -t -f DEVICE,TYPE,STATE device 2>&1)TTCMD"},
    {"storage.txt", R"TTCMD(lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,TRAN,ROTA,DISC-GRAN 2>&1; echo; findmnt -lo TARGET,SOURCE,FSTYPE,OPTIONS 2>&1 | grep -vE 'cgroup|proc|sysfs|tmpfs|devpts|securityfs|debugfs|tracefs|bpf|configfs|fusectl|mqueue|hugetlbfs'; echo; cat /etc/fstab 2>/dev/null; echo; nvme list 2>&1 | head -12; for d in /dev/nvme?n1 /dev/sd?; do [ -b $d ] && { echo "=== smartctl -i $d ==="; smartctl -i $d 2>&1 | head -16; }; done; echo; for q in /sys/block/*/queue/scheduler; do echo "$q = $(cat $q)"; done; echo; swapon --show 2>&1; grep -E 'zram|zswap' /proc/swaps /sys/module/zswap/parameters/enabled 2>/dev/null)TTCMD"},
    {"input-libinput.txt", R"TTCMD(libinput list-devices 2>&1 | head -150; echo; ls -l /dev/input/by-id /dev/input/by-path 2>&1 | head -60)TTCMD"},
    {"backlight-leds.txt", R"TTCMD(for b in /sys/class/backlight/*; do [ -d $b ] && echo "$(basename $b) type=$(cat $b/type) brightness=$(cat $b/brightness) max=$(cat $b/max_brightness)"; done; echo; ls /sys/class/leds/ 2>&1; echo; ls /sys/class/hwmon/*/name 2>/dev/null | while read f; do echo "$f: $(cat $f)"; done)TTCMD"},
    {"cpu-power-extra.txt", R"TTCMD(for p in /sys/devices/system/cpu/cpu0/cpufreq/*; do [ -f $p ] && echo "$(basename $p) = $(cat $p 2>/dev/null)"; done; echo; cat /sys/devices/system/cpu/intel_pstate/status /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null; cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null; echo; cpupower frequency-info 2>&1 | head -25; echo; grep -H . /sys/devices/system/cpu/vulnerabilities/* 2>/dev/null; echo; systemctl is-active thermald tlp power-profiles-daemon auto-cpufreq tuned scx 2>&1; echo; cat /sys/kernel/mm/transparent_hugepage/enabled /proc/sys/vm/max_map_count /proc/sys/vm/swappiness 2>/dev/null)TTCMD"},
    {"peripherals.txt", R"TTCMD(bluetoothctl list 2>&1 | head -5; rfkill list bluetooth 2>&1; echo; v4l2-ctl --list-devices 2>&1 | head -20; echo; ls /sys/class/power_supply/ 2>&1; echo; cat /sys/class/dmi/id/chassis_type 2>/dev/null; ls /sys/bus/thunderbolt/devices 2>/dev/null; ls /sys/class/typec 2>/dev/null)TTCMD"},
    {"vendor-controls.txt", R"TTCMD(for m in asus_wmi asus_nb_wmi asus_armoury legion_laptop ideapad_laptop thinkpad_acpi lenovo_wmi_gamezone hp_wmi hp_wmi_sensors acer_wmi acer_wmi_wireless msi_wmi msi_ec msi_wmi_platform samsung_laptop sony_laptop toshiba_acpi fujitsu_laptop lg_laptop huawei_wmi tuxedo_keyboard tuxedo_io clevo_wmi clevo_acpi system76_acpi system76_io framework_laptop cros_ec_lpcs gigabyte_wmi dell_wmi dell_laptop dell_smm_hwmon alienware_wmi razer_laptop apple_gmux macbook_pro_ec; do if [ -d /sys/module/$m ]; then echo "### $m (loaded)"; modinfo $m 2>/dev/null | grep -E '^(description|parm):' | head -12; grep -rH . /sys/module/$m/parameters/ 2>/dev/null | head -20; fi; done; echo; echo '# platform device tree'; ls /sys/devices/platform/ 2>&1 | grep -iE 'wmi|laptop|acpi|ec|fan|kbd|led|asus|msi|lenovo|legion|hp|acer|dell|tuxedo|system76|framework|razer|apple' ; echo; echo '# firmware-attributes (BIOS settings via sysfs)'; for d in /sys/class/firmware-attributes/*; do [ -d $d ] && { echo "## $d"; ls $d/attributes 2>/dev/null | head -80; }; done; echo; echo '# embedded controller'; ls /sys/kernel/debug/ec 2>&1 | head -4; ls /dev/cros_ec /dev/ec* 2>&1 | head; echo; echo '# ACPI platform_profile/fan_boost/etc.'; ls /sys/firmware/acpi/ 2>&1; cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)TTCMD"},
    {"tool-versions.txt", R"TTCMD(for c in 'nvidia-smi --version' 'vulkaninfo --version' 'glxinfo --version' 'wine --version' 'proton --version' 'gamescope --version' 'mangohud --version' 'gamemoded --version' 'steam --version' 'flatpak --version' 'systemctl --version' 'mesa-overlay-control.py --version' 'pipewire --version' 'ryzenadj --version' 'openrgb --version'; do echo "\$ $c"; timeout 5 $c 2>&1 | head -3; done)TTCMD"},
};

static const char* kIssueTemplate = R"TTCMD(### What happened


### What you expected instead


### Where in the toolkit (which page / button / tweak)


### Steps to reproduce
1.
2.
3.

### Is your hardware the Dell G15 5515 Ryzen Edition on Nobara?
<!-- This tool is written for exactly that one machine. On anything else most
     checks/tweaks won't apply - say what you're on. -->
- [ ] yes, G15 5515 Ryzen + Nobara
- [ ] close (other G15 / other Dell hybrid) - details:
- [ ] no - details:

### Debug report
<!-- Toolkit → Report a Bug page → "Generate report" → "Copy report",
     or a terminal:  sudo python3 /opt/tuxthrottle/tuxthrottle.py --debug
     Review it for your username/hostname, then paste between the ``` fences. -->
<details><summary>debug report</summary>

```
PASTE THE DEBUG REPORT HERE
```

</details>

### Screenshot / log console output (if relevant)

)TTCMD";
static const char* kBundleTools = R"TTCMD(nvidia-smi nvidia-settings nvidia-xconfig envycontrol supergfxctl prime-select switcherooctl ubuntu-drivers mokutil asusctl rog-control-center legion_cli ryzenadj intel-undervolt cpupower powerprofilesctl tlp auto-cpufreq thermald openrgb liquidctl corectrl lact gamemoderun mangohud gamescope steam lutris heroic flatpak snap distrobox vulkaninfo glxinfo eglinfo clinfo vainfo vdpauinfo wayland-info xrandr kscreen-doctor wlr-randr libinput evtest upower sensors smartctl nvme fwupdmgr dmidecode acpidump iasl inxi lshw hwinfo ectool smbios-token-ctl smbios-battery-ctl grubby update-grub grub-mkconfig grub2-mkconfig bootctl kernel-install dracut mkinitcpio update-initramfs dnf apt-get pacman emerge zypper rpm-ostree nix)TTCMD";

struct KeyName { int code; const char* name; };
static const KeyName kKeyNames[] = {
    {59, "F1"},
    {60, "F2"},
    {61, "F3"},
    {62, "F4"},
    {63, "F5"},
    {64, "F6"},
    {65, "F7"},
    {66, "F8"},
    {67, "F9"},
    {68, "F10"},
    {87, "F11"},
    {88, "F12"},
    {99, "SYSRQ"},
    {110, "INSERT"},
    {111, "DELETE"},
    {113, "MUTE"},
    {114, "VOLUMEDOWN"},
    {115, "VOLUMEUP"},
    {116, "POWER"},
    {119, "PAUSE"},
    {127, "MENU"},
    {128, "STOP"},
    {140, "CALC"},
    {142, "SLEEP"},
    {143, "WAKEUP"},
    {148, "PROG1"},
    {149, "PROG2"},
    {150, "WWW"},
    {152, "SCREENLOCK"},
    {158, "BACK"},
    {159, "FORWARD"},
    {161, "EJECTCD"},
    {163, "NEXTSONG"},
    {164, "PLAYPAUSE"},
    {165, "PREVIOUSSONG"},
    {166, "STOPCD"},
    {172, "HOMEPAGE"},
    {173, "REFRESH"},
    {183, "F13"},
    {184, "F14"},
    {185, "F15"},
    {186, "F16"},
    {187, "F17"},
    {188, "F18"},
    {189, "F19"},
    {190, "F20"},
    {191, "F21"},
    {192, "F22"},
    {193, "F23"},
    {194, "F24"},
    {202, "PAUSECD"},
    {217, "SEARCH"},
    {224, "BRIGHTNESSDOWN"},
    {225, "BRIGHTNESSUP"},
    {226, "MEDIA"},
    {227, "SWITCHVIDEOMODE"},
    {228, "KBDILLUMTOGGLE"},
    {229, "KBDILLUMDOWN"},
    {230, "KBDILLUMUP"},
    {236, "BATTERY"},
    {238, "WLAN"},
    {239, "UWB"},
    {240, "UNKNOWN"},
    {241, "VIDEO_NEXT"},
    {244, "BRIGHTNESS_AUTO"},
    {245, "DISPLAY_OFF"},
    {246, "WWAN"},
    {247, "RFKILL"},
    {248, "MICMUTE"},
    {418, "SCALE"},
    {431, "ASSISTANT"},
    {464, "FN"},
    {484, "FN_RIGHT_SHIFT"},
    {582, "MICMUTE"},
    {701, "PERFORMANCE (the G-key / G-Mode)"},
};
