// ttctl - TuxThrottle command line. C++20, standard library + libc only.
#include <unistd.h>

#include <cstdio>
#include <algorithm>
#include <cmath>
#include <iostream>
#include <string>
#include <vector>

#include "bench.hpp"
#include "bootcfg.hpp"
#include "distro.hpp"
#include "json.hpp"
#include "compat.hpp"
#include "control.hpp"
#include "desktop.hpp"
#include "devices.hpp"
#include "diag.hpp"
#include "drivehealth.hpp"
#include "engine.hpp"
#include "hwtools.hpp"
#include "formats.hpp"
#include "gamecfg.hpp"
#include "gpudrivers.hpp"
#include "hotkey.hpp"
#include "dbus.hpp"
#include "mhedit.hpp"
#include "launchbuild.hpp"
#include "kargs.hpp"
#include "launchers.hpp"
#include "mangohud.hpp"
#include "powerd.hpp"
#include "profiles.hpp"
#include "safety.hpp"
#include "sensors.hpp"
#include "steam.hpp"
#include "steamperf.hpp"
#include "status.hpp"
#include "userenv.hpp"
#include "vram.hpp"
#include "util.hpp"

using namespace tt;

static std::string app_config_dir() {
    if (const char* e = getenv("TUXTHROTTLE_CONFIG")) return e;
    for (const char* c : {"/opt/tuxthrottle/config", "config", "../config"})
        if (path_exists(std::string(c) + "/pkgmap.json")) return c;
    return "config";
}

static std::string self_path() {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    return n > 0 ? std::string(buf, static_cast<size_t>(n)) : "ttctl";
}

static void usage() {
    std::puts(
        "tuxthrottlectl - TuxThrottle\n"
        "  status [--json] | watch [sec] [--json]\n"
        "  get {power-profile|tdp|fans|battery|nvpl|gamemode|clocks|gpumode} [--json]\n"
        "  set power-profile <name> | tdp {preset|--stapm W --fast W --slow W} | fan-boost <1|2|both> <pct>\n"
        "      | battery <pct> | nvpl <W> | gpumode <integrated|hybrid|nvidia> | refresh <hz> | gpu-clock {<mhz> [--min MHZ] | reset}\n"
        "  gamemode {on|off|toggle} | schedule {show|on|off}\n"
        "  profile {list|apply|save|show|delete|export|import} [name] [path] | snapshot [label] | rollback [last|file]\n"
        "  daemon {status|ping|reload}\n"
        "ttctl dev helpers:\n"
        "  ttctl distro [--json]\n"
        "  ttctl expand '<command with {PKG_*} / {KARGS_*} placeholders>'\n"
        "  ttctl kargs add|del|check|live '<args>' | where\n"
        "  ttctl bench env [--dir D] [--delay S] [--duration S]\n"
        "  ttctl bench stats <log.csv> [--json]\n"
        "  ttctl bench compare <A.csv> <B.csv> [--json]");
}

static std::string num(double v) {
    char b[40];
    std::snprintf(b, sizeof b, "%g", v);
    return b;
}

static Json stats_json(const bench::Stats& s) {
    Json::Object o;
    auto put = [&](const char* k, Json v) { o.emplace_back(k, std::move(v)); };
    put("frames", static_cast<double>(s.frames));
    put("seconds", s.seconds);
    put("avg_fps", s.avg_fps);
    put("low1_fps", s.low1_fps);
    put("low01_fps", s.low01_fps);
    put("p99_ms", s.p99_ms);
    put("max_ms", s.max_ms);
    put("stutters", static_cast<double>(s.stutters));
    auto opt = [&](const char* k, const std::optional<double>& v) { put(k, v ? Json(*v) : Json()); };
    opt("cpu_load", s.cpu_load);
    opt("gpu_load", s.gpu_load);
    opt("cpu_temp", s.cpu_temp);
    opt("gpu_temp", s.gpu_temp);
    opt("gpu_power", s.gpu_power);
    return Json(std::move(o));
}

static int cmd_distro(const std::vector<std::string>& a) {
    const Distro& d = current_distro();
    if (!a.empty() && a[0] == "--json") {
        Json::Object o;
        o.emplace_back("id", d.id);
        o.emplace_back("name", d.name);
        o.emplace_back("version", d.version);
        o.emplace_back("family", d.family);
        o.emplace_back("atomic", d.atomic);
        o.emplace_back("pkg", d.pkg);
        std::puts(Json(std::move(o)).dump(2).c_str());
    } else {
        std::printf("%s (id=%s, family=%s, pkg=%s, atomic=%s)\n", d.name.c_str(), d.id.c_str(), d.family.c_str(),
                    d.pkg.empty() ? "?" : d.pkg.c_str(), d.atomic ? "true" : "false");
    }
    return 0;
}

