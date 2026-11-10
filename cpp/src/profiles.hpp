// Profiles + snapshots: named full-state bundles (port of tuxthrottle_profiles.py).
#pragma once
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::profiles {

struct ResultRow { std::string key; bool ok = true; std::string msg; };

std::string profiles_dir(const std::string& user = "");
std::string snapshots_dir(const std::string& user = "");
void write_config(const std::string& name, const Json* data, const std::string& user = "");  // data==nullptr deletes

Json capture_state(const std::string& user = "");
std::vector<ResultRow> apply_state(const Json& state, const std::string& user = "", bool with_gpu_mode = false);

std::string safe_name(const std::string& name);
std::vector<std::string> list_profiles(const std::string& user = "");
Json load_profile(const std::string& name, const std::string& user = "");
std::string save_profile(const std::string& name, const Json& state, const std::string& user = "");
bool delete_profile(const std::string& name, const std::string& user = "");

inline constexpr const char* kExportMarker = "_tuxthrottle_profile";
// export throws std::runtime_error when the profile is missing; import when the file isn't an export.
std::string export_profile(const std::string& name, const std::string& dest, const std::string& user = "");
std::string import_profile(const std::string& src, const std::string& name = "", const std::string& user = "");

std::string snapshot(const std::string& user = "", const std::string& label = "auto");
struct SnapshotInfo { std::string path, name, label, captured; };
std::vector<SnapshotInfo> list_snapshots(const std::string& user = "");
std::vector<ResultRow> rollback(const std::string& target, const std::string& user = "", bool with_gpu_mode = false);
std::vector<ResultRow> reassert(const std::string& user = "");

inline int keep_snapshots = 20;

}  // namespace tt::profiles
