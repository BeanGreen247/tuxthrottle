// Per-game MangoHud (gate script, flag files, per-game profiles, dedicated-GPU switch) - port of
// tuxthrottle_mangohud_games.py. Plus the MangoHud status line.
#pragma once
#include <map>
#include <set>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::mangohud {

extern const std::vector<std::string> kPositions;
extern const std::vector<std::string> kDetails;
extern const char* kGateScript;

// ---- pure launch-options surgery ----
struct OptState { bool enabled = false; std::string conf; };
OptState state(const std::string& opts);
std::string transform(const std::string& opts, bool enabled, const std::string& conffile = "");
std::string transform_dgpu(const std::string& opts, bool on, bool nvidia);
bool has_dgpu(const std::string& opts);
std::string dgpu_vars(bool nvidia);
std::string render_conf(const std::string& appid, const Json& settings, const std::string& base_text);
std::string put_env(const std::string& s, const std::string& var);

// ---- gate / flags / profiles ----
bool gate_installed();
bool gate_current();
std::string flags_dir();
bool flag_on(const std::string& appid);
void set_flag(const std::string& appid, bool on);
std::string conf_path(const std::string& appid);
std::string profile_state(const std::string& appid);   // on | off | none
Json read_settings(const std::string& appid);
std::string ensure_profile(const std::string& appid, const Json& settings = Json::obj());
void disable_profile(const std::string& appid);
void delete_profile(const std::string& appid);
std::string write_conf(const std::string& appid, const Json& settings);

// ---- games ----
std::map<std::string, std::string> installed_games();      // appid -> name
std::map<std::string, std::string> steam_install_dirs();    // appid -> install dir
Json list_games();
int set_game(const std::string& appid, bool enabled, const Json* settings, bool dry);
int apply_plan(const Json& plan, bool dry);
int hook_all(bool dry);
int disable_all(bool dry);
std::vector<std::string> global_confs();
int global_off();

// ---- status line (MangoHud.conf custom_text_center) ----
namespace status_line {
bool is_enabled(const std::string& user = "");
void set_enabled(bool value, const std::string& user = "");
void set_status_line(const std::string& text, const std::string& user = "");
void clear_status_line(const std::string& user = "");
}

int main(const std::vector<std::string>& args);
int status_main(const std::vector<std::string>& args);

}  // namespace tt::mangohud