static int cmd_kargs(const std::vector<std::string>& a) {
    if (a.empty()) { usage(); return 2; }
    if (a[0] == "where") {
        std::printf("bootloader: %s  staged: %s\n", bootcfg::detect().c_str(), join(bootcfg::staged_tokens(), " ").c_str());
        return 0;
    }
    auto args = split_ws(a.size() > 1 ? a[1] : "");
    if (a[0] == "live") return kargs::has_tokens(bootcfg::live_tokens(), args) ? 0 : 1;
    if (a[0] == "check") return kargs::has_tokens(bootcfg::staged_tokens(), args) ? 0 : 1;
    if (a[0] == "add" || a[0] == "del") {
        std::string msg;
        bool ok = a[0] == "add" ? bootcfg::apply(args, {}, msg) : bootcfg::apply({}, args, msg);
        if (!msg.empty()) std::printf("kargs: %s\n", msg.c_str());
        return ok ? 0 : 1;
    }
    usage();
    return 2;
}

static int cmd_bench(const std::vector<std::string>& a) {
    if (a.empty()) { usage(); return 2; }
    auto flag = [&](const char* name) {
        for (const auto& x : a) if (x == name) return true;
        return false;
    };
    auto opt = [&](const char* name, const std::string& def) {
        for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == name) return a[i + 1];
        return def;
    };
    auto load = [&](const std::string& path) {
        auto t = read_file(path);
        if (!t) throw std::runtime_error("cannot read " + path);
        return bench::stats(bench::parse_log(*t));
    };
    try {
        if (a[0] == "env") {
            std::string home = getenv("HOME") ? getenv("HOME") : ".";
            std::string dir = opt("--dir", home + "/.local/share/tuxthrottle/bench-logs");
            run_shell("mkdir -p " + shell_quote(dir));
            std::puts(bench::mangohud_env(dir, std::stoi(opt("--delay", "5")), std::stoi(opt("--duration", "60"))).c_str());
            return 0;
        }
        if (a[0] == "stats" && a.size() >= 2) {
            auto s = load(a[1]);
            if (flag("--json")) { std::puts(stats_json(s).dump(2).c_str()); return 0; }
            const Json j = stats_json(s);
            for (const auto& kv : j.object())
                if (!kv.second.is_null()) std::printf("%-12s%s\n", kv.first.c_str(), num(kv.second.as_number()).c_str());
            return 0;
        }
        if (a[0] == "compare" && a.size() >= 3) {
            auto rows = bench::compare(load(a[1]), load(a[2]));
            if (flag("--json")) {
                Json::Array arr;
                for (const auto& r : rows) {
                    Json::Object o;
                    o.emplace_back("metric", r.metric);
                    o.emplace_back("a", r.a);
                    o.emplace_back("b", r.b);
                    o.emplace_back("delta_pct", r.delta_pct);
                    o.emplace_back("verdict", r.verdict);
                    arr.emplace_back(std::move(o));
                }
                std::puts(Json(std::move(arr)).dump(2).c_str());
                return 0;
            }
            std::printf("%-28s%10s%10s%10s  verdict\n", "metric", "A", "B", "change");
            for (const auto& r : rows)
                std::printf("%-28s%10s%10s%+9.1f%%  %s\n", r.metric.c_str(), num(r.a).c_str(), num(r.b).c_str(),
                            r.delta_pct, r.verdict.c_str());
            return 0;
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 1;
    }
    usage();
    return 2;
}

// ------------------------------------------------------------------ tuxthrottlectl commands
static bool has_flag(const std::vector<std::string>& a, const char* f) {
    return std::find(a.begin(), a.end(), f) != a.end();
}
static std::vector<std::string> positional(const std::vector<std::string>& a, std::initializer_list<const char*> value_flags = {}) {
    std::vector<std::string> out;
    for (size_t i = 0; i < a.size(); ++i) {
        bool vf = false;
        for (const char* f : value_flags) vf = vf || a[i] == f;
        if (vf) { ++i; continue; }
        if (starts_with(a[i], "--")) continue;
        out.push_back(a[i]);
    }
    return out;
}
static std::optional<std::string> flag_value(const std::vector<std::string>& a, const char* f) {
    for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1];
    return std::nullopt;
}

static int fail(const std::string& msg) {
    std::fprintf(stderr, "error: %s\n", msg.c_str());
    return 1;
}

static void out_json_or_text(const Json& j, bool as_json) {
    if (as_json) { std::puts(j.dump(2, true).c_str()); return; }
    if (j.is_object()) {
        for (const auto& kv : j.object()) {
            std::string v = kv.second.is_string() ? kv.second.as_string() : kv.second.dump();
            std::printf("%s: %s\n", kv.first.c_str(), v.c_str());
        }
    } else std::puts((j.is_string() ? j.as_string() : j.dump()).c_str());
}

static std::optional<Json> daemon_call(const std::string& method, const Json& params = Json::obj()) {
    if (control::available()) return control::call(method, params);
    return std::nullopt;
}

// Routes a `set` through the daemon when it is up; returns an exit code, or nullopt to act directly.
static std::optional<int> daemon_set(const Json& params) {
    auto resp = daemon_call("set", params);
    if (!resp) return std::nullopt;
    if ((*resp)["ok"].as_bool()) {
        std::printf("%s: ok (via tuxthrottled)\n", params["target"].str().c_str());
        return 0;
    }
    return fail((*resp)["error"].str().empty() ? "daemon rejected the request" : (*resp)["error"].str());
}

