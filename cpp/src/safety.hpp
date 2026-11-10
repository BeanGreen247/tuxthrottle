// Safety nets: Btrfs/snapper snapshots before risky changes, and the confirm-or-auto-revert watchdog.
#pragma once
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::btrfs {
bool is_btrfs_root();
bool snapper_available();
std::optional<std::string> method();  // "snapper" | nullopt
bool available();
struct Snapshot { bool ok = false; std::string method, id, msg; };
Snapshot create_snapshot(const std::string& description = "pre-apply");
struct Row { std::string number, date, description; };
std::vector<Row> list_snapshots(size_t limit = 10);
std::string rollback_hint(const std::string& number);
}  // namespace tt::btrfs

namespace tt::watchdog {
inline const char* kUnitPrefix = "tuxthrottle-watchdog-";
// Throws std::runtime_error when systemd-run is unusable; the returned unit name goes to disarm/is_armed.
std::string arm(int seconds, const std::string& user, const std::string& ctl_path = "");
bool disarm(const std::string& unit);
bool is_armed(const std::string& unit);
}  // namespace tt::watchdog
