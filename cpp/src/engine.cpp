#include "engine.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <ctime>
#include <cstdio>
#include <mutex>
#include <set>
#include <tuple>
#include <thread>

#include <regex>

#include "distro.hpp"
#include "profiles.hpp"
#include "safety.hpp"
#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::engine {

std::string config_dir() { return sensors::app_dir() + "/config"; }

// The shared config still names the Python helpers; map each to its tuxthrottlectl subcommand / the daemon binary.
static std::string rewrite_python_helpers(const std::string& in0) {
    // one-off shell fragments that referenced Python modules / files directly
    std::string in = in0;
    std::string ctl = shell_quote(ctl_exe());
    in = replace_all(in, "cd {TOOLKIT_DIR} && python3 -c \"import tuxthrottle_shadercache as s; print(s.cache_dir())\"", ctl + " shadercache dir");
    in = replace_all(in, "{TTD}", shell_quote(dir_name(ctl_exe()) + "/tuxthrottled"));
    {   // the helpers are built into the binaries now: a chmod on the old script is a no-op
        static const std::regex chmod_py(R"re(chmod \+x \{TOOLKIT_DIR\}/\w+\.py)re");
        in = std::regex_replace(in, chmod_py, ":");
    }
    in = replace_all(in, "ln -sf {TOOLKIT_DIR}/hotkey_listener.py /usr/local/bin/tuxthrottle-hotkey-listener.py", ":");
    in = replace_all(in, "/usr/bin/python3 /usr/local/bin/tuxthrottle-hotkey-listener.py", ctl + " hotkey");
    in = replace_all(in, "{PKG_INSTALL:python3-evdev}", ":");
    in = replace_all(in, "install -m 0755 {TOOLKIT_DIR}/tuxthrottle_kbd.py /usr/local/bin/tuxthrottle-kbd",
                     "printf '%s\\n' '#!/bin/sh' 'exec " + ctl + " kbd \"$@\"' > /usr/local/bin/tuxthrottle-kbd && chmod 0755 /usr/local/bin/tuxthrottle-kbd");
    static const std::map<std::string, std::string> sub = {
        {"prefix_relocate", "prefix"}, {"compat_tools", "compat"}, {"shadercache", "shadercache"}, {"steamperf", "steamperf"},
        {"profiles", "profile"}, {"kde_panel", "kde-panel"}, {"drivehealth", "drives"}, {"co_stress", "co"}, {"savevault", "savevault"},
        {"vram", "vram"}, {"kbd", "kbd"}, {"fpslimit", "fpslimit"}, {"heroicopts", "heroicopts"}, {"lutrisopts", "lutrisopts"},
        {"launchopts", "launchopts"}, {"lutris_env", "lutris-env"}, {"mangohud_games", "mangohud-games"}, {"mangohud_status", "mangohud-status"},
        {"fixlog", "fixlog"}, {"crashwatch", "crashwatch"}, {"bench", "bench"}, {"hwprobe", "diag"}, {"gpudrivers", "gpu-drivers"},
        {"kargs", "kargs"}, {"automount", "automount"}, {"watchdog", "watchdog"}, {"btrfs", "btrfs-snapshot"}, {"protondb", "protondb"},
        {"distro", "distro"}};
    static const std::regex re(R"re((?:/usr/bin/)?python3 \{TOOLKIT_DIR\}/(tuxthrottle_(\w+)|apply_tweak)\.py( run)?)re");
    return regex_replace_fn(in, re, [&](const std::smatch& m) -> std::string {
        std::string name = m[2].matched ? m[2].str() : "";
        if (!m[2].matched) return shell_quote(ctl_exe()) + " apply-tweak";
        if (name == "powerd") return shell_quote(dir_name(ctl_exe()) + "/tuxthrottled") + (m[3].matched ? " run" : "");
        auto it = sub.find(name);
        if (it == sub.end()) return m[0].str();
        return shell_quote(ctl_exe()) + " " + it->second;
    });
}

std::string substitute(const std::string& cmd, const std::string& user) {
    std::string c = replace_all(cmd, "{USER}", user);
    c = rewrite_python_helpers(c);
    c = replace_all(c, "{TOOLKIT_DIR}", sensors::app_dir());
    c = replace_all(c, "{TT}", shell_quote(ctl_exe()));
    return expand(c, current_distro(), shell_quote(ctl_exe()) + " kargs");
}

