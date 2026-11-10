// Sensor reads and hardware controls (port of sensors.py). Linux sysfs / hwmon / nvidia-smi / ryzenadj.
#pragma once
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "json.hpp"

namespace tt::sensors {

using Result = std::pair<bool, std::string>;  // ok, message

std::string app_dir();  // install root: $TUXTHROTTLE_HOME, /opt/tuxthrottle, or the source tree

// --- machine identity / model profile (models/<slug>.json) ---
struct ModelInfo {
    std::string vendor, product, board, bios;
    bool is_target = false, is_close = false;
};
ModelInfo detect_model();
const Json& model_profile();
std::string model_id();
bool model_allows(const std::vector<std::string>& models);
bool model_skips_tweak(const std::string& tweak_id);

// small hardware lookups shared with other modules
std::string dmi_value(const std::string& name);
std::string hwmon_by_name(const std::string& name);   // "" when absent
std::string amdgpu_card_dir();
std::string nvidia_pci_dir();
std::string battery_dir_with_threshold();

// --- CPU ---
std::optional<double> cpu_freq_ghz();   // peak core
std::optional<double> cpu_temp_c();
std::string cpu_model_name();
std::optional<double> cpu_power_watts();  // RAPL delta over ~100 ms
bool rapl_permissions_ok();
bool cpu_is_amd();
std::optional<std::string> amd_pstate_mode();

// --- GPUs ---
struct GpuDevice { std::string name, pci; };
struct DrmGpu {
    std::string card, render, pci, vendor, driver, kind;
    bool boot_vga = false;
};
struct VramRow {
    std::string name, pci, driver, kind;
    std::optional<long> used_mb, total_mb, gtt_used_mb;
    std::optional<int> pct;
    bool asleep = false;
};
struct VramConsumer { long pid = 0; std::string comm, driver; double vram_mb = 0; };
struct DgpuValues { std::optional<int> clock_mhz, temp_c, util_pct; std::optional<double> power_w; };
struct IgpuValues { std::optional<int> clock_mhz; std::optional<double> temp_c; };

bool has_amd_gpu();
bool has_nvidia_gpu();
std::vector<std::string> gpu_names();
std::vector<GpuDevice> gpu_devices();
std::string norm_pci(const std::string& addr);
std::vector<std::string> mangohud_gpu_order();
std::vector<std::string> gpu_label_pci_map(const std::vector<std::string>& labels, const std::vector<GpuDevice>* devices = nullptr);
std::vector<DrmGpu> drm_gpus();
std::vector<VramRow> vram_info();
std::vector<VramConsumer> vram_consumers(size_t limit = 12);
IgpuValues igpu_values();
bool dgpu_is_awake();
DgpuValues dgpu_values();
struct RuntimePm { std::string control, status; };
std::optional<RuntimePm> nvidia_runtime_pm();
Result set_nvidia_runtime_pm(bool automatic);

struct NvPowerLimit { bool supported = false; std::optional<int> current, def; int min = 1, max = 100; };
std::optional<NvPowerLimit> nvidia_power_limit_info();
Result set_nvidia_power_limit(int watts);
struct NvClockInfo { bool supported = false; int gr_min = 210; std::optional<int> gr_max, gr_cur, mem_max, mem_cur; };
std::optional<NvClockInfo> nvidia_clock_info();
Result set_nvidia_clock_lock(int gr_min, int gr_max);
Result reset_nvidia_clocks();
struct NvOffsetInfo {
    bool available = false;
    std::string reason;
    std::optional<int> core, mem;
    std::pair<int, int> core_range{-200, 1000};
};
NvOffsetInfo nvidia_clock_offset_info();
Result set_nvidia_clock_offset(int core_mhz, int mem_mhz);
bool envycontrol_available();
std::optional<std::string> gpu_mode_get();
Result gpu_mode_set(const std::string& mode);
struct PowerdStatus { bool installed = false, active = false; };
PowerdStatus nvidia_powerd_status();

// --- power profile / game mode ---
std::string active_power_profile();
bool game_mode_state();
void notify(const std::string& summary, const std::string& body = "");
Result set_game_mode(bool enable);
Result toggle_game_mode();
struct GameModeStatus { bool installed = false, active = false; int clients = 0; };
GameModeStatus gamemode_status();

// --- fans ---
inline constexpr int PWM_FLOOR = 77;
struct Fan { int index = 0; std::string label; int rpm = 0, max = 0; std::optional<int> boost; };
std::vector<Fan> read_fans();
std::vector<int> get_fan_boost();
Result set_fan_boost(int index, int value);
std::vector<std::string> platform_profile_choices();
std::string get_platform_profile();
Result set_platform_profile(const std::string& name);
struct PwmState { std::optional<int> enable, pwm; };
std::vector<PwmState> get_pwm_state();
Result set_pwm_manual(int index, int value);
Result restore_fan_auto();

// --- ryzenadj ---
bool ryzenadj_available();
struct RyzenInfo {  // watts / degC; missing rows stay nullopt
    std::optional<double> stapm_limit, fast_limit, slow_limit, tctl_limit, stapm_value, fast_value, slow_value, tctl_value;
    bool any() const {
        return stapm_limit || fast_limit || slow_limit || tctl_limit || stapm_value || fast_value || slow_value || tctl_value;
    }
};
std::optional<RyzenInfo> read_ryzenadj_info();
Result set_ryzenadj_limits(std::optional<double> fast_w, std::optional<double> slow_w, std::optional<double> stapm_w);
bool ryzenadj_co_supported();
Result set_co_offset(int all_core);

// --- battery ---
struct ChargeLimitInfo {
    bool supported = false;
    std::string method;  // sysfs | libsmbios | ""
    std::optional<int> current, capacity;
    std::optional<bool> ac_online;
    bool dell_libsmbios_possible = false;
};
ChargeLimitInfo battery_charge_limit_info();
Result set_battery_charge_limit(int percent);
std::optional<std::string> battery_charge_mode();
Result set_battery_charge_mode(const std::string& mode);
struct BatteryHealth {
    bool present = false;
    std::string manufacturer, model, technology, status, unit;
    std::optional<int> capacity_pct, cycle_count, eta_min;
    std::optional<double> full, design, wear_pct, power_w, voltage_v;
    std::string eta_kind;
};
BatteryHealth battery_health_info();

// --- display / input ---
struct VrrStatus { std::vector<std::string> capable; int panels = 0; };
VrrStatus vrr_status();
struct PanelMode { std::string id; int w = 0, h = 0; double hz = 0; };
struct PanelModes {
    std::string output, current_id;
    std::optional<double> current_hz;
    std::vector<PanelMode> modes;
    std::vector<int> rates;
};
std::optional<PanelModes> panel_modes();
Result set_panel_refresh(int hz);
struct TouchpadInfo {
    bool available = false;
    std::string name;
    std::optional<bool> enabled, tap_to_click, natural_scroll, disable_while_typing;
};
TouchpadInfo touchpad_info();
Result set_touchpad_enabled(bool on);
Result set_touchpad_tap_to_click(bool on);
Result set_touchpad_natural_scroll(bool on);
Result set_touchpad_disable_while_typing(bool on);

// --- backends: which implementation answers each area on this machine ---
struct Backend { std::string area, name; bool ok = true; };
std::vector<Backend> backends();               // cached for a few seconds
bool daemon_running();                          // control socket present and a tuxthrottled process alive
std::string backend_summary();                  // "daemon · OpenRGB · NVML · ryzenadj"

// --- session helpers ---
void set_session_user(const std::string& name);
std::vector<std::string> session_cmd(const std::vector<std::string>& argv);
bool session_ready();

// --- cheap before/after reading ---
struct SnapshotLight {
    std::optional<double> cpu_temp_c, cpu_freq_ghz, stapm_w, dgpu_temp_c, dgpu_power_w;
    std::optional<int> dgpu_clock_mhz;
    std::vector<int> fan_rpm;
};
SnapshotLight snapshot_light();

}  // namespace tt::sensors
