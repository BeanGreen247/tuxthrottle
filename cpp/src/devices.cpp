#include "devices.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <regex>
#include <set>
#include <stdexcept>

#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::kbd {

const std::map<std::string, std::string> kEffectModes = {{"spectrum", "Spectrum Cycle"}};
static const char* kWriteLock = "/tmp/tuxthrottle-kbd.write.lock";
static const char* kReassertLock = "/tmp/tuxthrottle-kbd.reassert.lock";
static const char* kRestartStamp = "/tmp/tuxthrottle-openrgb-restart.stamp";
static const double kRestartMinGap = 20.0;

namespace {
const Json& kbd_profile() { return sensors::model_profile()["keyboard"]; }
std::string device() {
    std::string d = kbd_profile()["openrgb_device"].str();
    return d.empty() ? "Dell G Series LED Controller" : d;
}

struct Lock {
    int fd = -1;
    Lock() = default;
    Lock(const Lock&) = delete;
    Lock(Lock&& o) noexcept : fd(o.fd) { o.fd = -1; }
    ~Lock() { if (fd >= 0) close(fd); }
    explicit operator bool() const { return fd >= 0; }
};

std::optional<Lock> take_lock(const char* path, bool blocking) {
    int fd = open(path, O_RDWR | O_CREAT, 0666);
    if (fd < 0) fd = open(path, O_RDONLY);
    if (fd < 0) return std::nullopt;
    if (flock(fd, LOCK_EX | (blocking ? 0 : LOCK_NB)) != 0) { close(fd); return std::nullopt; }
    ::fchmod(fd, 0666);
    Lock l;
    l.fd = fd;
    return l;
}

std::string openrgb_exe() {
    std::string e = which("openrgb");
    if (e.empty()) throw std::runtime_error("openrgb not found - install the 'OpenRGB' app (Software tab). The G15 5515 keyboard backlight is only reachable through OpenRGB.");
    return e;
}

std::string run_once(const std::vector<std::string>& args, bool server) {
    std::vector<std::string> cmd{openrgb_exe()};
    if (!server) cmd.push_back("--noautoconnect");
    cmd.push_back("-d");
    cmd.push_back(device());
    cmd.insert(cmd.end(), args.begin(), args.end());
    auto r = run_argv(cmd, 40000);
    if (r.code == 124) throw std::runtime_error("openrgb timed out");
    if (r.out.find("not found") != std::string::npos || r.out.find("Connection attempt failed") != std::string::npos) {
        auto lines = split_lines(trim(r.out));
        throw std::runtime_error(lines.empty() ? "openrgb call failed" : lines.back());
    }
    return r.out;
}

std::string run(const std::vector<std::string>& args) {
    auto lk = take_lock(kWriteLock, true);
    bool server = true;
    std::string blob;
    try {
        blob = run_once(args, true);
    } catch (const std::exception&) {
        server = false;
        blob = run_once(args, false);
    }
    try {
        run_once(args, server);
    } catch (const std::exception&) {
    }
    return blob;
}

bool restart_too_recent() {
    struct stat st;
    if (::stat(kRestartStamp, &st) != 0) return false;
    return static_cast<double>(std::time(nullptr) - st.st_mtime) < kRestartMinGap;
}

std::string invoking_home() {
    std::string u = real_user("");
    return u.empty() ? env_or("HOME", "/root") : home_of(u);
}

void leave_effect_kick(const std::string& target = "") {
    std::string saved = load_meta().mode;
    if (kEffectModes.count(saved) && saved != target) restart_server();
}
}  // namespace

std::string hexify(const std::string& c) {
    std::string s = c;
    if (!s.empty() && s[0] == '#') s.erase(0, 1);
    for (auto& ch : s) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    return s;
}

std::string hexify(const Rgb& c) {
    char b[8];
    std::snprintf(b, sizeof b, "%02X%02X%02X", c.r & 255, c.g & 255, c.b & 255);
    return b;
}

bool valid_hex(const std::string& s_in) {
    std::string s = s_in;
    if (!s.empty() && s[0] == '#') s.erase(0, 1);
    return s.size() == 6 && std::all_of(s.begin(), s.end(), [](char c) { return std::isxdigit(static_cast<unsigned char>(c)); });
}