static std::vector<std::string> str_list(const Json& j) {
    std::vector<std::string> v;
    for (const auto& x : j.array()) v.push_back(x.str());
    return v;
}

Item make_item(const std::string& id, const Json& data, const std::string& kind, const std::string& user) {
    Item it;
    it.id = id;
    it.kind = kind;
    it.content = data["Content"].is_string() ? data["Content"].as_string() : id;
    it.description = data["Description"].str();
    it.category = data["category"].is_string() ? data["category"].as_string() : "Other";
    if (data["risk"].is_string()) it.risk = data["risk"].as_string();
    it.recommended = data["recommended"].as_bool();
    it.requires_vendor = data["requires_vendor"].str();
    if (data["models"].is_array()) it.requires_models = str_list(data["models"]);
    it.reinstall_if = str_list(data["reinstall_if"]);
    auto sub = [&](const std::string& c) { return substitute(c, user); };
    it.check_cmd = sub(data["check"].str());
    it.check_pending_cmd = sub(data["check_pending"].str());
    if (kind == "tweak") {
        for (const auto& c : data["apply"].array()) it.apply_cmds.push_back(sub(c.str()));
        for (const auto& c : data["undo"].array()) it.undo_cmds.push_back(sub(c.str()));
    } else {
        std::string manager = data["manager"].is_string() ? data["manager"].as_string() : "dnf";
        std::string package = data["package"].is_string() ? data["package"].as_string() : id;
        std::vector<std::string> alts;
        for (const auto& p : data["provides"].array()) alts.push_back(sub(p.str()));
        if (manager == "dnf") alts.push_back(sub("{PKG_CHECK:" + package + "}"));
        if (!data["binary"].str().empty()) alts.push_back("command -v " + data["binary"].str() + " >/dev/null 2>&1");
        if (manager == "flatpak") {
            alts.push_back("flatpak info " + package + " >/dev/null 2>&1");
            alts.push_back(sub("sudo -u {USER} flatpak info " + package + " >/dev/null 2>&1"));
        }
        if (!alts.empty()) {
            std::string base = trim(it.check_cmd);
            if (base.empty()) base = "false";
            std::vector<std::string> parts{"(" + base + ")"};
            for (const auto& a : alts) parts.push_back("(" + a + ")");
            it.check_cmd = join(parts, " || ");
        }
        if (data.has("install")) {
            for (const auto& c : data["install"].array()) it.apply_cmds.push_back(sub(c.str()));
        } else if (manager == "dnf") {
            it.apply_cmds = {sub("{PKG_INSTALL:" + package + "}")};
        } else if (manager == "flatpak") {
            it.apply_cmds = {sub("{PKG_INSTALL:flatpak}"),
                             "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo",
                             "flatpak install -y flathub " + package};
        }
    }
    return it;
}

static void gate(Item& it, bool has_nv, bool has_amd) {
    if (it.requires_vendor == "nvidia" && !has_nv) {
        it.hw_supported = false;
        it.description += "  (no NVIDIA GPU detected on this system - disabled)";
    } else if (it.requires_vendor == "amd" && !has_amd) {
        it.hw_supported = false;
        it.description += "  (no AMD GPU detected on this system - disabled)";
    }
    std::vector<std::string> cmds = it.apply_cmds;
    cmds.insert(cmds.end(), it.undo_cmds.begin(), it.undo_cmds.end());
    if (!portable(cmds, current_distro())) {
        it.hw_supported = false;
        it.description += "  (needs a Fedora-family system - not available on " + current_distro().name + ")";
    }
    if (!sensors::model_allows(it.requires_models) || sensors::model_skips_tweak(it.id)) {
        it.hidden = true;
        it.hw_supported = false;
    }
    if (it.id == "RaplPowerPermissions" && sensors::rapl_permissions_ok() && !path_exists("/etc/udev/rules.d/90-tuxthrottle-powercap-perms.rules"))
        it.hidden = true;
}

