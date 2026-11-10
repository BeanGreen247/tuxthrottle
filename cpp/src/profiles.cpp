#include "profiles.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::profiles {

std::string profiles_dir(const std::string& user) { return config_dir(user) + "/profiles"; }
std::string snapshots_dir(const std::string& user) { return config_dir(user) + "/snapshots"; }

void write_config(const std::string& name, const Json* data, const std::string& user) {
    std::string p = config_dir(user) + "/" + name;
    if (!data) ::unlink(p.c_str());
    else write_json_file(p, *data, user);
}

static Json obj_kv(std::initializer_list<std::pair<const char*, Json>> kv) {
    Json o = Json::obj();
    for (const auto& p : kv) o.set(p.first, p.second);
    return o;
}

Json capture_state(const std::string& user) {
    Json st = Json::obj();
    st.set("captured", timestamp("%Y-%m-%d %H:%M:%S"));
    std::string pp = sensors::get_platform_profile();
    if (!pp.empty()) st.set("platform_profile", pp);

    if (auto tdp = sensors::read_ryzenadj_info(); tdp && tdp->stapm_limit && *tdp->stapm_limit != 0) {
        double st_w = *tdp->stapm_limit;
        st.set("tdp", obj_kv({{"stapm", std::round(st_w)},
                              {"fast", std::round(tdp->fast_limit.value_or(st_w))},
                              {"slow", std::round(tdp->slow_limit.value_or(st_w))}}));
    }
    auto bat = sensors::battery_charge_limit_info();
    if (bat.current) st.set("battery", obj_kv({{"percent", *bat.current}}));
    auto nvpl = sensors::nvidia_power_limit_info();
    if (nvpl && nvpl->supported && nvpl->current && *nvpl->current) st.set("nvpl", obj_kv({{"watts", *nvpl->current}}));
    auto pm = sensors::panel_modes();
    if (pm && pm->current_hz && *pm->current_hz != 0) st.set("refresh_hz", static_cast<int>(std::lround(*pm->current_hz)));

    Json nvclk = read_json_file(config_dir(user) + "/nvclk.json");
    if (nvclk["gr_max"].as_number() != 0) {
        int hi = static_cast<int>(nvclk["gr_max"].as_number());
        int lo = static_cast<int>(nvclk["gr_min"].as_number());
        st.set("nvclk", obj_kv({{"gr_min", lo ? lo : hi}, {"gr_max", hi}}));
    }
    if (auto gm = sensors::gpu_mode_get()) st.set("gpu_mode", *gm);

    Json powerd = read_json_file(config_dir(user) + "/powerd.json");
    for (const char* k : {"fan_curve", "autoswitch", "game_profiles"})
        if (powerd[k].is_object()) st.set(k, powerd[k]);
    Json kbd = read_json_file(config_dir(user) + "/kbd.json");
    if (!kbd.object().empty()) st.set("kbd", kbd);
    return st;
}

static std::optional<double> opt_num(const Json& v) {
    return v.is_number() ? std::optional<double>(v.as_number()) : std::nullopt;
}

std::vector<ResultRow> apply_state(const Json& state, const std::string& user, bool with_gpu_mode) {
    std::vector<ResultRow> results;
    auto rec = [&](const char* key, bool ok, const std::string& msg = "") { results.push_back({key, ok, msg}); };

    if (state.has("platform_profile")) {
        auto r = sensors::set_platform_profile(state["platform_profile"].str());
        rec("platform_profile", r.first, r.second);
    }
    if (state.has("tdp")) {
        const Json& t = state["tdp"];
        auto r = sensors::set_ryzenadj_limits(opt_num(t["fast"]), opt_num(t["slow"]), opt_num(t["stapm"]));
        rec("tdp", r.first, r.second);
    }
    if (state.has("battery")) {
        auto r = sensors::set_battery_charge_limit(static_cast<int>(state["battery"]["percent"].as_number()));
        rec("battery", r.first, r.second);
    }
    if (state.has("nvpl")) {
        auto info = sensors::nvidia_power_limit_info();
        if (info && !info->supported) rec("nvpl", true, "skipped - firmware-locked on this GPU");
        else {
            auto r = sensors::set_nvidia_power_limit(static_cast<int>(state["nvpl"]["watts"].as_number()));
            rec("nvpl", r.first, r.second);
        }
    }
    if (state.has("refresh_hz")) {
        auto r = sensors::set_panel_refresh(static_cast<int>(state["refresh_hz"].as_number()));
        rec("refresh_hz", r.first, r.second);
    }
    if (state.has("nvclk")) {
        const Json& n = state["nvclk"];
        int hi = static_cast<int>(n["gr_max"].as_number());
        int lo = static_cast<int>(n["gr_min"].as_number());
        if (!lo) lo = hi;
        if (!sensors::nvidia_clock_info()) rec("nvclk", true, "dGPU asleep - saved for the boot service");
        else {
            auto r = sensors::set_nvidia_clock_lock(lo, hi);
            rec("nvclk", r.first, r.second);
        }
        write_json_file(config_dir(user) + "/nvclk.json", obj_kv({{"gr_min", lo}, {"gr_max", hi}}), user);
    }
    if (with_gpu_mode && !state["gpu_mode"].str().empty()) {
        auto cur = sensors::gpu_mode_get();
        if (!cur || *cur != state["gpu_mode"].str()) {
            auto r = sensors::gpu_mode_set(state["gpu_mode"].str());
            rec("gpu_mode", r.first, r.first ? r.second + " (log out to apply)" : r.second);
        }
    }
    Json pd_keys = Json::obj();
    std::vector<std::string> names;
    for (const char* k : {"fan_curve", "autoswitch", "game_profiles"})
        if (state.has(k)) { pd_keys.set(k, state[k]); names.push_back(k); }
    if (!names.empty()) {
        Json pd = read_json_file(config_dir(user) + "/powerd.json");
        for (const auto& kv : pd_keys.object()) pd.set(kv.first, kv.second);
        write_json_file(config_dir(user) + "/powerd.json", pd, user);
        rec("powerd", true, join(names, ", "));
    }
    if (state.has("kbd")) {
        write_json_file(config_dir(user) + "/kbd.json", state["kbd"], user);
        rec("kbd", true, "saved (apply via the Keyboard tab / boot service)");
    }
    Json active = state;
    active.set("applied", timestamp("%Y-%m-%d %H:%M:%S"));
    write_json_file(config_dir(user) + "/active_state.json", active, user);
    return results;
}