std::string state_path() {
    std::string o = env_or("TUXTHROTTLE_KBD_STATE");
    return !o.empty() ? o : invoking_home() + "/.config/tuxthrottle/kbd.json";
}

std::string accent_hex(const std::string& fallback) {
    std::string fb = fallback;
    if (!fb.empty() && fb[0] == '#') fb.erase(0, 1);
    if (fb.empty()) fb = "FF8800";
    auto text = read_file(invoking_home() + "/.config/kdeglobals");
    if (!text) return fb;
    std::string section;
    std::map<std::string, std::string> kv;
    for (const auto& raw : split_lines(*text)) {
        std::string l = trim(raw);
        if (l.empty() || l[0] == '#' || l[0] == ';') continue;
        if (l.front() == '[' && l.back() == ']') { section = l.substr(1, l.size() - 2); continue; }
        size_t eq = l.find('=');
        if (eq == std::string::npos) continue;
        kv[section + "|" + trim(l.substr(0, eq))] = trim(l.substr(eq + 1));
    }
    for (const char* key : {"General|AccentColor", "Colors:Selection|DecorationFocus"}) {
        auto it = kv.find(key);
        if (it == kv.end()) continue;
        const std::string& v = it->second;
        if (v.find(',') != std::string::npos) {
            int r, g, b;
            if (std::sscanf(v.c_str(), "%d,%d,%d", &r, &g, &b) == 3) return hexify(Rgb{r & 255, g & 255, b & 255});
            continue;
        }
        std::string h = v;
        if (!h.empty() && h[0] == '#') h.erase(0, 1);
        if (h.size() >= 6) return hexify(h.substr(0, 6));
    }
    return fb;
}

void save_state(const std::map<int, std::string>& zone_colors, int brightness, const std::string& mode, int speed, std::optional<bool> push_accent) {
    std::string path = state_path();
    mkdir_p(dir_name(path));
    bool pa = push_accent ? *push_accent : read_json_file(path)["push_accent"].as_bool();
    Json doc = Json::obj();
    doc.set("brightness", brightness);
    doc.set("mode", mode);
    doc.set("speed", speed);
    doc.set("push_accent", pa);
    Json zones = Json::obj();
    for (const auto& [z, c] : zone_colors) zones.set(std::to_string(z), hexify(c));
    doc.set("zones", zones);
    write_file(path, doc.dump(2, true));
    chown_to_user(path, "");
}

std::optional<SavedState> load_state() {
    Json d = read_json_file(state_path());
    SavedState st;
    for (const auto& kv : d["zones"].object()) {
        std::string c = hexify(kv.second.str());
        if (c.size() < 6) continue;
        st.zones[std::atoi(kv.first.c_str())] = Rgb{static_cast<int>(std::strtol(c.substr(0, 2).c_str(), nullptr, 16)),
                                                    static_cast<int>(std::strtol(c.substr(2, 2).c_str(), nullptr, 16)),
                                                    static_cast<int>(std::strtol(c.substr(4, 2).c_str(), nullptr, 16))};
    }
    if (st.zones.empty()) return std::nullopt;
    st.brightness = d.has("brightness") ? static_cast<int>(d["brightness"].as_number()) : 100;
    return st;
}

Meta load_meta() {
    Json d = read_json_file(state_path());
    Meta m;
    if (d.object().empty()) return m;
    if (d["mode"].is_string()) m.mode = d["mode"].as_string();
    if (d["speed"].is_number()) m.speed = static_cast<int>(d["speed"].as_number());
    m.push_accent = d["push_accent"].as_bool();
    return m;
}

std::vector<long> openrgb_server_pids(const std::string& proc) {
    std::vector<long> pids;
    for (const auto& dir : glob_paths(proc + "/[0-9]*")) {
        std::string cmd = read_file(dir + "/cmdline").value_or("");
        std::vector<std::string> argv;
        std::string cur;
        for (char c : cmd) { if (c == '\0') { argv.push_back(cur); cur.clear(); } else cur += c; }
        if (!cur.empty()) argv.push_back(cur);
        if (argv.empty() || base_name(argv[0]) != "openrgb") continue;
        if (std::find(argv.begin() + 1, argv.end(), "--server") != argv.end()) pids.push_back(std::atol(base_name(dir).c_str()));
    }
    return pids;
}