std::vector<Item> load_items(const std::string& user_in) {
    std::string user = user_in.empty() ? resolve_real_user() : user_in;
    load_pkgmap(config_dir());
    bool has_nv = sensors::has_nvidia_gpu(), has_amd = sensors::has_amd_gpu();
    std::vector<Item> items;
    for (auto [kind, file] : {std::pair<const char*, const char*>{"tweak", "tweaks.json"}, {"app", "apps.json"}}) {
        Json data = read_json_file(config_dir() + "/" + file);
        for (const auto& kv : data.object()) {
            Item it = make_item(kv.first, kv.second, kind, user);
            gate(it, has_nv, has_amd);
            items.push_back(std::move(it));
        }
    }
    return items;
}

Json load_presets() { return read_json_file(config_dir() + "/presets.json"); }

// ---------------------------------------------------------------- ledger
static std::string ledger_path() {
    std::string o = env_or("TUXTHROTTLE_STATE");   // lets the tests keep their records out of the real ledger
    return !o.empty() ? o : home_of(resolve_real_user()) + "/.config/tuxthrottle/state.json";
}

Json ledger_load() { return read_json_file(ledger_path()); }

void ledger_record(const std::string& id, const std::string& action, bool ok, const std::string& note) {
    Json data = ledger_load();
    Json e = Json::obj();
    e.set("action", action);
    e.set("ok", ok);
    e.set("ts", timestamp("%Y-%m-%d %H:%M:%S"));
    e.set("note", note.substr(0, 200));
    data.set(id, e);
    std::string p = ledger_path();
    mkdir_p(dir_name(p));
    if (write_file(p, data.dump(2, true))) {
        if (is_root()) {
            std::string u = resolve_real_user();
            chown_to_user(p, u);
            chown_to_user(dir_name(p), u);
        }
    }
}

// ---------------------------------------------------------------- evaluation
// pkexec / systemd strip PATH down to /usr/bin:/bin; check commands look for tools installed to these too
static void ensure_tool_path() {
    static std::once_flag once;
    std::call_once(once, [] {
        std::string p = env_or("PATH", "");
        std::string add;
        for (const std::string& d : {std::string("/usr/local/bin"), std::string("/usr/local/sbin"), std::string("/usr/sbin"), std::string("/sbin"),
                                     home_of(resolve_real_user()) + "/.local/bin", std::string("/var/lib/flatpak/exports/bin")})
            if (p.find(d) == std::string::npos) add += (add.empty() ? "" : ":") + d;
        if (!add.empty()) setenv("PATH", (p.empty() ? add : p + ":" + add).c_str(), 1);
    });
}

// A check that dies on a timeout or on a package-manager lock is retried once before it is believed
static RunResult run_check(const std::string& cmd) {
    auto r = run_shell(cmd);
    if (r.code == -1 || r.code == 124 || (r.code != 0 && r.out.find("lock") != std::string::npos)) {
        sleep_ms(1200);
        r = run_shell(cmd);
    }
    return r;
}

void evaluate_item(Item& it, const Json& ledger) {
    ensure_tool_path();
    it.check_rc.reset();
    it.check_out.clear();
    it.ledger = ledger[it.id];
    if (!it.hw_supported) { it.state = "unsupported"; return; }
    if (it.check_cmd.empty()) { it.state = "not_applied"; return; }
    auto r = run_check(it.check_cmd);
    it.check_rc = r.code;
    it.check_out = trim(r.out).substr(0, 600);
    if (r.code == 0) { it.state = "applied"; return; }
    const Json& led = it.ledger;
    if (r.code == -1 || r.code == 127) it.state = "error";
    else if (!it.check_pending_cmd.empty() && run_shell(it.check_pending_cmd).code == 0) it.state = "pending";
    else if (led.is_object() && led["action"].str() == "apply" && led["ok"].as_bool()) it.state = "drifted";
    else if (led.is_object() && !led["ok"].as_bool()) it.state = "failed";
    else it.state = "not_applied";
}

void evaluate_all(std::vector<Item>& items, int workers) {
    Json ledger = ledger_load();
    std::atomic<size_t> next{0};
    auto work = [&] {
        for (size_t i; (i = next.fetch_add(1)) < items.size();) evaluate_item(items[i], ledger);
    };
    std::vector<std::thread> pool;
    for (int i = 0; i < std::max(1, workers); ++i) pool.emplace_back(work);
    for (auto& t : pool) t.join();
}

