// tuxthrottled: fan curve, AC/battery auto-switch, per-game profiles, schedule, thermal events, control socket.
#pragma once
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "json.hpp"

namespace tt::powerd {

Json default_config();
Json load_config(const std::string& path);
Json merge_config(const Json& base, const Json& over);

// Piecewise-linear temp -> boost% (clamped at both ends).
int interp(const std::vector<std::pair<double, double>>& points, double temp);
std::vector<std::pair<double, double>> parse_points(const Json& pts);

struct Bundle { int stapm, fast, slow; };
std::optional<Bundle> tdp_preset(const std::string& name);  // Quiet / Balanced / Performance
bool hhmm_to_min(const std::string& s, int* out);
// Whether `cur` (minutes since midnight) is inside [from, to), wrapping past midnight when from > to.
bool within_window(int from, int to, int cur);

int run(const std::string& config_path, const std::string& user, bool once);

}  // namespace tt::powerd
