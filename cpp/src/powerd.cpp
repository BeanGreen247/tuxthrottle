#include "powerd.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <set>

#include "control.hpp"
#include "profiles.hpp"
#include "sensors.hpp"
#include "status.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::powerd {

static std::atomic<bool> g_stop{false};
static std::atomic<bool> g_reload{false};

static void log(const std::string& msg) {
    std::printf("[powerd] %s\n", msg.c_str());
    std::fflush(stdout);
}

static Json points_json(std::initializer_list<std::pair<int, int>> pts) {
    Json a = Json::arr();
    for (auto& p : pts) {
        Json pair = Json::arr();
        pair.push(p.first);
        pair.push(p.second);
        a.push(pair);
    }
    return a;
}

Json default_config() {
    Json c = Json::obj();
    c.set("poll_s", 2);
    Json fc = Json::obj();
    fc.set("enabled", false);
    fc.set("sensor", "max");
    fc.set("hysteresis_c", 3);
    fc.set("points", points_json({{45, 0}, {60, 25}, {72, 55}, {82, 85}, {90, 100}}));
    c.set("fan_curve", fc);
    Json as = Json::obj();
    as.set("enabled", false);
    as.set("on_ac", "Balanced");
    as.set("on_battery", "Quiet");
    as.set("refresh_ac", 0);
    as.set("refresh_battery", 0);
    c.set("autoswitch", as);
    Json gp = Json::obj();
    gp.set("enabled", false);
    gp.set("poll_s", 6);
    gp.set("match", Json::obj());
    gp.set("default", Json());
    c.set("game_profiles", gp);
    Json sc = Json::obj();
    sc.set("enabled", false);
    sc.set("poll_s", 60);
    sc.set("rules", Json::arr());
    sc.set("outside", Json());
    c.set("schedule", sc);
    Json tn = Json::obj();
    tn.set("enabled", false);
    tn.set("tjmax_c", 95);
    tn.set("tjmax_sustain_s", 20);
    tn.set("stalled_fan_hot_c", 70);
    tn.set("battery_perf_min_pct", 20);
    tn.set("cooldown_s", 300);
    tn.set("stalled_fan_recover", false);
    tn.set("stalled_fan_recover_s", 45);
    c.set("thermal_notify", tn);
    return c;
}

Json merge_config(const Json& base, const Json& over) {
    Json out = base;
    for (const auto& kv : over.object()) {
        if (kv.second.is_object() && out[kv.first].is_object()) {
            Json merged = out[kv.first];
            for (const auto& k2 : kv.second.object()) merged.set(k2.first, k2.second);
            out.set(kv.first, merged);
        } else {
            out.set(kv.first, kv.second);
        }
    }
    return out;
}

Json load_config(const std::string& path) {
    auto t = read_file(path);
    if (!t) return default_config();
    try {
        Json raw = Json::parse(*t);
        if (!raw.is_object()) return default_config();
        return merge_config(default_config(), raw);
    } catch (const std::exception&) {
        return default_config();
    }
}

std::vector<std::pair<double, double>> parse_points(const Json& pts) {
    std::vector<std::pair<double, double>> out;
    for (const auto& p : pts.array())
        if (p.array().size() >= 2) out.emplace_back(p.array()[0].as_number(), p.array()[1].as_number());
    return out;
}

int interp(const std::vector<std::pair<double, double>>& points, double temp) {
    auto pts = points;
    std::sort(pts.begin(), pts.end());
    if (pts.empty()) return 0;
    if (temp <= pts.front().first) return static_cast<int>(std::lround(pts.front().second));
    if (temp >= pts.back().first) return static_cast<int>(std::lround(pts.back().second));
    for (size_t i = 0; i + 1 < pts.size(); ++i) {
        auto [t0, b0] = pts[i];
        auto [t1, b1] = pts[i + 1];
        if (t0 <= temp && temp <= t1) {
            double frac = t1 != t0 ? (temp - t0) / (t1 - t0) : 0.0;
            return static_cast<int>(std::lround(b0 + frac * (b1 - b0)));
        }
    }
    return static_cast<int>(std::lround(pts.back().second));
}

std::optional<Bundle> tdp_preset(const std::string& name) {
    if (name == "Quiet") return Bundle{25, 35, 25};
    if (name == "Balanced") return Bundle{42, 54, 42};
    if (name == "Performance") return Bundle{65, 80, 54};
    return std::nullopt;
}