std::string state_label(const Item& it) {
    static const std::map<std::string, std::string> labels = {
        {"applied", "Applied"}, {"pending", "Pending reboot"}, {"not_applied", "Not applied"}, {"error", "Check error"},
        {"drifted", "Not detected now"}, {"failed", "Apply failed"}, {"unsupported", "unsupported"}, {"unknown", "checking..."}};
    std::string l = labels.count(it.state) ? labels.at(it.state) : "checking...";
    if (it.kind == "app") {
        if (l == "Applied") return "Installed";
        if (l == "Not applied") return "Not installed";
    }
    return l;
}

std::string format_status_report(const std::vector<Item>& items_in) {
    std::vector<const Item*> rows;
    for (const auto& it : items_in) rows.push_back(&it);
    std::sort(rows.begin(), rows.end(), [](const Item* a, const Item* b) {
        return std::make_tuple(a->category, a->kind, lower(a->content)) < std::make_tuple(b->category, b->kind, lower(b->content));
    });
    std::string out = "TuxThrottle - status report   " + timestamp("%Y-%m-%d %H:%M:%S") + "\n" + std::string(100, '=') + "\n";
    std::string cur;
    std::map<std::string, int> counts;
    char b[512];
    for (const Item* it : rows) {
        ++counts[it->state];
        if (it->category != cur) { cur = it->category; out += "\n[" + cur + "]\n"; }
        std::string up = it->state;
        for (auto& c : up) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        std::string rc = (!it->check_rc || *it->check_rc == 0) ? "" : " (rc " + std::to_string(*it->check_rc) + ")";
        std::snprintf(b, sizeof b, "  %-12s %-44s check%s: %s\n", up.c_str(), it->content.substr(0, 44).c_str(), rc.c_str(),
                      it->check_cmd.empty() ? "(none)" : it->check_cmd.substr(0, 60).c_str());
        out += b;
        if (!it->check_out.empty() && (it->state == "error" || it->state == "drifted" || it->state == "failed" || it->state == "not_applied")) {
            auto lines = split_lines(it->check_out);
            if (!lines.empty()) out += "    -> check said: " + lines.back().substr(0, 88) + "\n";
        }
        if (it->ledger.is_object()) {
            const Json& l = it->ledger;
            out += "    -> toolkit:    " + l["action"].str() + (l["ok"].as_bool() ? " ok " : " FAILED ") + l["ts"].str() +
                   (l["note"].str().empty() ? "" : " - " + l["note"].str()) + "\n";
        }
    }
    out += "\n" + std::string(100, '-') + "\n";
    std::vector<std::string> cs;
    for (const auto& kv : counts) cs.push_back(kv.first + "=" + std::to_string(kv.second));
    out += join(cs, "  ") + "\n";
    return out;
}

// ---------------------------------------------------------------- runners
static bool stream_cmd(const std::string& cmd, const LogFn& log) {
    int rc = run_shell_stream(cmd, [&](const std::string& l) { if (!l.empty()) log(l); }, 3600 * 1000);
    if (rc == 124) { log("[TIMEOUT] command ran over 60 min - killed"); return false; }
    return rc == 0;
}

bool apply_item(const Item& it, const LogFn& log) {
    if (it.kind == "app" && !it.check_cmd.empty() && run_shell(it.check_cmd).code == 0) {
        log("[skip, already present] " + it.content + " - nothing to install");
        ledger_record(it.id, "apply", true, "already present (another source); skipped");
        return true;
    }
    log("--- Applying: " + it.content + " ---");
    int n = 0;
    for (const auto& cmd : it.apply_cmds) {
        ++n;
        if (!stream_cmd(cmd, log)) {
            log("[FAILED] " + cmd);
            ledger_record(it.id, "apply", false, "failed at step " + std::to_string(n) + "/" + std::to_string(it.apply_cmds.size()) + ": " + cmd);
            return false;
        }
    }
    // every step exited 0: now prove the result with the item's own check before claiming success
    if (!it.check_cmd.empty()) {
        auto r = run_check(it.check_cmd);
        bool pending = r.code != 0 && !it.check_pending_cmd.empty() && run_shell(it.check_pending_cmd).code == 0;
        if (r.code != 0 && !pending) {
            std::string why = trim(r.out).substr(0, 160);
            log("[VERIFY FAILED] the steps ran without errors, but the check still says it is not there: " + it.check_cmd.substr(0, 200) + (why.empty() ? "" : "  ->  " + why));
            ledger_record(it.id, "apply", false, "steps ok but verification failed (rc " + std::to_string(r.code) + ")");
            return false;
        }
        if (pending) log("[OK, pending] " + it.content + " - takes effect after a reboot or re-login");
    }
    log("[OK] " + it.content);
    ledger_record(it.id, "apply", true, std::to_string(it.apply_cmds.size()) + " cmd(s) ok, verified");
    return true;
}

