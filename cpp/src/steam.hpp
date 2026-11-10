// Steam / Proton helpers: libraries, prefix relocation, save vault, shader cache management.
#pragma once
#include <filesystem>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

#include "json.hpp"

namespace tt::steam {

namespace fs = std::filesystem;

// --- libraries ---
std::optional<fs::path> steam_root_opt();                    // first of ~/.local/share/Steam, ~/.steam/steam, ~/.steam/root
std::vector<fs::path> all_libraries(const fs::path& root);
std::optional<fs::path> library_for_appid(const fs::path& root, const std::string& appid);
std::string appid_name(const fs::path& root, const std::string& appid);
bool colon_ok(const fs::path& dir);
std::pair<std::string, fs::path> classify(const fs::path& lib, const std::string& appid);  // symlink|absent|ok|needs-fix
bool steam_running();
struct Prefix { std::string appid, name, status; fs::path lib; };
std::vector<Prefix> all_prefixes(const fs::path& root);
bool symlink_ok(const fs::path& dir);                        // dir's filesystem takes symlinks (".", "/") and ':' names
std::string lutris_yml_prefix(const std::string& yml);       // game: prefix: value of a Lutris game yml, "" if none
std::string lutris_yml_retarget(const std::string& yml, const std::string& from, const std::string& to);
std::string expand_home(const std::string& p);               // leading ~ -> $HOME

// Pure parsers (testable).
std::vector<std::string> parse_library_paths(const std::string& vdf_text);
std::string parse_appmanifest_name(const std::string& acf_text);

// CLI entry points: args after the subcommand word.
int prefix_main(const std::vector<std::string>& args);
int savevault_main(const std::vector<std::string>& args);
int shadercache_main(const std::vector<std::string>& args);

// shader cache programmatic API (used by the GUI)
std::string shadercache_dir();
Json shadercache_status();
std::pair<bool, std::string> shadercache_set(const std::string& dir, std::optional<int> size_gb);
Json steam_link_status();
std::pair<bool, std::string> heal_steam_links();
std::optional<std::string> steam_bg_shaders_status();
std::pair<bool, std::string> set_steam_bg_shaders(bool enabled);
// Rewrites the "EnableShaderBackgroundProcessing" value inside config.vdf text; returns the new text,
// or nullopt when the ShaderCacheManager block is missing.
std::optional<std::string> set_bg_shader_key(const std::string& vdf, bool enabled);

}  // namespace tt::steam