static std::string profile_for_bundle(const std::string& name) {
    if (name == "Quiet" || name == "Balanced") return "balanced";
    if (name == "Performance") return "performance";
    return "";
}

bool hhmm_to_min(const std::string& s, int* out) {
    size_t c = s.find(':');
    if (c == std::string::npos) return false;
    char *e1 = nullptr, *e2 = nullptr;
    long h = std::strtol(s.c_str(), &e1, 10), m = std::strtol(s.c_str() + c + 1, &e2, 10);
    if (e1 != s.c_str() + c || e2 == s.c_str() + c + 1) return false;
    *out = static_cast<int>(((h % 24) + 24) % 24 * 60 + ((m % 60) + 60) % 60);
    return true;
}

bool within_window(int a, int b, int cur) { return a <= b ? (a <= cur && cur < b) : (cur >= a || cur < b); }

static std::optional<double> read_temp(const std::string& sensor) {
    auto cpu = sensors::cpu_temp_c();
    std::optional<double> gpu;
    auto d = sensors::dgpu_values();
    if (d.temp_c) gpu = static_cast<double>(*d.temp_c);
    if (sensor == "cpu") return cpu;
    if (sensor == "gpu") return gpu;
    if (cpu && gpu) return std::max(*cpu, *gpu);
    return cpu ? cpu : gpu;
}

static void apply_bundle(const std::string& name) {
    auto preset = tdp_preset(name);
    std::string prof = profile_for_bundle(name);
    if (!prof.empty()) {
        auto choices = sensors::platform_profile_choices();
        if (std::find(choices.begin(), choices.end(), prof) != choices.end()) {
            auto r = sensors::set_platform_profile(prof);
            log("bundle " + name + ": platform_profile -> " + prof + (r.first ? "" : " FAILED " + r.second));
        }
    }
    if (preset && sensors::ryzenadj_available()) {
        auto r = sensors::set_ryzenadj_limits(preset->fast, preset->slow, preset->stapm);
        log("bundle " + name + ": TDP -> " + std::to_string(preset->stapm) + "/" + std::to_string(preset->fast) + "/" +
            std::to_string(preset->slow) + " W" + (r.first ? "" : " FAILED " + r.second));
    }
}

static void apply_profile_named(const std::string& name, const std::string& user) {
    Json st = profiles::load_profile(name, user);
    if (st.object().empty()) { log("  (no such profile '" + name + "' - skipped)"); return; }
    for (const auto& r : profiles::apply_state(st, user))
        if (!r.ok) log("  " + r.key + ": FAILED " + r.msg);
}

static void apply_bundle_or_profile(const std::string& name, const std::string& user) {
    if (tdp_preset(name)) apply_bundle(name);
    else apply_profile_named(name, user);
}

static std::set<std::string> running_procs() {
    std::set<std::string> names;
    for (const auto& pdir : glob_paths("/proc/[0-9]*")) {
        std::string comm = lower(read_trim(pdir + "/comm"));
        if (!comm.empty()) names.insert(comm);
        std::string cmd = read_file(pdir + "/cmdline").value_or("");
        std::string arg0 = cmd.substr(0, cmd.find('\0'));
        if (!arg0.empty()) names.insert(lower(base_name(arg0)));
    }
    return names;
}

static std::string session_path(const std::string& user) { return config_dir(user) + "/last_session.json"; }

// ------------------------------------------------------------ game profiles
class GameProfileController {
public:
    explicit GameProfileController(std::string user) : user_(std::move(user)) {}

    void tick(const Json& cfg) {
        const Json& gp = cfg["game_profiles"];
        int poll_s = std::max(3, static_cast<int>(gp["poll_s"].as_number(6)));
        if (!gp["enabled"].as_bool() || gp["match"].object().empty()) {
            if (!active_key_.empty()) leave(gp);
            return;
        }
        std::vector<std::pair<std::string, std::string>> match;
        for (const auto& kv : gp["match"].object()) match.emplace_back(lower(kv.first), kv.second.str());
        auto procs = running_procs();
        std::optional<std::pair<std::string, std::string>> hit;
        for (const auto& [key, prof] : match) {
            if (key == "*") continue;
            std::string noexe = ends_with(key, ".exe") ? key.substr(0, key.size() - 4) : key;
            if (procs.count(key) || procs.count(noexe)) { hit = std::make_pair(key, prof); break; }
        }
        if (!hit)
            for (const auto& [key, prof] : match)
                if (key == "*" && sensors::gamemode_status().active) { hit = std::make_pair(key, prof); break; }
        if (hit && hit->first != active_key_) {
            log("game '" + hit->first + "' detected -> profile '" + hit->second + "' (snapshot first)");
            profiles::snapshot(user_, "pre-game");
            apply_profile_named(hit->second, user_);
            active_key_ = hit->first;
            session_start(hit->first, hit->second);
        } else if (!hit && !active_key_.empty()) {
            leave(gp);
        }
        if (!active_key_.empty() && session_) session_sample(poll_s);
    }

