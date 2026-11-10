// Per-game frame-rate limits, ProtonDB lookups and the shared fix-event log.
#pragma once
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::fpslimit {
std::map<std::string, std::string> env_for(int fps, const std::string& method);   // strangle | dxvk
std::string env_string(int fps, const std::string& method);
std::string norm_name(const std::string& name);                                    // sorted word set, lower-case
std::pair<std::optional<int>, std::string> recommended(const std::string& name);   // from config/fps_limits.json
std::string steam_opts_with(const std::string& opts, const std::map<std::string, std::string>* env);  // add/strip limiter vars
Json list_games();
int main(const std::vector<std::string>& args);
}  // namespace tt::fpslimit

namespace tt::protondb {
std::string label(const Json& data);                       // "ProtonDB: Gold" or ""
std::optional<Json> lookup(const std::string& appid, bool use_cache = true);   // uses `curl`; nullopt when unreachable
int main(const std::vector<std::string>& args);
}  // namespace tt::protondb

namespace tt::fixlog {
void log_event(const std::string& source, const std::string& message, const std::string& level = "info", const std::string& user = "");
Json read_recent(int n = 50, const std::string& user = "");
int main(const std::vector<std::string>& args);
}  // namespace tt::fixlog