bool restart_server(bool force) {
    if (!force && restart_too_recent()) return false;
    write_file(kRestartStamp, "");
    for (const auto& cmd : {std::vector<std::string>{"systemctl", "restart", "tuxthrottle-openrgb"},
                            std::vector<std::string>{"sudo", "-n", "systemctl", "restart", "tuxthrottle-openrgb"}})
        if (run_argv(cmd, 15000).code == 0) { sleep_ms(2500); return true; }
    auto pids = openrgb_server_pids();
    for (long p : pids) kill(static_cast<pid_t>(p), SIGTERM);
    if (!pids.empty()) sleep_ms(2000);
    return !pids.empty();
}

void set_all(const std::string& color, int brightness) {
    leave_effect_kick();
    run({"-m", "Static", "-c", hexify(color), "-b", std::to_string(std::max(0, std::min(100, brightness)))});
}

void set_zone(int, const std::string& color, int brightness) { set_all(color, brightness); }

void set_zones(const std::map<int, std::string>& colors, int brightness) {
    set_all(colors.empty() ? "FFFFFF" : colors.begin()->second, brightness);
}

void off() {
    leave_effect_kick();
    run({"-b", "0"});
}

void set_effect(const std::string& name, std::optional<int> speed, int brightness) {
    leave_effect_kick(name);
    auto it = kEffectModes.find(name);
    std::vector<std::string> args{"-m", it != kEffectModes.end() ? it->second : name, "-b", std::to_string(std::max(0, std::min(100, brightness)))};
    if (speed) {
        args.push_back("-s");
        args.push_back(std::to_string(100 - std::max(0, std::min(100, *speed))));
    }
    run(args);
}

static std::map<int, std::string> hex_zones(const SavedState& st) {
    std::map<int, std::string> m;
    for (const auto& [z, c] : st.zones) m[z] = hexify(c);
    return m;
}

void reset() {
    restart_server(true);
    auto st = load_state();
    if (!st) {
        run_once({"-m", "Static", "-c", "FFFFFF", "-b", "100"}, false);
        return;
    }
    Meta meta = load_meta();
    if (kEffectModes.count(meta.mode)) set_effect(meta.mode, meta.speed, st->brightness);
    else set_zones(hex_zones(*st), st->brightness);
}

Json info() {
    auto r = run_argv_stdout({openrgb_exe(), "--noautoconnect", "-l"}, 40000);
    bool seen = r.out.find(device()) != std::string::npos || r.out.find("AW-ELC") != std::string::npos || r.out.find("AlienWare") != std::string::npos;
    std::string plat;
    for (const auto& line : split_lines(r.out)) {
        size_t p = line.find("platform:");
        if (p != std::string::npos && (line.find("Unknown") != std::string::npos || line.find("Known") != std::string::npos)) {
            auto f = split_ws(line.substr(p + 9));
            if (!f.empty()) plat = f[0].substr(0, f[0].find(','));
        }
    }
    Json o = Json::obj();
    o.set("backend", "openrgb");
    o.set("device", device());
    o.set("detected", seen);
    o.set("platform_id", plat.empty() ? "?" : plat);
    o.set("physical_zones", kZoneCount);
    return o;
}