    bool active() const { return !active_key_.empty(); }

private:
    struct Session {
        std::string game, profile;
        double started = 0;
        int n = 0;
        double cpu_temp_max = 0, gpu_temp_max = 0, cpu_clk_sum = 0, gpu_clk_sum = 0, cpu_pow_max = 0, gpu_pow_max = 0,
               throttle_s = 0, sample_s = 0;
        int cpu_clk_n = 0, gpu_clk_n = 0;
    };

    void leave(const Json& gp) {
        std::string dflt = gp["default"].str();
        if (!dflt.empty()) {
            log("game gone -> default profile '" + dflt + "'");
            apply_profile_named(dflt, user_);
        } else {
            log("game gone -> rolling back the pre-game snapshot");
            profiles::rollback("last", user_);
        }
        session_end();
        active_key_.clear();
    }

    void session_start(const std::string& key, const std::string& prof) {
        Session s;
        s.game = key;
        s.profile = prof;
        s.started = static_cast<double>(std::time(nullptr));
        session_ = s;
    }

    void session_sample(double poll_s) {
        Session& s = *session_;
        ++s.n;
        s.sample_s += poll_s;
        auto info = sensors::read_ryzenadj_info();
        if (info && info->tctl_value) {
            s.cpu_temp_max = std::max(s.cpu_temp_max, *info->tctl_value);
            if (*info->tctl_value >= 90) s.throttle_s += poll_s;
        }
        if (auto pw = sensors::cpu_power_watts()) s.cpu_pow_max = std::max(s.cpu_pow_max, *pw);
        if (auto clk = sensors::cpu_freq_ghz(); clk && *clk > 0) { s.cpu_clk_sum += *clk; ++s.cpu_clk_n; }
        auto d = sensors::dgpu_values();
        if (d.temp_c) s.gpu_temp_max = std::max(s.gpu_temp_max, static_cast<double>(*d.temp_c));
        if (d.clock_mhz && *d.clock_mhz) { s.gpu_clk_sum += *d.clock_mhz; ++s.gpu_clk_n; }
        if (d.power_w) s.gpu_pow_max = std::max(s.gpu_pow_max, *d.power_w);
    }

    void session_end() {
        if (!session_) return;
        Session s = *session_;
        session_.reset();
        if (s.n < 2) return;
        double ended = static_cast<double>(std::time(nullptr));
        double dur = std::max(1.0, ended - s.started);
        Json sum = Json::obj();
        sum.set("game", s.game);
        sum.set("profile", s.profile);
        sum.set("started", s.started);
        sum.set("ended", ended);
        sum.set("duration_s", std::round(dur));
        sum.set("cpu_temp_max_c", std::round(s.cpu_temp_max));
        sum.set("gpu_temp_max_c", std::round(s.gpu_temp_max));
        sum.set("cpu_clock_avg_ghz", s.cpu_clk_n ? Json(std::round(s.cpu_clk_sum / s.cpu_clk_n * 100) / 100) : Json());
        sum.set("gpu_clock_avg_mhz", s.gpu_clk_n ? Json(std::round(s.gpu_clk_sum / s.gpu_clk_n)) : Json());
        sum.set("cpu_power_max_w", s.cpu_pow_max ? Json(std::round(s.cpu_pow_max)) : Json());
        sum.set("gpu_power_max_w", s.gpu_pow_max ? Json(std::round(s.gpu_pow_max)) : Json());
        sum.set("throttle_s", std::round(s.throttle_s));
        double pct = std::round(100 * s.throttle_s / std::max(1.0, s.sample_s));
        sum.set("throttle_pct", pct);
        std::string p = session_path(user_);
        mkdir_p(dir_name(p));
        write_file(p, sum.dump(2));
        chown_to_user(p, user_);
        std::string hp = dir_name(p) + "/sessions.jsonl";
        auto lines = split_lines(read_file(hp).value_or(""));
        lines.push_back(sum.dump());
        if (lines.size() > 50) lines.erase(lines.begin(), lines.end() - 50);
        std::string tmp = hp + ".tmp";
        write_file(tmp, join(lines, "\n") + "\n");
        ::rename(tmp.c_str(), hp.c_str());
        chown_to_user(hp, user_);
        log("session summary: " + s.game + " " + std::to_string(static_cast<int>(dur)) + "s, CPU max " +
            std::to_string(static_cast<int>(std::round(s.cpu_temp_max))) + "C, throttled " +
            std::to_string(static_cast<int>(pct)) + "% -> last_session.json (+sessions.jsonl)");
    }