static Json set_params(const char* target) {
    Json p = Json::obj();
    p.set("target", target);
    return p;
}

static int print_rows(const std::vector<profiles::ResultRow>& rows) {
    int bad = 0;
    for (const auto& r : rows) {
        std::printf("  [%s] %s%s\n", r.ok ? "ok " : "ERR", r.key.c_str(), r.msg.empty() ? "" : (" - " + r.msg).c_str());
        bad += !r.ok;
    }
    return bad ? 1 : 0;
}
static Json rows_to_json(const std::vector<profiles::ResultRow>& rows) {
    Json a = Json::arr();
    for (const auto& r : rows) {
        Json o = Json::obj();
        o.set("key", r.key);
        o.set("ok", r.ok);
        o.set("msg", r.msg);
        a.push(o);
    }
    return a;
}
static int print_rows_json(const Json& rows) {
    std::puts(rows.dump(2, true).c_str());
    for (const auto& r : rows.array()) if (!r["ok"].as_bool()) return 1;
    return 0;
}

static std::string one_liner() {
    Json s = status::gather();
    auto r = [](const Json& v, int nd = 0) -> std::string {
        if (!v.is_number()) return "?";
        return nd == 0 ? std::to_string(static_cast<long>(std::lround(v.as_number()))) : format_double(v.as_number(), nd);
    };
    std::vector<std::string> parts;
    parts.push_back(timestamp("%H:%M:%S"));
    parts.push_back("prof=" + (s["platform_profile"].str().empty() ? std::string("?") : s["platform_profile"].str()));
    parts.push_back(std::string("game=") + (s["game_mode"].as_bool() ? "on" : "off"));
    const Json& cpu = s["cpu"];
    parts.push_back("CPU " + r(cpu["temp_c"]) + "C " + r(cpu["freq_ghz"], 1) + "GHz " + r(cpu["power_w"]) + "W");
    const Json& tdp = s["tdp"];
    if (tdp.is_object() && !tdp.object().empty())
        parts.push_back("TDP " + r(tdp["stapm_limit"]) + "/" + r(tdp["fast_limit"]) + "/" + r(tdp["slow_limit"]) + "W");
    const Json& dg = s["dgpu"];
    parts.push_back(dg["awake"].as_bool() ? "dGPU " + r(dg["temp_c"]) + "C " + r(dg["clock_mhz"]) + "MHz " + r(dg["util_pct"]) + "% " + r(dg["power_w"]) + "W"
                                          : std::string("dGPU asleep"));
    if (!s["fans"].array().empty()) {
        std::vector<std::string> rp;
        for (const auto& f : s["fans"].array()) rp.push_back(std::to_string(static_cast<long>(f["rpm"].as_number())));
        parts.push_back("fans " + join(rp, "/") + "rpm");
    }
    const Json& bat = s["battery_charge_limit"];
    if (bat["current"].is_number()) parts.push_back("bat " + r(bat["current"]) + "%");
    return join(parts, "  ");
}

