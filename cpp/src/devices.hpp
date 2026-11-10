// Keyboard backlight (OpenRGB wrapper) and crash/freeze signature recognition.
#pragma once
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::kbd {

struct Rgb { int r = 255, g = 255, b = 255; };
struct SavedState {
    std::map<int, Rgb> zones;
    int brightness = 100;
};
struct Meta { std::string mode = "zones"; int speed = 50; bool push_accent = false; };

extern const std::map<std::string, std::string> kEffectModes;  // spectrum -> "Spectrum Cycle"
inline constexpr int kZoneCount = 4;

std::string hexify(const std::string& c);          // strips '#', upper-cases
std::string hexify(const Rgb& c);
bool valid_hex(const std::string& s);              // 6 hex digits, optional '#'
std::string state_path();
std::string accent_hex(const std::string& fallback = "FF8800");
void save_state(const std::map<int, std::string>& zone_colors, int brightness, const std::string& mode = "zones", int speed = 50,
                std::optional<bool> push_accent = std::nullopt);
std::optional<SavedState> load_state();
Meta load_meta();
std::vector<long> openrgb_server_pids(const std::string& proc = "/proc");
bool restart_server(bool force = false);

// Each throws std::runtime_error with the OpenRGB failure text.
void set_all(const std::string& color, int brightness = 100);
void set_zone(int zone, const std::string& color, int brightness = 100);
void set_zones(const std::map<int, std::string>& colors, int brightness = 100);
void off();
void set_effect(const std::string& name, std::optional<int> speed = std::nullopt, int brightness = 100);
void reset();
Json info();
int apply_saved(bool restart_server_first = false, bool quick = false);   // quick: skip the wait-for-device probe (guard re-asserts)
int guard();   // long-running watchdog: re-asserts the saved lighting after resume, USB re-enumeration, server restarts and power-source changes
int main(const std::vector<std::string>& args);  // on | off | zone | effect | apply-saved | reset | info

}  // namespace tt::kbd

namespace tt::crashwatch {
struct Finding {
    std::string id, label, hint, detail;
    bool benign = false;
};
using SteamHook = std::function<std::optional<Finding>(const Finding&)>;
void set_steam_double_start_hook(SteamHook h);  // supplied by the steamperf module

// Pure classification of a coredump (exe path + command line) against the signature table.
std::optional<Finding> classify(const std::string& exe, const std::string& cmdline);
std::vector<Finding> scan(int since_seconds = 120, const std::string& user = "");
int main(const std::vector<std::string>& args);  // scan [--since S] [--json] [--notify] [--user U]
}  // namespace tt::crashwatch