int apply_saved(bool restart_server_first, bool quick) {
    auto st = load_state();
    if (!st) { std::puts("no saved lighting state"); return 0; }
    Meta meta = load_meta();
    int br = st->brightness;
    auto guard = take_lock(kReassertLock, false);
    if (!guard) { std::puts("another keyboard write is in progress - skipping re-assert"); return 0; }
    std::string accent_target;
    if (meta.mode == "accent") {
        Rgb fb = st->zones.empty() ? Rgb{} : st->zones.begin()->second;
        accent_target = accent_hex(hexify(fb));
    }
    auto assert_saved = [&] {
        if (kEffectModes.count(meta.mode)) set_effect(meta.mode, meta.speed, br);
        else if (meta.mode == "accent") set_all(accent_target, br);
        else set_zones(hex_zones(*st), br);
    };
    if (restart_server_first) restart_server(true);   // after resume / re-enumeration the server may hold a stale USB handle
    double deadline = now_seconds() + (quick ? 0 : 45);
    bool seen = quick;
    while (!quick && now_seconds() < deadline) {
        auto r = run_argv_stdout({openrgb_exe(), "--noautoconnect", "-l"}, 25000);
        std::string up = r.out;
        for (auto& c : up) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        if (r.out.find(device()) != std::string::npos || up.find("AW-ELC") != std::string::npos) { seen = true; break; }
        sleep_ms(2000);
    }
    if (!seen && !quick) std::fputs("warning: keyboard controller not detected yet - trying anyway\n", stderr);
    // A failed write usually means the server lost the device: restart it and try again (up to 4 attempts)
    std::optional<std::string> last;
    for (int i = 0; i < 4; ++i) {
        if (i) { sleep_ms(1500); if (last) restart_server(true); }
        try {
            assert_saved();
            last.reset();
            if (i >= 1) break;   // one clean write after any retry is enough
        } catch (const std::exception& e) {
            last = e.what();
        }
    }
    if (last) throw std::runtime_error(*last);
    std::printf("re-applied saved lighting (%s) @ %d%%\n", meta.mode.c_str(), br);
    return 0;
}

// ---- guard: watch for the things that turn the backlight off behind our back ----
static std::string usb_signature() {   // changes when the controller is re-enumerated (resume, replug, firmware reset)
    std::string sig;
    for (const auto& d : glob_paths("/sys/bus/usb/devices/*")) {
        if (read_trim(d + "/idVendor") != "187c" || read_trim(d + "/idProduct") != "0550") continue;
        sig += base_name(d) + ":" + read_trim(d + "/devnum") + ";";
    }
    return sig;
}

static bool on_ac() {
    for (const auto& p : glob_paths("/sys/class/power_supply/*/online"))
        if (read_trim(p) == "1" && lower(read_trim(dir_name(p) + "/type")) == "mains") return true;
    return false;
}

static double boottime_minus_monotonic() {   // grows by the time spent suspended
    timespec b, m;
    clock_gettime(CLOCK_BOOTTIME, &b);
    clock_gettime(CLOCK_MONOTONIC, &m);
    return static_cast<double>(b.tv_sec - m.tv_sec) + static_cast<double>(b.tv_nsec - m.tv_nsec) / 1e9;
}

// A game is running (GameMode clients, TuxThrottle's Game Mode profile, or the CPU is busy): the guard stays quiet then,
// because a re-assert makes OpenRGB enumerate the USB HID devices (about 0.4 CPU-s over several seconds).
static bool gaming_now() {
    if (sensors::gamemode_status().clients > 0 || sensors::game_mode_state()) return true;
    double l1 = 0;
    if (std::sscanf(read_file("/proc/loadavg").value_or("").c_str(), "%lf", &l1) == 1) {
        long n = sysconf(_SC_NPROCESSORS_ONLN);
        if (l1 > 0.6 * static_cast<double>(n > 0 ? n : 1)) return true;
    }
    return false;
}