bool undo_item(const Item& it, const LogFn& log) {
    log("--- Reverting: " + it.content + " ---");
    int n = 0;
    for (const auto& cmd : it.undo_cmds) {
        ++n;
        if (!stream_cmd(cmd, log)) {
            log("[FAILED] " + cmd);
            ledger_record(it.id, "undo", false, "failed at step " + std::to_string(n) + "/" + std::to_string(it.undo_cmds.size()) + ": " + cmd);
            return false;
        }
    }
    if (!it.check_cmd.empty() && run_check(it.check_cmd).code == 0)
        log("[note] the check still passes after the undo steps (another source may provide it, or a reboot is needed): " + it.content);
    log("[OK reverted] " + it.content);
    ledger_record(it.id, "undo", true, std::to_string(it.undo_cmds.size()) + " cmd(s) ok");
    return true;
}

void pre_risky_snapshot(const std::string& label, const LogFn& log) {
    try {
        log("[snapshot] pre-apply rollback point: " + base_name(profiles::snapshot("", label)));
    } catch (const std::exception& e) {
        log(std::string("[snapshot] couldn't capture a rollback point: ") + e.what());
    }
    auto res = btrfs::create_snapshot(label);
    if (res.ok) log("[btrfs] " + res.msg + " - " + btrfs::rollback_hint(res.id));
    else log("[btrfs] " + res.msg);
}

std::optional<std::string> arm_watchdog_if_risky(const std::vector<const Item*>& items, int seconds, const std::string& user, const LogFn& log) {
    bool risky = false;
    for (const Item* it : items) risky = risky || (it && it->risk == "advanced");
    if (!risky) return std::nullopt;
    try {
        std::string unit = watchdog::arm(seconds, user);
        log("[watchdog] armed - auto-revert in " + std::to_string(seconds) + "s unless confirmed (" + unit + ")");
        return unit;
    } catch (const std::exception& e) {
        log(std::string("[watchdog] couldn't arm auto-revert timer: ") + e.what());
        return std::nullopt;
    }
}

static Item* find_item(std::vector<Item>& items, const std::string& id) {
    for (auto& it : items) if (it.id == id) return &it;
    return nullptr;
}

BatchResult apply_batch(std::vector<Item>& items, const std::vector<std::string>& ids, const std::string& label, const LogFn& log, const std::string& user) {
    BatchResult br;
    pre_risky_snapshot(label, log);
    std::vector<const Item*> touched;
    for (const auto& id : ids) {
        Item* it = find_item(items, id);
        if (!it || !it->hw_supported) { if (it) log("[skip, unsupported] " + it->content); continue; }
        if (it->done()) {
            ++br.skipped;
            log(std::string("[skip, already ") + (it->pending() ? "pending reboot" : (it->kind == "app" ? "installed" : "applied")) + "] " + it->content);
            continue;
        }
        touched.push_back(it);
        if (apply_item(*it, log)) ++br.applied; else ++br.failed;
    }
    log("=== Done. Run `tuxthrottlectl tweaks status` to confirm. ===");
    if (auto u = arm_watchdog_if_risky(touched, 20, user.empty() ? resolve_real_user() : user, log)) br.watchdog_unit = *u;
    return br;
}

