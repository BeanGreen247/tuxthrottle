// Game-launcher integration: Steam launch options, Heroic/Lutris bulk options, shader-cache env for Lutris,
// installed-game discovery for the MangoHud tab, and the offline anti-cheat scan.
#pragma once
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::launchers {

// ---- shared launch-options string parsing (Steam syntax: VAR=val ... wrapper %command%) ----
struct ParsedOpts {
    std::map<std::string, std::string> env;
    std::map<std::string, bool> flags;     // useGameMode / showMangohud
    std::vector<std::string> wrappers;
};
ParsedOpts parse_opts(const std::string& opts);       // throws std::invalid_argument on bad quoting
std::string expand_vars(const std::string& s);          // $VAR and ${VAR}

// ---- CLI entry points (args after the subcommand word) ----
int launchopts_main(const std::vector<std::string>& args);   // Steam localconfig.vdf
int heroicopts_main(const std::vector<std::string>& args);
int lutrisopts_main(const std::vector<std::string>& args);
int lutris_env_main(const std::vector<std::string>& args);

// ---- Steam per-game LaunchOptions access (used by the FPS-limit module) ----
std::vector<std::string> steam_game_ids();                                  // appids present in any localconfig.vdf
std::string steam_get_launch_options(const std::string& appid);              // "" when unset/unknown
// Applies `transform` to the LaunchOptions of one appid in every localconfig that lists it; backs up and
// rewrites only changed files (unless dry). Returns true when anything changed.
bool steam_update_launch_options(const std::string& appid, const std::function<std::string(const std::string&)>& transform, bool dry);

// One pass over every localconfig.vdf (the `_rewrite` of the MangoHud module): `appids` picks the games to touch from
// the ids already present, `change(appid, current) -> new`. Prints the same summary lines as the reference; returns
// 0 ok, 1 no localconfig, 2 Steam running.
int steam_rewrite(const std::function<std::vector<std::string>(const std::vector<std::string>&)>& appids,
                  const std::function<std::string(const std::string&, const std::string&)>& change, bool dry);
std::vector<std::string> steam_roots_list();            // Steam root dirs (native + Flatpak)
bool steam_is_running();

// ---- pure helpers exposed for tests ----
std::string steam_baseline(const std::vector<std::string>& launch_options);      // most common non-empty value
std::map<std::string, std::string> lutris_wanted_env(const std::string& cache_dir);

// ---- installed-game discovery (MangoHud tab) ----
struct OtherGame {
    std::string id, source, appid, name, path, yml, base;
    bool wanted = false, enabled = false, hook = true;
};
std::vector<OtherGame> heroic_games();
std::vector<OtherGame> lutris_games();
std::vector<OtherGame> other_games();
bool set_game_mangohud(const std::string& game_id, bool on);

// ---- anti-cheat scan ----
std::string scan_anticheat(const std::string& path);                       // "" = none
std::map<std::string, std::string> anticheat_for(const std::map<std::string, std::string>& paths);

}  // namespace tt::launchers