static int cmd_set(const std::vector<std::string>& a) {
    auto pos = positional(a, {"--stapm", "--fast", "--slow", "--min"});
    if (pos.empty()) return fail("set needs a target");
    std::string target = pos[0];
    std::vector<std::string> val(pos.begin() + 1, pos.end());
    auto num_flag = [&](const char* f) -> std::optional<double> {
        auto v = flag_value(a, f);
        return v ? std::optional<double>(std::atof(v->c_str())) : std::nullopt;
    };
    auto finish = [&](const sensors::Result& r) { return r.first ? 0 : fail(r.second); };
    if (target == "power-profile") {
        if (val.empty()) return fail("give a profile name");
        Json p = set_params("power-profile");
        p.set("value", val[0]);
        if (auto rc = daemon_set(p)) return *rc;
        return finish(sensors::set_platform_profile(val[0]));
    }
    if (target == "tdp") {
        std::optional<double> s, f, sl;
        std::string v = val.empty() ? "" : lower(val[0]);
        std::string cap = v.empty() ? v : std::string(1, static_cast<char>(std::toupper(v[0]))) + v.substr(1);
        if (auto pr = powerd::tdp_preset(cap)) { s = pr->stapm; f = pr->fast; sl = pr->slow; }
        else { s = num_flag("--stapm"); f = num_flag("--fast"); sl = num_flag("--slow"); }
        if (!s && !f && !sl) return fail("give a preset name or --stapm/--fast/--slow");
        Json p = set_params("tdp");
        p.set("stapm", s ? Json(*s) : Json());
        p.set("fast", f ? Json(*f) : Json());
        p.set("slow", sl ? Json(*sl) : Json());
        if (auto rc = daemon_set(p)) return *rc;
        return finish(sensors::set_ryzenadj_limits(f, sl, s));
    }
    if (target == "fan-boost") {
        if (val.size() < 2) return fail("fan-boost needs <1|2|both> <percent>");
        int pct = std::atoi(val[1].c_str());
        Json p = set_params("fan-boost");
        p.set("which", val[0]);
        p.set("percent", pct);
        if (auto rc = daemon_set(p)) return *rc;
        std::vector<int> idxs = val[0] == "both" ? std::vector<int>{1, 2} : std::vector<int>{std::atoi(val[0].c_str())};
        int raw = std::max(0, std::min(255, static_cast<int>(std::lround(pct * 255.0 / 100.0))));
        std::vector<std::string> errs;
        for (int i : idxs) { auto r = sensors::set_fan_boost(i, raw); if (!r.first) errs.push_back(r.second); }
        return errs.empty() ? 0 : fail(join(errs, "; "));
    }
    if (target == "battery" || target == "nvpl") {
        if (val.empty()) return fail("give a value");
        int v = std::atoi(val[0].c_str());
        Json p = set_params(target.c_str());
        p.set("value", v);
        if (auto rc = daemon_set(p)) return *rc;
        return finish(target == "battery" ? sensors::set_battery_charge_limit(v) : sensors::set_nvidia_power_limit(v));
    }
    if (target == "gpumode") {
        auto r = sensors::gpu_mode_set(val.empty() ? "" : val[0]);
        if (r.first) std::puts("switched - log out or reboot to apply");
        return finish(r);
    }
    if (target == "refresh") {
        if (val.empty()) return fail("give a refresh rate in Hz");
        Json p = set_params("refresh");
        p.set("value", std::atoi(val[0].c_str()));
        if (auto rc = daemon_set(p)) return *rc;
        return finish(sensors::set_panel_refresh(std::atoi(val[0].c_str())));
    }
    if (target == "gpu-clock") {
        std::string v = val.empty() ? "" : lower(val[0]);
        bool reset = v.empty() || v == "reset" || v == "unlock" || v == "off" || v == "0";
        Json p = set_params("gpu-clock");
        if (reset) p.set("value", "reset");
        else {
            p.set("value", std::atoi(v.c_str()));
            if (auto m = num_flag("--min")) p.set("min", static_cast<int>(*m));
        }
        if (auto rc = daemon_set(p)) return *rc;
        if (reset) {
            auto r = sensors::reset_nvidia_clocks();
            profiles::write_config("nvclk.json", nullptr);
            return finish(r);
        }
        auto info = sensors::nvidia_clock_info();
        int hi = std::atoi(v.c_str());
        int lo = num_flag("--min") ? static_cast<int>(*num_flag("--min")) : (info ? info->gr_min : 210);
        auto r = sensors::set_nvidia_clock_lock(lo, hi);
        if (r.first) {
            Json n = Json::obj();
            n.set("gr_min", lo);
            n.set("gr_max", hi);
            profiles::write_config("nvclk.json", &n);
        }
        return finish(r);
    }
    return fail("unknown target " + target);
}

static int cmd_schedule(const std::vector<std::string>& a) {
    auto pos = positional(a);
    std::string action = pos.empty() ? "show" : pos[0];
    std::string path = config_dir() + "/powerd.json";
    Json cfg = read_json_file(path);
    Json sc = cfg["schedule"].is_object() ? cfg["schedule"] : Json::obj();
    if (action == "show") {
        if (sc.object().empty()) {
            sc.set("enabled", false);
            sc.set("rules", Json::arr());
            sc.set("outside", Json());
        }
        out_json_or_text(sc, has_flag(a, "--json"));
        return 0;
    }
    if (!sc.has("poll_s")) sc.set("poll_s", 60);
    if (!sc.has("rules")) sc.set("rules", Json::arr());
    if (!sc.has("outside")) sc.set("outside", Json());
    sc.set("enabled", action == "on");
    cfg.set("schedule", sc);
    profiles::write_config("powerd.json", &cfg);
    std::printf("schedule: %s (%zu rule(s))\n", action == "on" ? "enabled" : "disabled", sc["rules"].array().size());
    return 0;
}

