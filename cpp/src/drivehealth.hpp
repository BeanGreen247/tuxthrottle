// Drive health: SMART, filesystems, kernel log, Steam libraries, plus the safe maintenance actions.
#pragma once
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::drivehealth {

// Level order: ok < note < warn < bad.
Json finding(const std::string& level, const std::string& text);
std::string worst(const Json& findings);

// Pure analysis pieces (testable without hardware).
std::pair<Json, Json> smart_findings(const Json& smartctl_doc);   // {summary, findings}
Json usage_finding(double used_pct, long long free_bytes);        // null when fine
Json btrfs_stats_findings(const std::string& text);
Json btrfs_scrub_finding(const std::string& text);
Json scan_log(const std::string& text);
Json trend_finding(long long boots, long long unsafe, long long cycles, double days);  // null when no data
Json fix_plan(const Json& report);

Json report(const std::string& user = "");
std::string format_report(const Json& rep);
int main(const std::vector<std::string>& args);

}  // namespace tt::drivehealth