int guard() {
    std::set<long> pids;
    std::string pending;
    bool pending_restart = false;
    for (long p : openrgb_server_pids()) pids.insert(p);
    std::string usb = usb_signature();
    bool ac = on_ac();
    double suspended = boottime_minus_monotonic();
    double last_apply = 0, next_periodic = now_seconds() + 1800;
    std::puts("kbd guard: watching the OpenRGB server, the USB controller, suspend/resume and the power source");
    std::fflush(stdout);
    while (true) {
        sleep_ms(3000);
        std::string why;
        bool restart = false;
        std::set<long> now_pids;
        for (long p : openrgb_server_pids()) now_pids.insert(p);
        if (now_pids.empty()) {
            // the server is gone (crash, manual kill): bring the service back; the PID change re-asserts the lighting
            if (run_argv({"systemctl", "start", "tuxthrottle-openrgb"}, 20000).code != 0) run_argv({"sudo", "-n", "systemctl", "start", "tuxthrottle-openrgb"}, 20000);
            sleep_ms(3000);
            continue;
        }
        if (now_pids != pids && !pids.empty()) why = "OpenRGB server restarted";
        pids = now_pids;
        double susp = boottime_minus_monotonic();
        if (susp - suspended > 3.0) { why = "resumed from suspend"; restart = true; sleep_ms(3000); }
        suspended = susp;
        std::string u = usb_signature();
        if (u != usb && !u.empty()) { why = "keyboard controller re-enumerated"; restart = true; sleep_ms(1500); }
        usb = u;
        bool a = on_ac();
        if (a != ac && why.empty()) why = a ? "AC connected" : "AC disconnected";
        ac = a;
        if (why.empty() && pending.empty() && now_seconds() >= next_periodic) why = "periodic safety re-assert";
        if (!why.empty()) { pending = why; pending_restart = pending_restart || restart; }
        if (pending.empty()) continue;
        // resume and USB re-enumeration must be handled, everything else waits for the game to end
        if (!pending_restart && gaming_now()) continue;
        if (!pending_restart && now_seconds() - last_apply < 15) continue;   // debounce bursts of events
        why = pending; restart = pending_restart; pending.clear(); pending_restart = false;
        last_apply = now_seconds();
        next_periodic = last_apply + 1800;
        std::printf("kbd guard: %s - re-applying saved lighting\n", why.c_str());
        std::fflush(stdout);
        try {
            apply_saved(restart, !restart);
            pids.clear();
            for (long p : openrgb_server_pids()) pids.insert(p);   // our own restart must not count as an event
        } catch (const std::exception& e) {
            std::printf("kbd guard: re-apply failed (%s), will retry\n", e.what());
            std::fflush(stdout);
            next_periodic = now_seconds() + 60;
        }
    }
}

int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: kbd {on|off|zone|effect|apply-saved [--restart-server]|guard|reset|info}"); return 2; }
    auto opt = [&](const char* f) -> std::optional<std::string> {
        for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1];
        return std::nullopt;
    };
    auto num = [&](const char* f, int def) { auto v = opt(f); return v ? std::atoi(v->c_str()) : def; };
    try {
        const std::string& cmd = a[0];
        if (cmd == "info") {
            Json inf = info();
            for (const auto& kv : inf.object()) {
                std::string v = kv.second.is_string() ? kv.second.as_string() : kv.second.dump();
                std::printf("%-15s: %s\n", kv.first.c_str(), v.c_str());
            }
        } else if (cmd == "on") {
            std::string color = opt("--color").value_or("FFFFFF");
            if (!valid_hex(color)) { std::fputs("colour must be RRGGBB hex\n", stderr); return 2; }
            int br = num("--brightness", 100);
            set_all(color, br);
            std::map<int, std::string> z;
            for (int i = 0; i < kZoneCount; ++i) z[i] = hexify(color);
            save_state(z, br);
            std::printf("backlight on: #%s @ %d%%\n", lower(hexify(color)).c_str(), br);
        } else if (cmd == "off") {
            off();
            std::puts("backlight off");
        } else if (cmd == "reset") {
            reset();
            std::puts("backlight reset (server restarted, saved state re-applied)");
        } else if (cmd == "zone") {
            if (a.size() < 2) return 2;
            int idx = std::atoi(a[1].c_str());
            auto color = opt("--color");
            if (idx < 0 || idx >= kZoneCount || !color || !valid_hex(*color)) { std::fputs("zone <0-3> --color RRGGBB\n", stderr); return 2; }
            auto st = load_state();
            std::map<int, std::string> colors;
            if (st) colors = hex_zones(*st); else for (int i = 0; i < kZoneCount; ++i) colors[i] = "FFFFFF";
            int br = opt("--brightness") ? std::atoi(opt("--brightness")->c_str()) : (st ? st->brightness : 100);
            colors[idx] = hexify(*color);
            set_zones(colors, br);
            save_state(colors, br);
            static const char* names[] = {"Left", "Middle", "Right", "Numpad"};
            std::printf("zone %d (%s) -> #%s @ %d%%\n", idx, names[idx], lower(hexify(*color)).c_str(), br);
        } else if (cmd == "effect") {
            if (a.size() < 2 || !kEffectModes.count(a[1])) { std::fputs("effect spectrum [--speed 0-100] [--brightness 0-100]\n", stderr); return 2; }
            auto st = load_state();
            std::map<int, std::string> colors;
            if (st) colors = hex_zones(*st); else for (int i = 0; i < kZoneCount; ++i) colors[i] = "FFFFFF";
            int speed = num("--speed", 50), br = num("--brightness", 100);
            set_effect(a[1], speed, br);
            save_state(colors, br, a[1], speed);
            std::printf("effect %s @ speed %d, %d%%\n", a[1].c_str(), speed, br);
        } else if (cmd == "apply-saved") {
            return apply_saved(std::find(a.begin(), a.end(), "--restart-server") != a.end());
        } else if (cmd == "guard") {
            return guard();
        } else {
            return 2;
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 1;
    }
    return 0;
}

}  // namespace tt::kbd