static int cmd_profile(const std::vector<std::string>& a) {
    auto pos = positional(a);
    bool as_json = has_flag(a, "--json");
    if (pos.empty()) return fail("profile needs an action");
    std::string act = pos[0];
    std::string name = pos.size() > 1 ? pos[1] : "", path = pos.size() > 2 ? pos[2] : "";
    if (act == "list") {
        auto names = profiles::list_profiles();
        if (as_json) {
            Json arr = Json::arr();
            for (const auto& n : names) arr.push(n);
            std::puts(arr.dump().c_str());
        } else std::puts(names.empty() ? "(none)" : join(names, "\n").c_str());
        return 0;
    }
    if (act == "reassert") {
        auto rows = profiles::reassert();
        for (const auto& r : rows) std::printf("%s: %s%s\n", r.key.c_str(), r.ok ? "ok" : "FAILED", r.msg.empty() ? "" : (" - " + r.msg).c_str());
        return 0;
    }
    if (name.empty()) return fail("'profile " + act + "' needs a name");
    if (act == "save") {
        Json st = profiles::capture_state();
        profiles::save_profile(name, st);
        if (as_json) std::puts(st.dump(2, true).c_str());
        else std::printf("saved profile '%s'\n", name.c_str());
        return 0;
    }
    if (act == "show") {
        Json st = profiles::load_profile(name);
        out_json_or_text(st, as_json);
        return st.object().empty() ? 1 : 0;
    }
    if (act == "delete") return profiles::delete_profile(name) ? 0 : fail("no such profile");
    if (act == "export") {
        if (path.empty()) return fail("'profile export' needs a name and a destination path");
        try {
            std::printf("exported '%s' -> %s\n", name.c_str(), profiles::export_profile(name, path).c_str());
        } catch (const std::exception& e) { return fail(e.what()); }
        return 0;
    }
    if (act == "import") {
        try {
            std::printf("imported '%s' from %s\n", profiles::import_profile(name, path).c_str(), name.c_str());
        } catch (const std::exception& e) { return fail(e.what()); }
        return 0;
    }
    if (act == "apply") {
        Json st = profiles::load_profile(name);
        if (st.object().empty()) return fail("no such profile: " + name);
        Json params = Json::obj();
        params.set("name", name);
        params.set("with_gpu_mode", has_flag(a, "--with-gpu-mode"));
        if (auto resp = daemon_call("apply_profile", params)) {
            if (!(*resp)["ok"].as_bool()) return fail((*resp)["error"].str().empty() ? "daemon rejected apply_profile" : (*resp)["error"].str());
            const Json& rows = (*resp)["result"]["results"];
            if (as_json) return print_rows_json(rows);
            std::puts("(via tuxthrottled)");
            int bad = 0;
            for (const auto& r : rows.array()) {
                std::printf("  [%s] %s%s\n", r["ok"].as_bool() ? "ok " : "ERR", r["key"].str().c_str(),
                            r["msg"].str().empty() ? "" : (" - " + r["msg"].str()).c_str());
                bad += !r["ok"].as_bool();
            }
            return bad ? 1 : 0;
        }
        profiles::snapshot("", "pre-apply-" + name);
        auto rows = profiles::apply_state(st, "", has_flag(a, "--with-gpu-mode"));
        return as_json ? print_rows_json(rows_to_json(rows)) : print_rows(rows);
    }
    return 2;
}

static int cmd_daemon(const std::vector<std::string>& a) {
    auto pos = positional(a);
    std::string action = pos.empty() ? "status" : pos[0];
    bool as_json = has_flag(a, "--json");
    auto pres = control::presence();
    if (pres != control::Presence::Up) {
        std::string msg = pres == control::Presence::RootOnly ? "running, but root-only - re-run with sudo" : "not running";
        if (as_json) {
            Json o = Json::obj();
            o.set("up", false);
            o.set("state", control::presence_name(pres));
            std::puts(o.dump().c_str());
        } else std::printf("tuxthrottled control socket: %s\n", msg.c_str());
        return 1;
    }
    if (action == "ping") {
        if (as_json) std::puts("{\"up\":true}");
        else std::puts("tuxthrottled: up");
        return 0;
    }
    auto resp = control::call(action);
    if (!resp) return 1;
    out_json_or_text(*resp, as_json);
    return (*resp)["ok"].as_bool() ? 0 : 1;
}

static int cmd_btrfs(const std::vector<std::string>& a) {
    std::string sub = a.empty() ? "available" : a[0];
    if (sub == "available") {
        auto m = btrfs::method();
        Json o = Json::obj();
        o.set("available", m.has_value());
        o.set("method", m ? Json(*m) : Json());
        std::puts(o.dump().c_str());
        return 0;
    }
    if (sub == "create") {
        auto s = btrfs::create_snapshot(a.size() > 1 ? a[1] : "pre-apply");
        Json o = Json::obj();
        o.set("ok", s.ok);
        o.set("method", s.method.empty() ? Json() : Json(s.method));
        o.set("id", s.id.empty() ? Json() : Json(s.id));
        o.set("msg", s.msg);
        std::puts(o.dump().c_str());
        if (s.ok) std::fprintf(stderr, "# to roll back: %s\n", btrfs::rollback_hint(s.id).c_str());
        return s.ok ? 0 : 1;
    }
    if (sub == "list") {
        size_t limit = 10;
        if (auto v = flag_value(a, "--limit")) limit = static_cast<size_t>(std::atoi(v->c_str()));
        Json arr = Json::arr();
        for (const auto& r : btrfs::list_snapshots(limit)) {
            Json o = Json::obj();
            o.set("number", r.number);
            o.set("date", r.date);
            o.set("description", r.description);
            arr.push(o);
        }
        std::puts(arr.dump(2).c_str());
        return 0;
    }
    return 2;
}

static int cmd_watchdog(const std::vector<std::string>& a) {
    if (a.empty()) return 2;
    if (a[0] == "arm") {
        auto pos = positional(std::vector<std::string>(a.begin() + 1, a.end()), {"--user", "--toolkit-dir"});
        auto user = flag_value(a, "--user");
        if (pos.empty() || !user) return fail("watchdog arm <seconds> --user NAME");
        try {
            std::puts(watchdog::arm(std::atoi(pos[0].c_str()), *user).c_str());
        } catch (const std::exception& e) {
            std::fprintf(stderr, "watchdog: could not arm: %s\n", e.what());
            return 1;
        }
        return 0;
    }
    if (a[0] == "disarm" && a.size() > 1) return watchdog::disarm(a[1]) ? 0 : 1;
    if (a[0] == "status" && a.size() > 1) { std::puts(watchdog::is_armed(a[1]) ? "armed" : "idle"); return 0; }
    return 2;
}

