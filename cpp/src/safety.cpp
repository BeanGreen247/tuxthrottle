#include "safety.hpp"

#include <algorithm>
#include <cstdio>
#include <stdexcept>

#include "util.hpp"

namespace tt::btrfs {

static const char* kConfig = "root";
static const std::string kPrefix = "tuxthrottle: ";

bool is_btrfs_root() {
    auto r = run_argv_stdout({"findmnt", "-n", "-o", "FSTYPE", "/"}, 5000);
    return r.code == 0 && trim(r.out) == "btrfs";
}

bool snapper_available() {
    if (which("snapper").empty()) return false;
    return run_argv_stdout({"snapper", "-c", kConfig, "list"}, 10000).code == 0;
}

std::optional<std::string> method() {
    if (!is_btrfs_root()) return std::nullopt;
    if (snapper_available()) return std::string("snapper");
    return std::nullopt;
}

bool available() { return method().has_value(); }

Snapshot create_snapshot(const std::string& description) {
    Snapshot s;
    auto m = method();
    if (!m) { s.msg = "not on a Btrfs root with snapper configured - skipped"; return s; }
    s.method = *m;
    auto r = run_argv_stdout({"snapper", "-c", kConfig, "create", "--type", "single", "--print-number", "--description",
                              kPrefix + description}, 30000);
    if (r.code != 0) { s.msg = "snapper exit " + std::to_string(r.code); return s; }
    s.id = trim(r.out);
    s.ok = true;
    s.msg = "snapper snapshot #" + s.id + " created";
    return s;
}

std::vector<Row> list_snapshots(size_t limit) {
    std::vector<Row> rows;
    if (!snapper_available()) return rows;
    auto r = run_argv_stdout({"snapper", "-c", kConfig, "list", "--columns", "number,date,description"}, 10000);
    if (r.code != 0) return rows;
    for (const auto& line : split_lines(r.out)) {
        if (line.find(kPrefix) == std::string::npos) continue;
        std::vector<std::string> parts;
        std::string cur;
        for (char c : line + "|") {
            if (c == '|') { parts.push_back(trim(cur)); cur.clear(); }
            else cur += c;
        }
        if (parts.size() < 3) continue;
        bool digits = !parts[0].empty() && std::all_of(parts[0].begin(), parts[0].end(), [](char c) { return c >= '0' && c <= '9'; });
        if (!digits) continue;
        std::string desc = parts[2];
        if (starts_with(desc, kPrefix)) desc = desc.substr(kPrefix.size());
        rows.push_back({parts[0], parts[1], desc});
    }
    std::sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) { return std::stol(a.number) > std::stol(b.number); });
    if (rows.size() > limit) rows.resize(limit);
    return rows;
}

std::string rollback_hint(const std::string& number) {
    return std::string("sudo snapper -c ") + kConfig + " rollback " + number + "   # then reboot";
}

}  // namespace tt::btrfs

namespace tt::watchdog {

std::string arm(int seconds, const std::string& user, const std::string& ctl_path) {
    if (seconds < 1) throw std::invalid_argument("seconds must be >= 1");
    std::string hex;
    {
        FILE* f = std::fopen("/dev/urandom", "rb");
        unsigned char b[6] = {0};
        if (f) { if (std::fread(b, 1, sizeof b, f) != sizeof b) {} std::fclose(f); }
        static const char* digits = "0123456789abcdef";
        for (unsigned char c : b) { hex += digits[c >> 4]; hex += digits[c & 15]; }
    }
    std::string unit = kUnitPrefix + hex;
    std::string ctl = ctl_path.empty() ? real_path("/proc/self/exe") : ctl_path;
    auto r = run_argv({"systemd-run", "--unit=" + unit, "--collect", "--on-active=" + std::to_string(seconds),
                       "--description=TuxThrottle auto-revert watchdog", ctl, "rollback", "last", "--user", user}, 15000);
    if (r.code == -1) throw std::runtime_error("systemd-run unavailable");
    if (r.code != 0) throw std::runtime_error("systemd-run exit " + std::to_string(r.code) + ": " + trim(r.out));
    return unit;
}

bool disarm(const std::string& unit) {
    if (!starts_with(unit, kUnitPrefix)) return false;
    bool ok = true;
    for (const std::string& target : {unit + ".timer", unit + ".service"}) {
        auto r = run_argv({"systemctl", "stop", target}, 10000);
        if (r.code != 0 && lower(r.out).find("not loaded") == std::string::npos) ok = false;
    }
    run_argv({"systemctl", "reset-failed", unit}, 10000);
    return ok;
}

bool is_armed(const std::string& unit) {
    return trim(run_argv_stdout({"systemctl", "is-active", unit + ".timer"}, 10000).out) == "active";
}

}  // namespace tt::watchdog
