#include "hwtools.hpp"

#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <regex>
#include <set>

#include "profiles.hpp"
#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::modelgen {

std::string slugify(const std::string& s) {
    std::string out;
    bool dash = false;
    for (char c : lower(s)) {
        if (std::isalnum(static_cast<unsigned char>(c))) { out += c; dash = false; }
        else if (!dash && !out.empty()) { out += '-'; dash = true; }
    }
    while (!out.empty() && out.back() == '-') out.pop_back();
    return out.empty() ? "unknown-model" : out;
}

static std::vector<std::string> hwmon_names() {
    std::vector<std::string> out;
    for (const auto& p : glob_paths("/sys/class/hwmon/hwmon*/name")) out.push_back(read_trim(p));
    return out;
}

static std::optional<std::string> pci_id(const std::string& dir) {
    if (dir.empty()) return std::nullopt;
    std::string v = read_trim(dir + "/vendor"), d = read_trim(dir + "/device");
    if (v.size() < 3 || d.size() < 3) return std::nullopt;
    return v.substr(2) + ":" + d.substr(2);
}

static std::optional<std::string> openrgb_device() {
    if (which("openrgb").empty()) return std::nullopt;
    auto r = run_argv_stdout({"openrgb", "--noautoconnect", "-l"}, 40000);
    static const std::regex row(R"(^\s*\d+:\s+(.*\S))");
    for (const auto& ln : split_lines(r.out)) {
        std::smatch m;
        if (!std::regex_search(ln, m, row)) continue;
        std::string l = lower(m[1]);
        for (const char* k : {"led", "rgb", "keyboard", "aura", "g series"})
            if (l.find(k) != std::string::npos) return m[1].str();
    }
    return std::nullopt;
}

static Json opt_str(const std::optional<std::string>& v) { return v ? Json(*v) : Json(); }

Json build_scaffold(const std::string& slug) {
    auto hw = hwmon_names();
    auto has = [&](const char* n) { return std::find(hw.begin(), hw.end(), n) != hw.end(); };
    std::string vendor = sensors::dmi_value("sys_vendor"), product = sensors::dmi_value("product_name"),
                board = sensors::dmi_value("board_name");
    std::set<std::string> todo;

    std::optional<std::string> cpu_hwmon;
    for (const char* n : {"k10temp", "zenpower", "coretemp"})
        if (has(n)) { cpu_hwmon = n; break; }
    if (!cpu_hwmon) todo.insert("cpu.hwmon");

    std::optional<std::string> fan_hwmon, pwm_hwmon;
    for (const char* n : {"alienware_wmi", "dell_smm", "asus_nb_wmi", "asus_wmi_sensors", "hp_wmi", "thinkpad"})
        if (has(n)) { fan_hwmon = n; break; }
    if (has("dell_smm")) pwm_hwmon = "dell_smm";
    int fan_count = 0;
    if (fan_hwmon) {
        std::string d = sensors::hwmon_by_name(*fan_hwmon);
        if (!d.empty()) {
            fan_count = static_cast<int>(glob_paths(d + "/fan[0-9]_input").size());
            if (!pwm_hwmon && !glob_paths(d + "/pwm[0-9]").empty()) pwm_hwmon = fan_hwmon;
        }
    } else todo.insert("fans.hwmon");
    if (!fan_count) todo.insert("fans.count");
    if (!pwm_hwmon) todo.insert("fans.pwm_hwmon");

    std::optional<std::string> pp_path = std::string("/sys/firmware/acpi/platform_profile");
    Json pp_choices = Json::arr();
    if (path_exists(*pp_path)) {
        for (const auto& c : split_ws(read_file(*pp_path + "_choices").value_or(""))) pp_choices.push(c);
    } else {
        pp_path.reset();
        todo.insert("fans.platform_profile_path");
    }

    auto igpu = pci_id(sensors::amdgpu_card_dir());
    auto dgpu = pci_id(sensors::nvidia_pci_dir());
    auto orgb = openrgb_device();
    if (!orgb) todo.insert("keyboard.openrgb_device");
    todo.insert("keyboard.usb");

    bool bat_sysfs = !sensors::battery_dir_with_threshold().empty();
    std::optional<std::string> bat_method;
    if (bat_sysfs) bat_method = "sysfs";
    else if (!which("smbios-battery-ctl").empty()) bat_method = "libsmbios";
    else todo.insert("battery.method");
    for (const char* t : {"gkey.device", "gkey.evdev", "gkey.keycode_fnlock_off", "cpu.stock_ppt_w"}) todo.insert(t);

    auto strs = [](const std::string& v) { Json a = Json::arr(); if (!v.empty()) a.push(v); return a; };
    Json o = Json::obj();
    o.set("id", slug.empty() ? slugify(product) : slug);
    o.set("name", !product.empty() ? product : (!slug.empty() ? slug : "unknown model"));
    Json match = Json::obj();
    match.set("product_name", strs(product));
    match.set("board_name", strs(board));
    o.set("match", match);
    Json cpu = Json::obj();
    cpu.set("vendor", sensors::cpu_is_amd() ? "amd" : "intel");
    cpu.set("hwmon", opt_str(cpu_hwmon));
    cpu.set("ryzenadj", sensors::ryzenadj_available());
    cpu.set("stock_ppt_w", Json());
    o.set("cpu", cpu);
    Json ig = Json::obj();
    ig.set("pci", opt_str(igpu));
    o.set("igpu", ig);
    Json dg = Json::obj();
    dg.set("pci", opt_str(dgpu));
    dg.set("vendor", dgpu ? Json("nvidia") : Json());
    o.set("dgpu", dg);
    Json fans = Json::obj();
    fans.set("hwmon", opt_str(fan_hwmon));
    fans.set("pwm_hwmon", opt_str(pwm_hwmon));
    fans.set("count", fan_count ? Json(fan_count) : Json());
    fans.set("pwm_floor", 77);
    fans.set("platform_profile_path", opt_str(pp_path));
    fans.set("platform_profiles", pp_choices);
    o.set("fans", fans);
    Json gm = Json::obj();
    gm.set("mechanism", "platform_profile");
    gm.set("value", "performance");
    o.set("game_mode", gm);
    Json kb = Json::obj();
    kb.set("rgb", orgb ? Json("openrgb") : Json());
    kb.set("openrgb_device", opt_str(orgb));
    kb.set("usb", Json());
    kb.set("zones", 1);
    Json eff = Json::arr();
    eff.push("spectrum");
    kb.set("effects", eff);
    kb.set("brightness_on", 100);
    o.set("keyboard", kb);
    Json gk = Json::obj();
    for (const char* k : {"device", "evdev", "keycode_fnlock_off", "keycode_fnlock_on"}) gk.set(k, Json());
    o.set("gkey", gk);
    Json bat = Json::obj();
    bat.set("sysfs_threshold", bat_sysfs);
    bat.set("method", opt_str(bat_method));
    o.set("battery", bat);
    o.set("tweaks_skip", Json::arr());
    Json td = Json::arr();
    for (const auto& t : todo) td.push(t);
    o.set("_todo", td);
    Json gen = Json::obj();
    gen.set("by", "tuxthrottle_modelgen");
    Json dmi = Json::obj();
    dmi.set("sys_vendor", vendor);
    dmi.set("product_name", product);
    dmi.set("board_name", board);
    gen.set("host_dmi", dmi);
    o.set("_generated", gen);
    return o;
}

}  // namespace tt::modelgen