BatchResult undo_batch(std::vector<Item>& items, const std::vector<std::string>& ids, const LogFn& log, const std::string& user) {
    BatchResult br;
    pre_risky_snapshot("pre-undo", log);
    std::vector<const Item*> touched;
    for (const auto& id : ids) {
        Item* it = find_item(items, id);
        if (!it || it->kind != "tweak" || it->undo_cmds.empty()) { if (it) log("[skip, nothing to undo] " + it->content); continue; }
        if (!it->applied()) { ++br.skipped; log("[skip, not applied] " + it->content); continue; }
        touched.push_back(it);
        if (undo_item(*it, log)) ++br.applied; else ++br.failed;
    }
    (void)user;
    log("=== Done. ===");
    return br;
}

std::vector<const Item*> recommended(const std::vector<Item>& items, const std::string& category, bool pending_only) {
    std::vector<const Item*> out;
    for (const auto& it : items) {
        if (!it.recommended || !it.hw_supported || it.hidden) continue;
        if (!category.empty() && it.category != category) continue;
        if (pending_only && it.done()) continue;
        out.push_back(&it);
    }
    return out;
}

// ---------------------------------------------------------------- version / metadata
std::string toolkit_version() {
    std::string base = sensors::app_dir();
    if (!path_exists(base + "/.git")) {
        std::string v = read_trim(base + "/.version");
        if (!v.empty()) return v;
    }
    std::string dv = trim(run_argv_stdout({"git", "-C", base, "log", "-1", "--format=%cd", "--date=format:%y.%m.%d"}, 5000).out);
    std::string v = read_trim(base + "/VERSION");
    if (!dv.empty() && !v.empty()) return std::max(dv, v);
    if (!dv.empty() || !v.empty()) return dv.empty() ? v : dv;
    return "unknown";
}

static std::optional<double> dnf_metadata_secs() {
    double newest = 0;
    for (const char* pat : {"/var/cache/dnf/*/repodata/repomd.xml", "/var/cache/libdnf5/*/repodata/repomd.xml"})
        for (const auto& p : glob_paths(pat)) {
            struct stat st;
            if (::stat(p.c_str(), &st) == 0) newest = std::max(newest, static_cast<double>(st.st_mtime));
        }
    if (newest == 0) return std::nullopt;
    return std::max(0.0, static_cast<double>(std::time(nullptr)) - newest);
}

std::string dnf_metadata_age() {
    auto s = dnf_metadata_secs();
    if (!s) return "";
    if (*s < 90) return "as of just now";
    if (*s < 5400) return "as of " + std::to_string(static_cast<long>(std::lround(*s / 60))) + " min ago";
    if (*s < 172800) return "as of " + std::to_string(static_cast<long>(std::lround(*s / 3600))) + " h ago";
    return "as of " + std::to_string(static_cast<long>(std::lround(*s / 86400))) + " d ago";
}

bool dnf_metadata_stale(double threshold) {
    auto s = dnf_metadata_secs();
    return !s || *s >= threshold;
}

// ---------------------------------------------------------------- CLI
static Json item_json(const Item& it) {
    Json o = Json::obj();
    o.set("id", it.id);
    o.set("kind", it.kind);
    o.set("content", it.content);
    o.set("category", it.category);
    o.set("risk", it.risk);
    o.set("recommended", it.recommended);
    o.set("supported", it.hw_supported);
    o.set("hidden", it.hidden);
    o.set("state", it.state);
    return o;
}

// installed units, scripts and cron jobs from the Python era that point at files the C++ install no longer ships
static bool stale_python_refs() {
    std::string home = home_of(resolve_real_user());
    std::string cmd = "grep -rlE '(tuxthrottle[_a-z-]*\\.py|/opt/tuxthrottle/[a-z_]+\\.py|hotkey-listener\\.py|hotkey_listener\\.py|apply_tweak\\.py)' "
                      "/etc/systemd/system /usr/lib/systemd/system-sleep /usr/local/bin /etc/cron.d /etc/cron.daily /etc/cron.hourly /etc/udev/rules.d " +
                      shell_quote(home + "/.config/systemd/user") + " " + shell_quote(home + "/.local/bin") + " 2>/dev/null | head -1";
    return !trim(run_shell(cmd).out).empty();
}