static int cmd_co(const std::vector<std::string>& a) {
    if (a.empty()) return fail("co needs apply|confirm|revert|reapply|status");
    std::string user = flag_value(a, "--user").value_or("");
    std::string sub = a[0];
    if (sub != "status" && !is_root()) return fail("run as root (sudo)");
    if (sub == "status") return co::status(user);
    if (sub == "confirm") return co::confirm(user);
    if (sub == "revert") return co::revert(user);
    if (sub == "reapply") return co::reapply(user);
    if (sub == "apply") {
        auto pos = positional(std::vector<std::string>(a.begin() + 1, a.end()), {"--minutes", "--user"});
        if (pos.empty()) return fail("co apply <-N> [--minutes M] [--no-gpu]");
        double minutes = flag_value(a, "--minutes") ? std::atof(flag_value(a, "--minutes")->c_str()) : 5.0;
        return co::apply(std::atoi(pos[0].c_str()), minutes, has_flag(a, "--no-gpu"), user);
    }
    return 2;
}

static Json vdf_json(const Vdf& v) {
    if (!v.is_block()) return Json(v.str());
    Json o = Json::obj();
    for (const auto& kv : v.children()) o.set(kv.first, vdf_json(kv.second));
    return o;
}
static Json yaml_json(const Yaml& y) {
    switch (y.kind) {
        case Yaml::Kind::Null: return Json();
        case Yaml::Kind::Scalar: return Json(y.scalar);
        case Yaml::Kind::Map: { Json o = Json::obj(); for (const auto& kv : y.map) o.set(kv.first, yaml_json(kv.second)); return o; }
        case Yaml::Kind::Seq: { Json a = Json::arr(); for (const auto& i : y.seq) a.push(yaml_json(i)); return a; }
    }
    return Json();
}

// dev helpers used to cross-check the parsers against the Python reference implementations
static int cmd_lobuild(const std::string& json_text) {
    Json j = Json::parse(json_text);
    launchbuild::Opts o;
    auto b = [&](const char* k, bool& dst) { if (j[k].is_bool()) dst = j[k].as_bool(); };
    b("mangohud", o.mangohud); b("mh_off", o.mh_off); b("mh_dlsym", o.mh_dlsym); b("ac_keep_mh", o.ac_keep_mh); b("gamemode", o.gamemode);
    b("gamescope", o.gamescope); b("prime", o.prime); b("nvcache", o.nvcache); b("nv_threaded", o.nv_threaded); b("radv_gpl", o.radv_gpl);
    b("radv_full", o.radv_full); b("mesa_cache", o.mesa_cache); b("no_vsync", o.no_vsync); b("dxvk_cache", o.dxvk_cache); b("dxvk_async", o.dxvk_async);
    b("proton_nolog", o.proton_nolog); b("ntsync", o.ntsync); b("anticheat", o.anticheat); b("staging_shm", o.staging_shm); b("gl_yield", o.gl_yield);
    b("gl_maxframes", o.gl_maxframes); b("gl_vrr", o.gl_vrr); b("glthread", o.glthread);
    if (j["w"].is_string()) o.w = j["w"].as_string();
    if (j["h"].is_string()) o.h = j["h"].as_string();
    if (j["fps"].is_string()) o.fps = j["fps"].as_string();
    std::puts(launchbuild::build(o, j["dir"].str("~/.cache/tuxthrottle-shaders"), static_cast<int>(j["gb"].as_number(80)), j["user"].str("bean")).c_str());
    return 0;
}

static int cmd_dbus_ping() {
    dbus::Connection c;
    if (!c.connect()) return fail("cannot connect to the session bus");
    std::printf("connected as %s\n", c.unique_name().c_str());
    dbus::Message m, r;
    m.dest = "org.freedesktop.DBus"; m.path = "/org/freedesktop/DBus"; m.iface = "org.freedesktop.DBus"; m.member = "ListNames";
    if (!c.call(m, &r)) return fail("ListNames failed");
    dbus::Reader rd(r.body.data(), r.body.size());
    uint32_t len = rd.u32();
    size_t end = rd.pos() + len;
    int n = 0;
    bool watcher = false;
    while (rd.pos() < end) { std::string s = rd.str(); ++n; if (s == "org.kde.StatusNotifierWatcher") watcher = true; rd.align(4); }
    std::printf("%d names on the bus; StatusNotifierWatcher: %s\n", n, watcher ? "present" : "absent");
    return 0;
}

namespace tt::clients { int waybar_main(const std::vector<std::string>&); int mangohud_line_main(const std::vector<std::string>&); }