    std::string user_, active_key_;
    std::optional<Session> session_;
};

// ------------------------------------------------------------ schedule
class ScheduleController {
public:
    explicit ScheduleController(std::string user) : user_(std::move(user)) {}
    void tick(const Json& cfg, bool game_active) {
        const Json& sc = cfg["schedule"];
        if (!sc["enabled"].as_bool()) { last_.clear(); have_last_ = false; return; }
        double poll_s = std::max(15, static_cast<int>(sc["poll_s"].as_number(60)));
        double now = now_seconds();
        if (have_last_ && now - last_check_ < poll_s) return;
        last_check_ = now;
        if (game_active) return;
        std::time_t t = std::time(nullptr);
        std::tm lt;
        localtime_r(&t, &lt);
        int cur = lt.tm_hour * 60 + lt.tm_min;
        int pyw = (lt.tm_wday + 6) % 7;  // tm_wday: 0=Sun; Python weekday: 0=Mon
        std::string target = sc["outside"].str();
        for (const auto& rule : sc["rules"].array()) {
            const auto& days = rule["days"].array();
            if (!days.empty()) {
                bool in = false;
                for (const auto& d : days) in = in || static_cast<int>(d.as_number(-1)) == pyw;
                if (!in) continue;
            }
            int a, b;
            if (!hhmm_to_min(rule["from"].str(), &a) || !hhmm_to_min(rule["to"].str(), &b)) continue;
            if (within_window(a, b, cur)) { target = rule["apply"].str(); break; }
        }
        if (target.empty() || (have_last_ && target == last_)) return;
        log("schedule -> apply '" + target + "'");
        apply_bundle_or_profile(target, user_);
        last_ = target;
        have_last_ = true;
    }

private:
    std::string user_, last_;
    bool have_last_ = false;
    double last_check_ = 0;
};

// ------------------------------------------------------------ fan curve
class FanController {
public:
    void tick(const Json& cfg) {
        const Json& fc = cfg["fan_curve"];
        if (!fc["enabled"].as_bool()) {
            if (active_) {
                sensors::restore_fan_auto();
                log("fan curve disabled -> restored automatic control");
                active_ = false;
                applied_.reset();
            }
            return;
        }
        auto temp = read_temp(fc["sensor"].str().empty() ? "max" : fc["sensor"].str());
        if (!temp) return;
        auto pts = parse_points(fc["points"]);
        if (pts.empty()) pts = parse_points(default_config()["fan_curve"]["points"]);
        double hys = fc["hysteresis_c"].as_number(3);
        int target = interp(pts, *temp);
        if (!applied_) { commit(target, *temp); return; }
        if (target >= *applied_) {
            if (target != *applied_) commit(target, *temp);
        } else if (interp(pts, *temp + hys) < *applied_) {
            commit(target, *temp);
        }
    }
    void restore() {
        if (active_) {
            sensors::restore_fan_auto();
            log("exiting -> restored automatic fan control");
        }
    }

private:
    void commit(int pct, double temp) {
        int raw = std::max(0, std::min(255, static_cast<int>(std::lround(pct * 255.0 / 100.0))));
        for (int i : {1, 2}) sensors::set_fan_boost(i, raw);
        applied_ = pct;
        active_ = true;
        log("temp " + std::to_string(static_cast<int>(std::lround(temp))) + "C -> fan boost " + std::to_string(pct) + "%");
    }
    std::optional<int> applied_;
    bool active_ = false;
};

