#include "hotkey.hpp"

#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <thread>

#include "devices.hpp"
#include "gamecfg.hpp"
#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::hotkey {

bool double_tap_fires(double ts, double* last_press, double window_s) {
    double gap = ts - *last_press;
    if (gap > 0 && gap <= window_s) { *last_press = 0.0; return true; }
    *last_press = ts;
    return false;
}

static std::atomic<bool> g_busy{false};

static void fire_toggle() {
    if (g_busy.exchange(true)) { std::puts("Toggle already in progress - ignoring"); std::fflush(stdout); return; }
    std::thread([] {
        auto r = sensors::toggle_game_mode();
        if (!r.first) { std::printf("Toggle failed: %s\n", r.second.c_str()); std::fflush(stdout); }
        g_busy = false;
    }).detach();
}

static std::string dev_name(int fd) {
    char buf[256] = {0};
    if (ioctl(fd, EVIOCGNAME(sizeof buf - 1), buf) < 0) return "";
    return buf;
}

static int find_device(const std::vector<std::string>& hints, std::string* path_out, std::string* name_out) {
    int unreadable = 0;
    for (const auto& p : glob_paths("/dev/input/event*")) {
        int fd = open(p.c_str(), O_RDONLY);
        if (fd < 0) { ++unreadable; continue; }
        std::string name = dev_name(fd);
        for (const auto& h : hints)
            if (name == h) { *path_out = p; *name_out = name; return fd; }
        close(fd);
    }
    if (unreadable)
        std::printf("Can't read %d input device(s) incl. the keyboard - your user is not in the 'input' group. Apply the HotkeyListener tweak (it runs 'usermod -aG input'), then log out and back in.\n", unreadable);
    return -1;
}

int main(const std::vector<std::string>&) {
    const Json& gkey = sensors::model_profile()["gkey"];
    int target = KEY_PERFORMANCE;
    std::string code_name = gkey["keycode_fnlock_off"].str();
    if (!code_name.empty()) {
        // the profile names an evdev constant; map the ones a G-key can be
        static const std::pair<const char*, int> known[] = {{"KEY_PERFORMANCE", KEY_PERFORMANCE}, {"KEY_PROG1", KEY_PROG1}, {"KEY_PROG2", KEY_PROG2},
                                                            {"KEY_PROG3", KEY_PROG3}, {"KEY_PROG4", KEY_PROG4}, {"KEY_F9", KEY_F9}, {"KEY_F13", KEY_F13}};
        for (auto& [n, c] : known) if (code_name == n) target = c;
    }
    std::vector<std::string> hints;
    if (!gkey["device"].str().empty()) hints.push_back(gkey["device"].str());
    if (std::find(hints.begin(), hints.end(), "AT Translated Set 2 keyboard") == hints.end()) hints.push_back("AT Translated Set 2 keyboard");
    std::string mode = lower(trim(env_or("TUXTHROTTLE_HOTKEY_MODE", "single")));
    int double_ms = std::atoi(env_or("TUXTHROTTLE_HOTKEY_DOUBLE_MS", "600").c_str());
    if (double_ms <= 0) double_ms = 600;
    bool single = mode == "single";
    double window = double_ms / 1000.0;
    while (true) {
        std::string path, name;
        int fd = find_device(hints, &path, &name);
        if (fd < 0) { std::puts("Keyboard device not found, retrying in 5s..."); std::fflush(stdout); sleep_ms(5000); continue; }
        std::printf("Listening on %s (%s) for KEY_PERFORMANCE - trigger: %s\n", path.c_str(), name.c_str(),
                    single ? "single press" : ("double-tap (<" + std::to_string(double_ms) + "ms)").c_str());
        std::fflush(stdout);
        double last_press = 0;
        struct input_event ev;
        while (true) {
            ssize_t n = read(fd, &ev, sizeof ev);
            if (n != static_cast<ssize_t>(sizeof ev)) break;
            if (ev.type != EV_KEY || ev.code != target || ev.value != 1) continue;
            double ts = static_cast<double>(ev.time.tv_sec) + static_cast<double>(ev.time.tv_usec) / 1e6;
            if (single) { fire_toggle(); continue; }
            if (double_tap_fires(ts, &last_press, window)) fire_toggle();
        }
        close(fd);
        std::puts("Device disconnected, retrying...");
        std::fflush(stdout);
        sleep_ms(2000);
    }
}

}  // namespace tt::hotkey