static int cmd_dev(const std::vector<std::string>& a) {
    if (a.size() == 1 && a[0] == "bench-core") {
        auto t = [](const char* n, int iters, const std::function<void()>& f) { f(); double t0 = now_seconds(); for (int i = 0; i < iters; ++i) f(); std::printf("%-26s %8.3f ms/iter\n", n, (now_seconds() - t0) * 1000 / iters); };
        std::string cfg = engine::config_dir();
        auto big = read_file(cfg + "/tweaks.json").value_or("");
        t("json parse tweaks.json", 200, [&] { Json::parse(big); });
        t("json dump tweaks.json", 200, [&] { Json::parse(big).dump(2, true); });
        t("engine::load_items", 20, [&] { engine::load_items(); });
        launchbuild::Opts o;
        t("launchbuild x1000", 20, [&] { for (int i = 0; i < 1000; ++i) launchbuild::build(o, "~/.cache/x", 80, "bean"); });
        std::string conf = "# c\nfps\ncpu_stats\ncpu_text=Ryzen 7 5800H\nposition=top-left\nfont_size=20\n";
        t("mhedit merge x2000", 20, [&] { mhedit::Editor e; e.cpu = "R"; e.gpus = {"A", "B"}; for (int i = 0; i < 2000; ++i) mhedit::merge_conf(conf, mhedit::build_managed(e)); });
        auto words = std::vector<std::string>{"a", "bb", "ccc"};
        t("shlex+base64 x20000", 20, [&] { for (int i = 0; i < 20000; ++i) base64_decode(base64_encode(join(words, " "))); });
        return 0;
    }
    if (a.size() == 1 && a[0] == "sensors-bench") {
        auto t = [](const char* n, const std::function<void()>& f) { double t0 = now_seconds(); for (int i = 0; i < 20; ++i) f(); std::printf("%-22s %.2f ms\n", n, (now_seconds() - t0) * 1000 / 20); };
        t("cpu_temp", [] { sensors::cpu_temp_c(); });
        t("cpu_freq", [] { sensors::cpu_freq_ghz(); });
        sensors::cpu_power_watts();
        t("cpu_power(warm)", [] { sensors::cpu_power_watts(); });
        t("igpu_values", [] { sensors::igpu_values(); });
        t("dgpu_values", [] { sensors::dgpu_values(); });
        t("rapl_ok", [] { sensors::rapl_permissions_ok(); });
        t("game_mode_state", [] { sensors::game_mode_state(); });
        return 0;
    }
    if (a.size() == 1 && a[0] == "dbus-ping") return cmd_dbus_ping();
    if (a.size() == 2 && a[0] == "lobuild") return cmd_lobuild(a[1]);
    if (a.size() == 2 && a[0] == "subst") { std::puts(engine::substitute(a[1], resolve_real_user()).c_str()); return 0; }
    if (a.size() < 2) return 2;
    auto text = read_file(a[1]);
    if (!text) return fail("cannot read " + a[1]);
    try {
        if (a[0] == "vdf") { std::puts(vdf_json(Vdf::parse(*text)).dump(2, true).c_str()); return 0; }
        if (a[0] == "vdf-roundtrip") { std::fputs(Vdf::parse(*text).dump().c_str(), stdout); return 0; }
        if (a[0] == "yaml") { std::puts(yaml_json(Yaml::parse(*text)).dump(2, true).c_str()); return 0; }
        if (a[0] == "yaml-roundtrip") { std::fputs(Yaml::parse(*text).dump().c_str(), stdout); return 0; }
        if (a[0] == "sqlite" && a.size() > 2) {
            SqliteReader r(a[1]);
            auto t = r.read_table(a[2]);
            Json rows = Json::arr();
            for (const auto& row : t.rows) {
                Json o = Json::obj();
                for (size_t i = 0; i < t.columns.size() && i < row.size(); ++i)
                    o.set(t.columns[i], row[i].is_null() ? Json() : Json(row[i].str()));
                rows.push(o);
            }
            std::puts(rows.dump(2, true).c_str());
            return 0;
        }
    } catch (const std::exception& e) { return fail(e.what()); }
    return 2;
}