// ------------------------------------------------------------ thermal watcher
class ThermalWatcher {
public:
    explicit ThermalWatcher(std::string user) : user_(std::move(user)) {}
    void tick(const Json& cfg) {
        const Json& tn = cfg["thermal_notify"];
        if (!tn["enabled"].as_bool()) { hot_since_ = 0; return; }
        double cd = tn["cooldown_s"].as_number(300);
        auto info = sensors::read_ryzenadj_info();
        double tjmax = tn["tjmax_c"].as_number(95);
        if (info && info->tctl_value && *info->tctl_value >= tjmax) {
            if (hot_since_ == 0) hot_since_ = now_seconds();
            if (now_seconds() - hot_since_ >= tn["tjmax_sustain_s"].as_number(20)) {
                char b[160];
                std::snprintf(b, sizeof b, "Tctl %.0f C >= %.0f C sustained - clocks are being throttled.", *info->tctl_value, tjmax);
                fire("tjmax", cd, "CPU at thermal limit", b);
            }
        } else {
            hot_since_ = 0;
        }
        auto fans = sensors::read_fans();
        auto hot = read_temp("max");
        std::vector<std::string> stalled;
        for (const auto& f : fans)
            if (f.rpm == 0) stalled.push_back(f.label.empty() ? "fan" + std::to_string(f.index) : f.label);
        bool stalled_hot = !stalled.empty() && hot && *hot >= tn["stalled_fan_hot_c"].as_number(70);
        if (stalled_hot) {
            std::string names = join(stalled, ", ");
            char b[200];
            std::snprintf(b, sizeof b, "%s at 0 RPM with a sensor at %.0f C.", names.c_str(), *hot);
            fire("stalled_fan", cd, "Fan not spinning while hot", b);
            if (tn["stalled_fan_recover"].as_bool()) kick_recover(names, tn["stalled_fan_recover_s"].as_number(45));
        } else if (have_prev_ && now_seconds() >= recover_until_) {
            end_recover();
        }
        bool known = false;
        bool ac = status::ac_online(&known);
        if (known && !ac && !have_prev_) {
            auto bat = sensors::battery_charge_limit_info();
            std::string prof = lower(sensors::get_platform_profile());
            double floor = tn["battery_perf_min_pct"].as_number(20);
            if ((prof == "performance" || prof == "custom") && bat.capacity && *bat.capacity < floor)
                fire("battery_perf", cd, "Performance profile on low battery",
                     prof + " profile active on battery at " + std::to_string(*bat.capacity) + "% - consider Balanced/Quiet.");
        }
    }

private:
    void fire(const std::string& kind, double cd, const std::string& summary, const std::string& body) {
        double now = now_seconds();
        auto it = last_fired_.find(kind);
        if (it != last_fired_.end() && now - it->second < cd) return;
        last_fired_[kind] = now;
        log("THERMAL-EVENT " + kind + ": " + summary + " - " + body);
        sensors::notify(summary, body);
    }
    void kick_recover(const std::string& names, double hold_s) {
        if (!have_prev_) {
            std::string prev = lower(sensors::get_platform_profile());
            if (prev.empty()) prev = "balanced";
            prev_ = prev;
            have_prev_ = true;
            if (prev != "performance") {
                auto r = sensors::set_platform_profile("performance");
                log("THERMAL-EVENT stalled_fan_recover: " + names + " stalled -> platform_profile performance (was " + prev + ")" +
                    (r.first ? "" : " FAILED " + r.second));
            } else {
                log("THERMAL-EVENT stalled_fan_recover: " + names + " stalled, already in performance - holding");
            }
        }
        recover_until_ = now_seconds() + std::max(10.0, hold_s);
    }
    void end_recover() {
        std::string prev = prev_;
        have_prev_ = false;
        recover_until_ = 0;
        if (!prev.empty() && prev != "performance") {
            auto r = sensors::set_platform_profile(prev);
            log("THERMAL-EVENT stalled_fan_recover: fans turning again -> restored platform_profile " + prev + (r.first ? "" : " FAILED " + r.second));
        }
    }
    std::string user_, prev_;
    bool have_prev_ = false;
    double hot_since_ = 0, recover_until_ = 0;
    std::map<std::string, double> last_fired_;
};

