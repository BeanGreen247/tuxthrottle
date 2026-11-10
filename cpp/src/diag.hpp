// Debug report + hardware onboarding bundle (port of tuxthrottle_diag.py and tuxthrottle_hwprobe.py).
#pragma once
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::diag {

std::string redact(const std::string& text, const std::string& user = "");     // strips serials, UUIDs, MACs, IPs, host/user names
std::string collect_debug_report(bool wrap = false);
std::string wrap_issue_block(const std::string& report);
std::string issue_template();
// Writes the .tar.gz and returns its path (throws on failure). `dest_dir` empty = the real user's home.
std::string collect_hw_bundle(const std::string& dest_dir = "", bool redact_data = true);
std::string decode_key_caps(const std::string& proc_input_devices);             // pure
Json system_summary();
Json capabilities();
int main(const std::vector<std::string>& args);     // --debug | --report | --collect [dir] [--no-redact]

}  // namespace tt::diag