int main(int argc, char** argv) {
    std::vector<std::string> a(argv + 1, argv + argc);
    // global --user NAME: act for that user's config (boot services / watchdog run as root)
    for (size_t i = 0; i + 1 < a.size(); ++i)
        if (a[i] == "--user" && !(a.size() > 1 && (a[0] == "watchdog" || a[0] == "automount" || a[0] == "apply-tweak" || a[0] == "drives" || a[0] == "fixlog" || a[0] == "mangohud-status"))) {
            setenv("SUDO_USER", a[i + 1].c_str(), 1);
            sensors::set_session_user(a[i + 1]);
            a.erase(a.begin() + static_cast<long>(i), a.begin() + static_cast<long>(i) + 2);
            break;
        }
    if (a.empty() || a[0] == "-h" || a[0] == "--help") { usage(); return a.empty() ? 2 : 0; }
    load_pkgmap(app_config_dir());
    std::string cmd = a[0];
    a.erase(a.begin());
    bool as_json = has_flag(a, "--json");
    if (cmd == "distro") return cmd_distro(a);
    if (cmd == "expand" && !a.empty()) {
        std::puts(expand(a[0], current_distro(), shell_quote(self_path()) + " kargs").c_str());
        return 0;
    }
    if (cmd == "kargs") return cmd_kargs(a);
    if (cmd == "bench") return cmd_bench(a);
    if (cmd == "status") { out_json_or_text(status::gather(), as_json); return 0; }
    if (cmd == "watch") {
        auto pos = positional(a);
        double interval = pos.empty() ? 2.0 : std::atof(pos[0].c_str());
        while (true) {
            if (as_json) std::puts(status::gather().dump().c_str());
            else { std::fputs("[2J[H", stdout); std::puts(one_liner().c_str()); }
            std::fflush(stdout);
            sleep_ms(static_cast<int>(std::max(0.5, interval) * 1000));
        }
    }
    if (cmd == "get") {
        auto pos = positional(a);
        if (pos.empty()) return fail("get needs a target");
        out_json_or_text(status::get(pos[0]), as_json);
        return 0;
    }
    if (cmd == "set") return cmd_set(a);
    if (cmd == "gamemode") {
        auto pos = positional(a);
        if (pos.empty()) return fail("gamemode needs on|off|toggle");
        auto r = pos[0] == "toggle" ? sensors::toggle_game_mode() : sensors::set_game_mode(pos[0] == "on");
        return r.first ? 0 : fail(r.second.empty() ? "failed" : r.second);
    }
    if (cmd == "schedule") return cmd_schedule(a);
    if (cmd == "profile") return cmd_profile(a);
    if (cmd == "snapshot") {
        auto pos = positional(a);
        std::puts(profiles::snapshot("", pos.empty() ? "manual" : pos[0]).c_str());
        return 0;
    }
    if (cmd == "rollback") {
        auto pos = positional(a);
        auto rows = profiles::rollback(pos.empty() ? "last" : pos[0], "", has_flag(a, "--with-gpu-mode"));
        return as_json ? print_rows_json(rows_to_json(rows)) : print_rows(rows);
    }
    if (cmd == "daemon") return cmd_daemon(a);
    if (cmd == "btrfs-snapshot") return cmd_btrfs(a);
    if (cmd == "co") return cmd_co(a);
    if (cmd == "dev") return cmd_dev(a);
    if (cmd == "launchopts") return launchers::launchopts_main(a);
    if (cmd == "heroicopts") return launchers::heroicopts_main(a);
    if (cmd == "lutrisopts") return launchers::lutrisopts_main(a);
    if (cmd == "lutris-env") return launchers::lutris_env_main(a);
    if (cmd == "fpslimit") return fpslimit::main(a);
    if (cmd == "protondb") return protondb::main(a);
    if (cmd == "fixlog") return fixlog::main(a);
    if (cmd == "mangohud-games") return mangohud::main(a);
    if (cmd == "mangohud-status") return mangohud::status_main(a);
    if (cmd == "compat") return compat::main(a);
    if (cmd == "diag") return diag::main(a);
    if (cmd == "gpu-drivers") return gpudrivers::main(a);
    if (cmd == "waybar") return clients::waybar_main(a);
    if (cmd == "mangohud-line") return clients::mangohud_line_main(a);
    if (cmd == "hotkey") return hotkey::main(a);
    if (cmd == "tui") return tui::main(a);
    if (cmd == "vram") return vram::main(a);
    if (cmd == "prefix") return steam::prefix_main(a);
    if (cmd == "savevault") return steam::savevault_main(a);
    if (cmd == "shadercache") return steam::shadercache_main(a);
    if (cmd == "drives") return drivehealth::main(a);
    if (cmd == "tweaks") return engine::cli_tweaks(a);
    if (cmd == "preset") return engine::cli_preset(a);
    if (cmd == "apply-tweak") return engine::cli_apply_tweak(a);
    if (cmd == "version") { std::puts(engine::toolkit_version().c_str()); return 0; }
    if (cmd == "kbd") return kbd::main(a);
    if (cmd == "steamperf") return steamperf::main(a);
    if (cmd == "crashwatch") {
        crashwatch::set_steam_double_start_hook([](const crashwatch::Finding& f) -> std::optional<crashwatch::Finding> {
            if (is_root()) return std::nullopt;
            auto moved = steamperf::migrate_stray_bak();
            if (moved.empty()) return std::nullopt;
            std::vector<std::string> names;
            for (const auto& m : moved) names.push_back(base_name(m));
            crashwatch::Finding n = f;
            n.label = "Steam was started twice at login and one copy crashed";
            n.hint = "Cause: " + join(names, ", ") + " in ~/.config/autostart was being run as a second Steam. It has been moved to ~/.local/share/tuxthrottle - from the next login only one Steam starts.";
            return n;
        });
        return crashwatch::main(a);
    }
    if (cmd == "kde-panel") return kdepanel::main(a);
    if (cmd == "automount") return automount::main(a);
    if (cmd == "collect-model") {
        Json prof = modelgen::build_scaffold(flag_value(a, "--slug").value_or(""));
        auto out = flag_value(a, "--out");
        if (!out) { std::puts(prof.dump(2).c_str()); return 0; }
        write_file(*out, prof.dump(2) + "\n");
        std::printf("wrote %s  - %zu field(s) still need manual completion (see the \"_todo\" list); delete \"_todo\"/\"_generated\" when done.\n",
                    out->c_str(), prof["_todo"].array().size());
        return 0;
    }
    if (cmd == "watchdog") return cmd_watchdog(a);
    usage();
    return 2;
}