// ------------------------------------------------------------ control dispatch
static void register_methods(control::Server& srv, const std::string& user) {
    srv.add("status", [](const Json&) {
        auto ig = sensors::igpu_values();
        auto dg = sensors::dgpu_values();
        Json o = Json::obj();
        o.set("daemon", "tuxthrottled");
        o.set("platform_profile", sensors::get_platform_profile());
        Json cpu = Json::obj();
        cpu.set("temp_c", status::opt(sensors::cpu_temp_c()));
        cpu.set("power_w", status::opt(sensors::cpu_power_watts()));
        o.set("cpu", cpu);
        Json i = Json::obj();
        i.set("clock_mhz", status::opt(ig.clock_mhz));
        i.set("temp_c", status::opt(ig.temp_c));
        o.set("igpu", i);
        Json d = Json::obj();
        d.set("clock_mhz", status::opt(dg.clock_mhz));
        d.set("temp_c", status::opt(dg.temp_c));
        d.set("util_pct", status::opt(dg.util_pct));
        d.set("power_w", status::opt(dg.power_w));
        o.set("dgpu", d);
        o.set("tdp", status::ryzen_json());
        o.set("fans", status::fans_json());
        o.set("battery", status::charge_limit_json());
        bool known = false;
        bool ac = status::ac_online(&known);
        o.set("ac_online", known ? Json(ac) : Json());
        return o;
    });
    srv.add("reload", [](const Json&) {
        g_reload = true;
        Json o = Json::obj();
        o.set("reload", "queued");
        return o;
    });
    auto rows_json = [](const std::vector<profiles::ResultRow>& rows) {
        Json a = Json::arr();
        for (const auto& r : rows) {
            Json o = Json::obj();
            o.set("key", r.key);
            o.set("ok", r.ok);
            o.set("msg", r.msg);
            a.push(o);
        }
        return a;
    };
    srv.add("apply_profile", [user, rows_json](const Json& p) {
        std::string name = p["name"].str();
        Json st = profiles::load_profile(name, user);
        if (st.object().empty()) throw std::runtime_error("no such profile: " + name);
        profiles::snapshot(user, "pre-apply-" + name);
        Json o = Json::obj();
        o.set("name", name);
        o.set("results", rows_json(profiles::apply_state(st, user, p["with_gpu_mode"].as_bool())));
        return o;
    });
    srv.add("snapshot", [user](const Json& p) {
        Json o = Json::obj();
        o.set("path", profiles::snapshot(user, p["label"].str().empty() ? "manual" : p["label"].str()));
        return o;
    });
    srv.add("rollback", [user, rows_json](const Json& p) {
        Json o = Json::obj();
        o.set("results", rows_json(profiles::rollback(p["target"].str().empty() ? "last" : p["target"].str(), user)));
        return o;
    });
    srv.add("set", [user](const Json& p) {
        std::string target = p["target"].str();
        sensors::Result r{true, ""};
        auto num = [&](const Json& v) -> std::optional<double> { return v.is_number() ? std::optional<double>(v.as_number()) : std::nullopt; };
        if (target == "power-profile") r = sensors::set_platform_profile(p["value"].str());
        else if (target == "tdp") r = sensors::set_ryzenadj_limits(num(p["fast"]), num(p["slow"]), num(p["stapm"]));
        else if (target == "battery") r = sensors::set_battery_charge_limit(static_cast<int>(p["value"].as_number()));
        else if (target == "nvpl") r = sensors::set_nvidia_power_limit(static_cast<int>(p["value"].as_number()));
        else if (target == "fan-boost") {
            int raw = std::max(0, std::min(255, static_cast<int>(std::lround(p["percent"].as_number() * 255 / 100))));
            std::string which = p["which"].str().empty() ? "both" : p["which"].str();
            std::vector<int> idxs = which == "both" ? std::vector<int>{1, 2} : std::vector<int>{std::atoi(which.c_str())};
            std::vector<std::string> errs;
            for (int i : idxs) {
                auto o = sensors::set_fan_boost(i, raw);
                if (!o.first) errs.push_back(o.second);
            }
            r = {errs.empty(), join(errs, "; ")};
        } else if (target == "gpumode") r = sensors::gpu_mode_set(p["value"].str());
        else if (target == "refresh") r = sensors::set_panel_refresh(static_cast<int>(p["value"].as_number()));
        else if (target == "gpu-clock") {
            std::string val = lower(p["value"].is_string() ? p["value"].as_string() : "");
            bool reset = p["value"].is_null() || val == "reset" || val == "unlock" || val == "off" || val == "0" ||
                         (p["value"].is_number() && p["value"].as_number() == 0);
            if (reset) {
                r = sensors::reset_nvidia_clocks();
                profiles::write_config("nvclk.json", nullptr, user);
            } else {
                auto info = sensors::nvidia_clock_info();
                int hi = static_cast<int>(p["value"].is_number() ? p["value"].as_number() : std::atof(val.c_str()));
                int lo = static_cast<int>(p["min"].as_number());
                if (!lo) lo = info ? info->gr_min : 210;
                r = sensors::set_nvidia_clock_lock(lo, hi);
                if (r.first) {
                    Json n = Json::obj();
                    n.set("gr_min", lo);
                    n.set("gr_max", hi);
                    profiles::write_config("nvclk.json", &n, user);
                }
            }
        } else throw std::runtime_error("unknown set target: " + target);
        if (!r.first) throw std::runtime_error(r.second.empty() ? "failed" : r.second);
        Json o = Json::obj();
        o.set("target", target);
        o.set("ok", true);
        return o;
    });
}