int cli_tweaks(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: tweaks {list|status|report|apply|undo|apply-recommended} [ids...] [--json] [--category C] [--kind tweak|app]"); return 2; }
    std::string sub = a[0];
    bool as_json = std::find(a.begin(), a.end(), "--json") != a.end();
    std::string category, kind;
    std::vector<std::string> ids;
    for (size_t i = 1; i < a.size(); ++i) {
        if (a[i] == "--category" && i + 1 < a.size()) category = a[++i];
        else if (a[i] == "--kind" && i + 1 < a.size()) kind = a[++i];
        else if (!starts_with(a[i], "--")) ids.push_back(a[i]);
    }
    auto items = load_items();
    LogFn log = [](const std::string& l) { std::puts(l.c_str()); std::fflush(stdout); };
    auto selected = [&](const Item& it) { return (category.empty() || it.category == category) && (kind.empty() || it.kind == kind); };
    if (sub == "list" || sub == "status") {
        if (sub == "status") evaluate_all(items);
        Json arr = Json::arr();
        for (const auto& it : items) {
            if (it.hidden || !selected(it)) continue;
            if (as_json) { arr.push(item_json(it)); continue; }
            std::printf("%-12s %-9s %-34s %s\n", sub == "status" ? state_label(it).c_str() : (it.hw_supported ? "-" : "unsupported"),
                        it.category.c_str(), it.id.c_str(), it.content.c_str());
        }
        if (as_json) std::puts(arr.dump(2).c_str());
        return 0;
    }
    if (sub == "refresh-legacy") {
        bool force_all = std::find(a.begin(), a.end(), "--all") != a.end();
        // re-run the apply of every enabled tweak whose installed units/scripts still call the Python helpers
        Json raw = read_json_file(config_dir() + "/tweaks.json");
        evaluate_all(items);
        int n = 0, bad = 0;
        for (auto& it : items) {
            if (it.kind != "tweak" || !it.hw_supported || !it.done()) continue;
            std::string all;
            for (const auto& c : raw[it.id]["apply"].array()) all += c.str() + "\n";
            // tweaks that install a unit / script / cron job calling the helpers: legacy Python text or the {TT}/{TTD}/hotkey placeholders
            bool calls_helper = all.find("python3 {TOOLKIT_DIR}") != std::string::npos || all.find("{TOOLKIT_DIR}/hotkey_listener.py") != std::string::npos ||
                                all.find("{TOOLKIT_DIR}/tuxthrottle_kbd.py") != std::string::npos || all.find("{TT}") != std::string::npos || all.find("{TTD}") != std::string::npos;
            if (!calls_helper) continue;
            if (!stale_python_refs() && !force_all) continue;
            log("--- refreshing " + it.content + " ---");
            ++n;
            if (!apply_item(it, log)) ++bad;
        }
        std::printf("refreshed %d tweak(s), %d failed\n", n, bad);
        return bad ? 1 : 0;
    }
    if (sub == "report") {
        evaluate_all(items);
        std::puts(format_status_report(items).c_str());
        if (!is_root()) std::puts("note: not running as root - privileged checks may read as 'Not applied'/'Check error'. Re-run with sudo for accuracy.");
        return 0;
    }
    if (sub == "apply" || sub == "undo" || sub == "apply-recommended") {
        if (!is_root()) { std::fputs("error: applying tweaks needs root (use sudo)\n", stderr); return 1; }
        evaluate_all(items);
        if (sub == "apply-recommended")
            for (const Item* it : recommended(items, category, true)) ids.push_back(it->id);
        if (ids.empty()) { std::puts("nothing selected"); return 0; }
        BatchResult br = sub == "undo" ? undo_batch(items, ids, log) : apply_batch(items, ids, "pre-apply-selected", log);
        std::printf("applied/reverted=%d skipped=%d failed=%d\n", br.applied, br.skipped, br.failed);
        return br.failed ? 1 : 0;
    }
    return 2;
}

