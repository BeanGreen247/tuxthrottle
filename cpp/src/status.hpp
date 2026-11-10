// JSON views of sensor readings (what `tuxthrottlectl status --json` and the daemon socket return).
#pragma once
#include "json.hpp"
#include "sensors.hpp"

namespace tt::status {

Json opt(const std::optional<double>& v);
Json opt(const std::optional<int>& v);
Json fans_json();
Json ryzen_json();                 // null when unavailable
Json charge_limit_json();
Json nvpl_json();                  // null when asleep / missing
Json gamemode_json();
Json gather();                     // full `status` object
Json get(const std::string& what); // `get <what>`
bool ac_online(bool* known);       // /sys/class/power_supply/A[CD]*/online

}  // namespace tt::status