namespace tt::co {

static const char* kRunDir = "/run/tuxthrottle";
static const char* kWatchdog = "/run/tuxthrottle/co_watchdog";
static std::atomic<bool> g_interrupted{false};

static std::string user_name(const std::string& explicit_user) {
    if (!explicit_user.empty()) return explicit_user;
    std::string u = real_user("");
    return u.empty() ? "root" : u;
}
static std::string co_path(const std::string& user) { return home_of(user) + "/.config/tuxthrottle/co.json"; }

static std::vector<std::string> dmesg_faults(double since_epoch) {
    int secs = static_cast<int>(std::time(nullptr) - since_epoch) + 2;
    auto r = run_argv_stdout({"dmesg", "--since", "-" + std::to_string(secs) + "sec", "--level=err,crit,alert,emerg", "--notime"}, 8000);
    std::vector<std::string> out;
    for (const auto& ln : split_lines(r.out)) {
        std::string l = lower(ln);
        for (const char* k : {"mce", "hardware error", "whea", "uncorrected", "machine check", "gpf", "general protection"})
            if (l.find(k) != std::string::npos) { out.push_back(ln); break; }
    }
    return out;
}

static long start_cpu_load(double minutes) {
    int secs = std::max(1, static_cast<int>(minutes * 60));
    if (!which("stress-ng").empty()) return spawn_quiet({"stress-ng", "--cpu", "0", "--timeout", std::to_string(secs) + "s", "--metrics-brief"});
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return spawn_quiet({"sh", "-c", "for i in $(seq " + std::to_string(n > 0 ? n : 8) + "); do yes >/dev/null & done; sleep " +
                                        std::to_string(secs) + "; kill $(jobs -p) 2>/dev/null"});
}

static long start_gpu_load() {
    struct Cand { const char* exe; std::vector<std::string> args; };
    for (const Cand& c : {Cand{"glmark2", {"--run-forever", "--off-screen"}}, Cand{"vkmark", {"--run-forever"}}, Cand{"glxgears", {}}}) {
        if (which(c.exe).empty()) continue;
        std::vector<std::string> argv{c.exe};
        argv.insert(argv.end(), c.args.begin(), c.args.end());
        return spawn_quiet(argv, {"DISPLAY=" + env_or("DISPLAY", ":0")});
    }
    return -1;
}

static void save_co(const std::string& user, const Json& d) {
    std::string p = co_path(user);
    mkdir_p(dir_name(p));
    write_file(p, d.dump(2));
    chown_to_user(p, user);
}

static Json load_co(const std::string& user) { return read_json_file(co_path(user)); }

static void on_sigint(int) { g_interrupted = true; }

int apply(int offset_in, double minutes, bool no_gpu, const std::string& user_in) {
    std::string user = user_name(user_in);
    int offset = std::max(-50, std::min(-1, offset_in));
    if (!sensors::ryzenadj_co_supported()) { std::fputs("FAIL: ryzenadj / AMD CPU not available\n", stderr); return 1; }
    std::puts("[co] snapshot (pre-CO) ...");
    profiles::snapshot(user, "pre-curve-optimizer");
    double started = static_cast<double>(std::time(nullptr));
    mkdir_p(kRunDir);
    write_file(kWatchdog, std::to_string(started + minutes * 60 + 120));
    Json d = Json::obj();
    d.set("offset", offset);
    d.set("confirmed", false);
    d.set("applied", timestamp("%Y-%m-%d %H:%M:%S"));
    save_co(user, d);
    auto r = sensors::set_co_offset(offset);
    if (!r.first) {
        std::fprintf(stderr, "FAIL: could not apply --set-coall=%d: %s\n", offset, r.second.c_str());
        ::unlink(co_path(user).c_str());
        ::unlink(kWatchdog);
        return 1;
    }
    std::printf("[co] applied --set-coall=%d; stress-testing %g min (Ctrl-C reverts) ...\n", offset, minutes);
    std::fflush(stdout);
    std::signal(SIGINT, on_sigint);
    long cpu = start_cpu_load(minutes);
    long gpu = no_gpu ? -1 : start_gpu_load();
    if (gpu > 0) std::puts("[co] GPU load running alongside");
    double deadline = started + minutes * 60;
    std::string reason;
    while (static_cast<double>(std::time(nullptr)) < deadline && reason.empty()) {
        for (int i = 0; i < 50 && !g_interrupted; ++i) sleep_ms(100);
        if (g_interrupted) { reason = "interrupted by user"; break; }
        write_file(kWatchdog, std::to_string(static_cast<double>(std::time(nullptr)) + 120));
        auto faults = dmesg_faults(started);
        if (!faults.empty()) { reason = "kernel error: " + faults.back().substr(0, 160); break; }
        int rc = 0;
        if (cpu > 0 && !child_running(cpu, &rc) && rc != 0) { reason = "CPU stress process died (rc=" + std::to_string(rc) + ")"; break; }
    }
    child_stop(cpu);
    child_stop(gpu);
    if (!reason.empty()) {
        sensors::set_co_offset(0);
        ::unlink(co_path(user).c_str());
        ::unlink(kWatchdog);
        std::fprintf(stderr, "FAIL: %s\n[co] reverted to stock (--set-coall=0).\n", reason.c_str());
        return 1;
    }
    std::printf("OK: --set-coall=%d survived %g min of load.\n    It is applied now but NOT armed for boot. To keep it:\n"
                "      sudo tuxthrottlectl co confirm\n    Otherwise it clears on the next reboot.\n", offset, minutes);
    return 0;
}

int confirm(const std::string& user_in) {
    std::string user = user_name(user_in);
    Json d = load_co(user);
    if (d.object().empty() || d["offset"].as_number() >= 0) { std::fputs("nothing to confirm (no armed offset)\n", stderr); return 1; }
    d.set("confirmed", true);
    d.set("confirmed_at", timestamp("%Y-%m-%d %H:%M:%S"));
    save_co(user, d);
    ::unlink(kWatchdog);
    std::printf("confirmed: --set-coall=%d will be re-applied at boot (tuxthrottle-co.service).\n", static_cast<int>(d["offset"].as_number()));
    return 0;
}

int revert(const std::string& user_in) {
    std::string user = user_name(user_in);
    auto r = sensors::set_co_offset(0);
    ::unlink(co_path(user).c_str());
    ::unlink(kWatchdog);
    if (r.first) std::puts("reverted to stock CO (0).");
    else std::fprintf(stderr, "revert failed: %s\n", r.second.c_str());
    return r.first ? 0 : 1;
}

int reapply(const std::string& user_in) {
    std::string user = user_name(user_in);
    Json d = load_co(user);
    int off = static_cast<int>(d["offset"].as_number());
    if (!d["confirmed"].as_bool() || off >= 0) {
        std::printf("[co] nothing to re-apply (confirmed=%s, offset=%d)\n", d["confirmed"].as_bool() ? "True" : "False", off);
        return 0;
    }
    auto r = sensors::set_co_offset(off);
    if (r.first) std::printf("[co] re-applied --set-coall=%d\n", off);
    else std::printf("[co] re-apply failed: %s\n", r.second.c_str());
    return r.first ? 0 : 1;
}

int status(const std::string& user_in) {
    std::string user = user_name(user_in);
    Json d = load_co(user);
    Json o = Json::obj();
    o.set("co", d.object().empty() ? Json() : d);
    o.set("watchdog_armed", path_exists(kWatchdog));
    o.set("ryzenadj_co_supported", sensors::ryzenadj_co_supported());
    std::puts(o.dump(2).c_str());
    return 0;
}

}  // namespace tt::co