int cli_preset(const std::vector<std::string>& a) {
    Json presets = load_presets();
    if (a.empty() || a[0] == "list") {
        for (const auto& kv : presets.object())
            std::printf("%-20s %s (%zu tweaks, %zu apps)\n", kv.first.c_str(), kv.second["Content"].str().c_str(), kv.second["tweaks"].array().size(),
                        kv.second["apps"].array().size());
        return 0;
    }
    if (a[0] == "apply" && a.size() > 1) {
        if (!presets.has(a[1])) { std::fprintf(stderr, "error: no such preset %s\n", a[1].c_str()); return 1; }
        if (!is_root()) { std::fputs("error: applying a preset needs root (use sudo)\n", stderr); return 1; }
        auto items = load_items();
        evaluate_all(items);
        std::vector<std::string> ids = str_list(presets[a[1]]["tweaks"]);
        for (const auto& i : str_list(presets[a[1]]["apps"])) ids.push_back(i);
        auto before = sensors::snapshot_light();
        (void)before;
        BatchResult br = apply_batch(items, ids, "pre-preset-" + a[1], [](const std::string& l) { std::puts(l.c_str()); std::fflush(stdout); });
        std::printf("applied=%d skipped=%d failed=%d\n", br.applied, br.skipped, br.failed);
        return br.failed ? 1 : 0;
    }
    return 2;
}

int cli_apply_tweak(const std::vector<std::string>& a) {
    std::string id, toolkit_dir = sensors::app_dir(), user;
    bool only_if_present = false;
    for (size_t i = 0; i < a.size(); ++i) {
        if (a[i] == "--only-if-present") only_if_present = true;
        else if (a[i] == "--toolkit-dir" && i + 1 < a.size()) toolkit_dir = a[++i];
        else if (a[i] == "--user" && i + 1 < a.size()) user = a[++i];
        else if (!starts_with(a[i], "--")) id = a[i];
    }
    if (id.empty()) { std::puts("usage: apply-tweak <TweakId> [--only-if-present] [--toolkit-dir D] [--user U]"); return 2; }
    while (toolkit_dir.size() > 1 && toolkit_dir.back() == '/') toolkit_dir.pop_back();
    if (user.empty()) {
        user = env_or("SUDO_USER");
        if (user.empty() || user == "root") user = env_or("PKEXEC_USER");
        if (user.empty() || user == "root") user = trim(run_argv_stdout({"logname"}, 3000).out);
        if (user.empty()) user = env_or("USER", "root");
    }
    Json tweaks = read_json_file(toolkit_dir + "/config/tweaks.json");
    if (tweaks.object().empty()) { std::fprintf(stderr, "apply_tweak: cannot read %s/config/tweaks.json\n", toolkit_dir.c_str()); return 2; }
    if (!tweaks.has(id)) { std::fprintf(stderr, "apply_tweak: no such tweak '%s'\n", id.c_str()); return 2; }
    load_pkgmap(toolkit_dir + "/config");
    const Json& t = tweaks[id];
    auto sub = [&](const std::string& c) {
        std::string x = rewrite_python_helpers(replace_all(c, "{USER}", user));
        return expand(replace_all(replace_all(x, "{TOOLKIT_DIR}", toolkit_dir), "{TT}", shell_quote(ctl_exe())),
                      current_distro(), shell_quote(ctl_exe()) + " kargs");
    };
    if (only_if_present) {
        bool present = run_shell(sub(t["check"].is_string() ? t["check"].as_string() : "false")).code == 0;
        if (!present)
            for (const auto& pat : t["reinstall_if"].array())
                if (!glob_paths(sub(pat.str())).empty()) { present = true; break; }
        if (!present) { std::printf("apply_tweak: %s not enabled - skipped\n", id.c_str()); return 3; }
    }
    std::vector<std::string> cmds;
    for (const auto& c : t["apply"].array()) cmds.push_back(sub(c.str()));
    int i = 0;
    for (const auto& cmd : cmds) {
        ++i;
        std::string head = split_lines(cmd).empty() ? cmd : split_lines(cmd)[0];
        std::printf("[%d/%zu] %s\n", i, cmds.size(), head.substr(0, 88).c_str());
        std::fflush(stdout);
        int rc = run_shell_stream(cmd, [](const std::string& l) { std::puts(l.c_str()); std::fflush(stdout); });
        if (rc != 0) std::fprintf(stderr, "  !! exit %d\n", rc);
    }
    bool ok = run_shell(sub(t["check"].is_string() ? t["check"].as_string() : "true")).code == 0;
    std::printf("%s: check %s after apply\n", id.c_str(), ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

}  // namespace tt::engine
