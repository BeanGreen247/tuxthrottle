// Proton / Wine / DXVK / runtime manager (port of tuxthrottle_compat_tools.py): GitHub releases, install with
// sha512 verification, Valve tools via Steam, default compat-tool mapping, verify.
#pragma once
#include <map>
#include <set>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::compat {

std::string sha512_hex(const std::string& data);                      // pure; used for download verification
std::string natural_key(const std::string& s);                         // for natural sort ordering (debug)
bool natural_less(const std::string& a, const std::string& b);

// Pure helpers (testable)
std::string pick_arch(bool has_avx2);                                  // x86_64_v3 | x86_64
Json pick_assets(const std::string& tool, const Json& assets, const std::string& arch);  // null when no archive
std::string appinfo_installdir_from(const std::string& bytes, const std::string& appid);
std::map<std::string, std::string> read_mapping(const std::string& config_vdf_text);       // appid -> tool name
std::string write_mapping(const std::string& text, const std::map<std::string, std::string>& changes);   // throws std::runtime_error
std::vector<std::string> check_build(const std::string& path, const std::string& kind);

Json targets();                     // [{name, launcher, present, dir, dirs{}}]
Json status();
Json steam_tools();
Json compat_names();
Json verify();
int main(const std::vector<std::string>& args);

}  // namespace tt::compat