namespace tt::crashwatch {

namespace {
struct Sig {
    const char* id;
    const char* match_exe;
    const char* match_cmdline;  // may be null
    bool benign;
    const char* label;
    const char* hint;
};
const std::vector<Sig>& signatures() {
    static const std::vector<Sig> s = {
        {"proton-bootstrap-quit", "wine-preloader", "d3ddriverquery64.exe", true, "Proton bootstrap driver check (normal)",
         "Steam quits this helper itself after querying your GPU driver - not a real crash."},
        {"steamwebhelper-crash", "steamwebhelper", nullptr, false, "Steam's web UI (steamwebhelper) crashed",
         "If this repeats, open Game Tools -> Steam client low-resource mode and make sure no custom -cef-single-process / -no-cef-sandbox / "
         "-no-browser flags are set - those are known to crash-loop the CEF process on current Steam builds."},
        {"steam-client-crash", "ubuntu12_32/steam", nullptr, false, "The Steam client crashed",
         "If it happens right after login, Steam was probably started twice: a leftover `steam.desktop.<something>` backup in ~/.config/autostart "
         "is run as a second Steam, the two race each other and one segfaults. TuxThrottle moves such files out automatically when it sees this."},
        {"wine-preloader-crash", "wine-preloader", nullptr, false, "A Windows game/Proton process crashed",
         "Usually game-specific - check that game's Proton log (compatdata/<appid>/pfx or PROTON_LOG=1) for the real error."},
    };
    return s;
}
struct JSig { const char* id; const char* pattern; const char* label; const char* hint; };
const std::vector<JSig>& journal_signatures() {
    static const std::vector<JSig> s = {
        {"ntfs-dirty", "ntfs3.*volume is dirty", "An NTFS drive was left 'dirty' by Windows and failed to mount",
         "Enable the NtfsForceMount tweak (Stability tab), or turn off Windows 'Fast Startup' (Control Panel -> Power Options) to stop it recurring."},
        {"scx-stall", "sched_ext.*(disabl|error)|scx_lavd.*(stall|error)", "The sched_ext scheduler (scx_lavd) reported a problem",
         "See project notes: scx_lavd is known to stall/freeze on some kernels - leave it off and use the stock EEVDF scheduler."},
    };
    return s;
}
SteamHook g_steam_hook;

std::string state_file(const std::string& user) {
    std::string home = user.empty() ? env_or("HOME", "/root") : home_of(user);
    return home + "/.local/share/tuxthrottle/crashwatch-state.json";
}
}  // namespace

void set_steam_double_start_hook(SteamHook h) { g_steam_hook = std::move(h); }

std::optional<Finding> classify(const std::string& exe, const std::string& cmdline) {
    std::string e = lower(exe), c = lower(cmdline);
    for (const auto& s : signatures()) {
        if (e.find(lower(s.match_exe)) == std::string::npos) continue;
        if (s.match_cmdline && c.find(lower(s.match_cmdline)) == std::string::npos) continue;
        Finding f;
        f.id = s.id;
        f.label = s.label;
        f.hint = s.hint;
        f.benign = s.benign;
        return f;
    }
    return std::nullopt;
}

std::vector<Finding> scan(int since, const std::string& user) {
    Json state = read_json_file(state_file(user));
    std::set<std::string> seen;
    for (const auto& s : state["seen"].array()) seen.insert(s.str());
    std::vector<Finding> findings;
    std::vector<std::string> order(seen.begin(), seen.end());

    auto r = run_argv_stdout({"coredumpctl", "list", "--json=short", "--since", "-" + std::to_string(since) + "s"}, 15000);
    Json rows;
    try { if (!trim(r.out).empty()) rows = Json::parse(r.out); } catch (const std::exception&) {}
    for (const auto& row : rows.array()) {
        std::string pid = row["pid"].is_number() ? std::to_string(static_cast<long>(row["pid"].as_number())) : row["pid"].str();
        if (pid.empty()) continue;
        std::string exe = row["exe"].str();
        std::string ts = row["time"].is_number() ? std::to_string(static_cast<long long>(row["time"].as_number())) : row["time"].str();
        std::string key = "core:" + pid + ":" + ts;
        if (seen.count(key)) continue;
        seen.insert(key);
        order.push_back(key);
        std::string cmdline;
        for (const auto& il : split_lines(run_argv_stdout({"coredumpctl", "info", pid}, 15000).out)) {
            std::string t = trim(il);
            if (starts_with(t, "Command Line:")) { cmdline = trim(t.substr(13)); break; }
        }
        auto sig = classify(exe, cmdline);
        Finding f;
        if (sig) f = *sig;
        else {
            std::string name = exe.empty() ? "An unidentified process" : base_name(exe);
            f.id = "unknown-crash";
            f.label = name + " crashed";
            f.hint = "No known signature for this one yet.";
        }
        if (f.id == "steam-client-crash" && g_steam_hook)
            if (auto h = g_steam_hook(f)) f = *h;
        f.detail = exe;
        findings.push_back(f);
    }
    auto jr = run_argv_stdout({"journalctl", "--user", "--since=-" + std::to_string(since) + "s", "--no-pager", "-o", "cat"}, 15000);
    for (const auto& js : journal_signatures()) {
        std::regex re(js.pattern, std::regex::icase | std::regex::extended);
        if (!std::regex_search(jr.out, re)) continue;
        std::string key = std::string("journal:") + js.id + ":" + std::to_string(since) + ":" + std::to_string(static_cast<long long>(std::time(nullptr) / since));
        if (seen.count(key)) continue;
        seen.insert(key);
        order.push_back(key);
        Finding f;
        f.id = js.id;
        f.label = js.label;
        f.hint = js.hint;
        f.detail = "journal";
        findings.push_back(f);
    }
    Json arr = Json::arr();
    size_t start = order.size() > 200 ? order.size() - 200 : 0;
    for (size_t i = start; i < order.size(); ++i) arr.push(order[i]);
    Json out = Json::obj();
    out.set("seen", arr);
    std::string p = state_file(user);
    mkdir_p(dir_name(p));
    write_file(p, out.dump());
    return findings;
}

int main(const std::vector<std::string>& a) {
    if (a.empty() || a[0] != "scan") { std::puts("usage: crashwatch scan [--since S] [--json] [--notify] [--user U]"); return 2; }
    int since = 120;
    std::string user;
    bool as_json = false, notify = false;
    for (size_t i = 1; i < a.size(); ++i) {
        if (a[i] == "--since" && i + 1 < a.size()) since = std::atoi(a[++i].c_str());
        else if (a[i] == "--user" && i + 1 < a.size()) user = a[++i];
        else if (a[i] == "--json") as_json = true;
        else if (a[i] == "--notify") notify = true;
    }
    auto findings = scan(since, user);
    if (notify)
        for (const auto& f : findings) if (!f.benign) sensors::notify(f.label, f.hint);
    if (as_json) {
        Json arr = Json::arr();
        for (const auto& f : findings) {
            Json o = Json::obj();
            o.set("id", f.id);
            o.set("label", f.label);
            o.set("hint", f.hint);
            o.set("benign", f.benign);
            o.set("detail", f.detail);
            arr.push(o);
        }
        std::puts(arr.dump(2).c_str());
    } else if (findings.empty()) std::puts("no new findings");
    else for (const auto& f : findings) std::printf("[%s] %s - %s\n", f.benign ? "benign" : "!", f.label.c_str(), f.hint.c_str());
    return 0;
}

}  // namespace tt::crashwatch
