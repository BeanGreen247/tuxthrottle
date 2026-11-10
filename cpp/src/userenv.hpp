// Which login user the tool acts for, and where their TuxThrottle config lives.
#pragma once
#include <string>

#include "json.hpp"

namespace tt {

std::string real_user(const std::string& explicit_user = "");  // --user, SUDO_USER, PKEXEC_*, else current
std::string resolve_real_user();                                // PKEXEC_UID/SUDO_UID first, then names (items.py order)
std::string home_of(const std::string& user);
std::string config_dir(const std::string& user = "");          // ~/.config/tuxthrottle of that user

Json read_json_file(const std::string& path);                  // {} on any error / non-object
bool write_json_file(const std::string& path, const Json& data, const std::string& user = "");
bool mkdir_p(const std::string& path);
void chown_to_user(const std::string& path, const std::string& user);
std::string timestamp(const char* fmt);                        // strftime of "now"

}  // namespace tt