static void on_signal(int sig) {
    g_stop = true;
    (void)sig;
}

int run(const std::string& config_path, const std::string& user, bool once) {
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    FanController fans;
    GameProfileController games(user);
    ThermalWatcher thermal(user);
    ScheduleController sched(user);
    double last_mtime = -1;
    Json cfg = load_config(config_path);
    bool have_ac = false, last_ac = false;
    double last_game_check = 0;

    std::unique_ptr<control::Server> srv;
    if (!once && is_root()) {
        srv = std::make_unique<control::Server>();
        register_methods(*srv, user);
        if (srv->start()) log(std::string("control socket -> ") + control::kSocketPath);
        else { log("control socket unavailable"); srv.reset(); }
    }
    log("start; config " + config_path + (path_exists(config_path) ? " (exists)" : " (defaults)"));
    while (!g_stop) {
        struct stat st;
        double m = ::stat(config_path.c_str(), &st) == 0 ? static_cast<double>(st.st_mtime) + static_cast<double>(st.st_mtim.tv_nsec) / 1e9 : 0.0;
        if (g_reload.exchange(false)) last_mtime = -1;
        if (m != last_mtime) {
            cfg = load_config(config_path);
            last_mtime = m;
            log(std::string("config loaded: fan_curve=") + (cfg["fan_curve"]["enabled"].as_bool() ? "True" : "False") +
                " autoswitch=" + (cfg["autoswitch"]["enabled"].as_bool() ? "True" : "False") +
                " game_profiles=" + (cfg["game_profiles"]["enabled"].as_bool() ? "True" : "False") +
                " schedule=" + (cfg["schedule"]["enabled"].as_bool() ? "True" : "False"));
        }
        fans.tick(cfg);
        thermal.tick(cfg);
        sched.tick(cfg, games.active());
        if (cfg["autoswitch"]["enabled"].as_bool()) {
            bool known = false;
            bool ac = status::ac_online(&known);
            if (known && (!have_ac || ac != last_ac)) {
                std::string bundle = cfg["autoswitch"][ac ? "on_ac" : "on_battery"].str();
                log(std::string("power source -> ") + (ac ? "AC" : "battery") + "; apply '" + bundle + "'");
                apply_bundle(bundle);
                int hz = static_cast<int>(cfg["autoswitch"][ac ? "refresh_ac" : "refresh_battery"].as_number());
                if (hz) {
                    auto r = sensors::set_panel_refresh(hz);
                    log("autoswitch: panel refresh -> " + std::to_string(hz) + " Hz" + (r.first ? "" : " FAILED " + r.second));
                }
                last_ac = ac;
                have_ac = true;
            }
        }
        int gp_poll = std::max(3, static_cast<int>(cfg["game_profiles"]["poll_s"].as_number(6)));
        if (once || now_seconds() - last_game_check >= gp_poll) {
            games.tick(cfg);
            last_game_check = now_seconds();
        }
        if (once) break;
        int wait = std::max(1, static_cast<int>(cfg["poll_s"].as_number(2)));
        for (int i = 0; i < wait * 10 && !g_stop; ++i) sleep_ms(100);
    }
    fans.restore();
    if (srv) srv->stop();
    return 0;
}

}  // namespace tt::powerd