namespace tt::tui {

namespace {
struct Raw {
    struct termios old;
    bool ok = false;
    Raw() {
        if (tcgetattr(STDIN_FILENO, &old) == 0) {
            struct termios t = old;
            t.c_lflag &= static_cast<tcflag_t>(~(ICANON | ECHO));
            t.c_cc[VMIN] = 0;
            t.c_cc[VTIME] = 0;
            ok = tcsetattr(STDIN_FILENO, TCSANOW, &t) == 0;
        }
        std::fputs("\x1b[?1049h\x1b[?25l", stdout);
    }
    ~Raw() {
        std::fputs("\x1b[?25h\x1b[?1049l", stdout);
        std::fflush(stdout);
        if (ok) tcsetattr(STDIN_FILENO, TCSANOW, &old);
    }
};

std::pair<bool, std::string> ctl(const std::vector<std::string>& args) { return privileged_ctl(args); }

std::string cell(const std::string& label, const std::string& value) { return "\x1b[1m" + label + "\x1b[0m\n  \x1b[38;5;86m" + value + "\x1b[0m"; }
}  // namespace

int main(const std::vector<std::string>&) {
    Raw raw;
    std::string notice;
    double notice_until = 0;
    double next = 0, next_scan = 0;
    std::string cpu, igpu, dgpu, gm;
    auto refresh = [&] {
        auto t = sensors::cpu_temp_c();
        auto f = sensors::cpu_freq_ghz();
        auto p = sensors::cpu_power_watts();
        char b[160];
        std::snprintf(b, sizeof b, "%s, %s%s", t ? (format_double(*t, 0) + " C").c_str() : "n/a", f ? (format_double(*f, 2) + " GHz").c_str() : "n/a",
                      p ? (", " + format_double(*p, 1) + " W").c_str() : "");
        cpu = b;
        auto ig = sensors::igpu_values();
        igpu = (ig.clock_mhz ? std::to_string(*ig.clock_mhz) + " MHz" : "?") + ", " + (ig.temp_c ? format_double(*ig.temp_c, 0) + " C" : "?");
        auto dg = sensors::dgpu_values();
        dgpu = !dg.clock_mhz ? "n/a (no nvidia-smi / asleep)"
                             : std::to_string(*dg.clock_mhz) + " MHz, " + std::to_string(dg.temp_c.value_or(0)) + " C, " + std::to_string(dg.util_pct.value_or(0)) + "% util" +
                                   (dg.power_w ? ", " + format_double(*dg.power_w, 0) + " W" : "");
        gm = sensors::game_mode_state() ? "ON" : "OFF";
    };
    while (true) {
        double now = now_seconds();
        if (now >= next) { refresh(); next = now + 2.0; }
        if (now >= next_scan) {
            for (const auto& f : crashwatch::scan(16)) fixlog::log_event("crashwatch", f.label, f.benign ? "info" : "warn");
            next_scan = now + 15.0;
        }
        std::string out = "\x1b[2J\x1b[H\x1b[1;38;5;86mTuxThrottle\x1b[0m - live dashboard   (g Game Mode  0/5/1 fan boost  r refresh  q quit)\n\n";
        out += cell("CPU", cpu) + "\n\n" + cell("iGPU", igpu) + "\n\n" + cell("dGPU", dgpu) + "\n\n" + cell("Game Mode", gm) + "\n\n";
        out += "\x1b[1;38;5;86mRecent fixes / crash-watch findings\x1b[0m\n";
        Json entries = fixlog::read_recent(12);
        if (entries.array().empty()) out += "  (nothing logged yet)\n";
        for (const auto& e : entries.array()) {
            std::time_t ts = static_cast<std::time_t>(e["ts"].as_number());
            std::tm tm;
            localtime_r(&ts, &tm);
            char hm[16];
            std::strftime(hm, sizeof hm, "%H:%M", &tm);
            std::string lvl = e["level"].str();
            const char* col = lvl == "warn" ? "\x1b[33m" : lvl == "error" ? "\x1b[31m" : "\x1b[2m";
            char line[512];
            std::snprintf(line, sizeof line, "  %s%s  %-16s %s\x1b[0m\n", col, hm, e["source"].str().c_str(), e["message"].str().c_str());
            out += line;
        }
        if (!notice.empty() && now < notice_until) out += "\n" + notice + "\n";
        std::fputs(out.c_str(), stdout);
        std::fflush(stdout);
        struct pollfd pfd = {STDIN_FILENO, POLLIN, 0};
        if (poll(&pfd, 1, 500) > 0) {
            char c = 0;
            if (read(STDIN_FILENO, &c, 1) == 1) {
                if (c == 'q') break;
                if (c == 'r') next = 0;
                else if (c == 'g') {
                    auto r = ctl({"gamemode", "toggle"});
                    notice = r.first ? "Game Mode toggled" : "Game Mode change failed: " + r.second;
                    notice_until = now_seconds() + 4;
                    next = 0;
                } else if (c == '0' || c == '5' || c == '1') {
                    int pct = c == '0' ? 0 : c == '5' ? 50 : 100;
                    auto r = ctl({"set", "fan-boost", "both", std::to_string(pct)});
                    notice = r.first ? "Fan boost -> " + std::to_string(pct) + "%" : "Fan boost failed: " + r.second;
                    notice_until = now_seconds() + 4;
                }
            }
        }
    }
    return 0;
}

}  // namespace tt::tui