std::string safe_name(const std::string& name) {
    std::string out;
    for (char c : name)
        if (std::isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_' || c == ' ') out += c;
    out = trim(out);
    return out.empty() ? "unnamed" : out;
}

static std::vector<std::string> json_files(const std::string& dir) {
    auto v = glob_paths(dir + "/*.json");
    std::sort(v.begin(), v.end());
    return v;
}

std::vector<std::string> list_profiles(const std::string& user) {
    std::vector<std::string> out;
    for (const auto& p : json_files(profiles_dir(user))) {
        std::string b = base_name(p);
        out.push_back(b.substr(0, b.size() - 5));
    }
    return out;
}

Json load_profile(const std::string& name, const std::string& user) {
    return read_json_file(profiles_dir(user) + "/" + safe_name(name) + ".json");
}

std::string save_profile(const std::string& name, const Json& state, const std::string& user) {
    std::string p = profiles_dir(user) + "/" + safe_name(name) + ".json";
    write_json_file(p, state, user);
    return p;
}

bool delete_profile(const std::string& name, const std::string& user) {
    return ::unlink((profiles_dir(user) + "/" + safe_name(name) + ".json").c_str()) == 0;
}

std::string export_profile(const std::string& name, const std::string& dest, const std::string& user) {
    Json st = load_profile(name, user);
    if (st.object().empty()) throw std::runtime_error("no such profile: " + name);
    Json payload = Json::obj();
    payload.set(kExportMarker, 1);
    payload.set("name", name);
    for (const auto& kv : st.object()) payload.set(kv.first, kv.second);
    mkdir_p(dir_name(dest));
    write_file(dest, payload.dump(2, true));
    return dest;
}

std::string import_profile(const std::string& src, const std::string& name, const std::string& user) {
    Json data = read_json_file(src);
    if (data.object().empty() || !data.has(kExportMarker))
        throw std::runtime_error(src + " is not a TuxThrottle profile export (missing schema marker)");
    Json payload = Json::obj();
    for (const auto& kv : data.object())
        if (kv.first != kExportMarker && kv.first != "name") payload.set(kv.first, kv.second);
    std::string final_name = !name.empty() ? name : (!data["name"].str().empty() ? data["name"].str() : [&] {
        std::string b = base_name(src);
        size_t dot = b.rfind('.');
        return dot == std::string::npos ? b : b.substr(0, dot);
    }());
    save_profile(final_name, payload, user);
    return final_name;
}

static void prune_snapshots(const std::string& user) {
    auto snaps = json_files(snapshots_dir(user));
    if (static_cast<int>(snaps.size()) <= keep_snapshots) return;
    for (size_t i = 0; i + static_cast<size_t>(keep_snapshots) < snaps.size(); ++i) ::unlink(snaps[i].c_str());
}

std::string snapshot(const std::string& user, const std::string& label) {
    std::string lab = safe_name(label);
    std::replace(lab.begin(), lab.end(), ' ', '-');
    std::string p = snapshots_dir(user) + "/" + timestamp("%Y%m%d-%H%M%S") + "_" + lab + ".json";
    Json st = capture_state(user);
    st.set("label", label);
    write_json_file(p, st, user);
    prune_snapshots(user);
    return p;
}

std::vector<SnapshotInfo> list_snapshots(const std::string& user) {
    std::vector<SnapshotInfo> out;
    auto files = json_files(snapshots_dir(user));
    std::reverse(files.begin(), files.end());
    for (const auto& p : files) {
        Json d = read_json_file(p);
        out.push_back({p, base_name(p), d["label"].is_string() ? d["label"].str() : "?",
                       d["captured"].is_string() ? d["captured"].str() : "?"});
    }
    return out;
}

std::vector<ResultRow> rollback(const std::string& target, const std::string& user, bool with_gpu_mode) {
    std::string path;
    if (target.empty() || target == "last" || target == "latest") {
        auto snaps = json_files(snapshots_dir(user));
        if (snaps.empty()) return {{"-", false, "no snapshots to roll back to"}};
        path = snaps.back();
    } else {
        path = target;
        if (path.empty() || path[0] != '/') path = snapshots_dir(user) + "/" + target;
    }
    Json state = read_json_file(path);
    if (state.object().empty()) return {{"-", false, "unreadable snapshot: " + path}};
    snapshot(user, "pre-rollback");
    return apply_state(state, user, with_gpu_mode);
}

std::vector<ResultRow> reassert(const std::string& user) {
    Json st = read_json_file(config_dir(user) + "/active_state.json");
    if (st.object().empty()) return {{"-", true, "nothing to reassert"}};
    return apply_state(st, user);
}

}  // namespace tt::profiles
