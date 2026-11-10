#include "desktop.hpp"

#include <pwd.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <regex>
#include <set>

#include "json.hpp"
#include "util.hpp"

namespace tt::kdepanel {

static const char* kApplets = "plasma-org.kde.plasma.desktop-appletsrc";

static std::string config_home() {
    std::string x = env_or("XDG_CONFIG_HOME");
    return x.empty() ? env_or("HOME", "/root") + "/.config" : x;
}
static std::string cfg_path() { return config_home() + "/" + kApplets; }

static std::vector<std::pair<std::string, std::string>> find_applets(const std::string& plugin) {
    std::vector<std::pair<std::string, std::string>> out;
    auto text = read_file(cfg_path());
    if (!text) return out;
    static const std::regex hdr(R"(^\[Containments\]\[(\d+)\]\[Applets\]\[(\d+)\]$)");
    std::pair<std::string, std::string> cur;
    bool have = false;
    for (const auto& line : split_lines(*text)) {
        std::smatch m;
        if (std::regex_match(line, m, hdr)) { cur = {m[1], m[2]}; have = true; }
        else if (trim(line) == "plugin=" + plugin && have) out.push_back(cur);
    }
    return out;
}

static void kwrite(const std::vector<std::string>& groups, const std::string& key, const std::optional<std::string>& value,
                   const std::string& file = kApplets) {
    std::vector<std::string> args{"kwriteconfig6", "--file", file};
    for (const auto& g : groups) { args.push_back("--group"); args.push_back(g); }
    args.push_back("--key");
    args.push_back(key);
    if (value) args.push_back(*value);
    else args.push_back("--delete");
    run_argv(args);
}

static void kwrite_applet(const std::string& cid, const std::string& aid, const std::vector<std::string>& chain, const std::string& key,
                          const std::optional<std::string>& value) {
    std::vector<std::string> groups{"Containments", cid, "Applets", aid};
    groups.insert(groups.end(), chain.begin(), chain.end());
    kwrite(groups, key, value);
}

static std::string kread(const std::vector<std::string>& groups, const std::string& key) {
    std::vector<std::string> args{"kreadconfig6", "--file", kApplets};
    for (const auto& g : groups) { args.push_back("--group"); args.push_back(g); }
    args.push_back("--key");
    args.push_back(key);
    return trim(run_argv_stdout(args).out);
}

static std::vector<std::string> csv(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s + ",") {
        if (c == ',') { std::string t = trim(cur); if (!t.empty()) out.push_back(t); cur.clear(); }
        else cur += c;
    }
    return out;
}

static void stop_plasmashell() {
    if (run_argv({"systemctl", "--user", "stop", "plasma-plasmashell.service"}).code != 0) run_argv({"kquitapp6", "plasmashell"});
    sleep_ms(2000);
}

static void restart_plasmashell() {
    if (run_argv({"systemctl", "--user", "restart", "plasma-plasmashell.service"}).code == 0) return;
    run_argv({"kquitapp6", "plasmashell"});
    run_argv({"sh", "-c", "sleep 1; setsid kstart plasmashell >/dev/null 2>&1 &"});
}

static int systray_show(const std::string& plugin, bool enable) {
    auto applets = find_applets("org.kde.plasma.systemtray");
    if (applets.empty()) { std::fputs("no system-tray applet in the panel config\n", stderr); return 0; }
    stop_plasmashell();
    for (const auto& [cid, aid] : applets) {
        std::vector<std::string> base{"Containments", cid, "Applets", aid, "General"};
        auto shown = csv(kread(base, "shownItems"));
        auto hidden = csv(kread(base, "hiddenItems"));
        if (enable) {
            if (std::find(shown.begin(), shown.end(), plugin) == shown.end()) shown.push_back(plugin);
            hidden.erase(std::remove(hidden.begin(), hidden.end(), plugin), hidden.end());
        } else {
            shown.erase(std::remove(shown.begin(), shown.end(), plugin), shown.end());
        }
        kwrite_applet(cid, aid, {"General"}, "shownItems", shown.empty() ? std::nullopt : std::optional<std::string>(join(shown, ",")));
        kwrite_applet(cid, aid, {"General"}, "hiddenItems", hidden.empty() ? std::nullopt : std::optional<std::string>(join(hidden, ",")));
        kwrite_applet(cid, aid, {"Configuration", "General"}, "shownItems", std::nullopt);
        kwrite_applet(cid, aid, {"Configuration", "General"}, "hiddenItems", std::nullopt);
    }
    std::printf("%s -> %s in %zu system tray(s)\n", plugin.c_str(), enable ? "always shown" : "auto", applets.size());
    return 0;
}

static const char* kLauncherPlugins[] = {"org.kde.plasma.kickoff", "org.kde.plasma.kicker", "org.kde.plasma.kickerdash"};
static const char* kSystemFavorites = "suspend,hibernate,reboot,shutdown,lock-screen,logout,save-session,switch-user";
static const char* kKickerActions = "lock-screen,logout,save-session,switch-user,suspend,hibernate,reboot,shutdown";

static size_t restore_launcher_power(bool enable) {
    size_t n = 0;
    for (const char* plug : kLauncherPlugins)
        for (const auto& [cid, aid] : find_applets(plug)) {
            ++n;
            kwrite_applet(cid, aid, {"Configuration", "General"}, "systemFavorites", enable ? std::optional<std::string>(kSystemFavorites) : std::nullopt);
            kwrite_applet(cid, aid, {"Configuration", "General"}, "favoriteSystemActions", enable ? std::optional<std::string>(kKickerActions) : std::nullopt);
            if (enable) kwrite_applet(cid, aid, {"Configuration", "General"}, "favoritesPortedToKAstats", std::nullopt);
        }
    return n;
}

static int launcher_power(bool enable) {
    bool any = false;
    for (const char* plug : kLauncherPlugins) any = any || !find_applets(plug).empty();
    if (!any) { std::fputs("no application-launcher applet in the panel config\n", stderr); return 0; }
    stop_plasmashell();
    size_t n = restore_launcher_power(enable);
    std::printf("launcher power buttons -> %s on %zu launcher(s)\n", enable ? "full set" : "default", n);
    return 0;
}

static int clock_seconds(bool enable) {
    auto applets = find_applets("org.kde.plasma.digitalclock");
    if (applets.empty()) { std::fputs("no digital-clock applet in the panel config\n", stderr); return 0; }
    for (const auto& [cid, aid] : applets)
        kwrite_applet(cid, aid, {"Configuration", "Appearance"}, "showSeconds", enable ? std::optional<std::string>("2") : std::nullopt);
    std::printf("showSeconds %s on %zu clock applet(s)\n", enable ? "2" : "default", applets.size());
    return 0;
}

static int classic_menu(bool enable) {
    std::string p = cfg_path();
    auto text = read_file(p);
    if (!text) { std::fputs("panel config not found\n", stderr); return 1; }
    stop_plasmashell();
    text = read_file(p);
    std::string out;
    static const std::regex to_kicker(R"(^plugin=org\.kde\.plasma\.(kickoff|kickerdash)$)");
    static const std::regex to_kickoff(R"(^plugin=org\.kde\.plasma\.kicker$)");
    bool first = true;
    for (const auto& line : split_lines(*text)) {
        std::string l = enable ? std::regex_replace(line, to_kicker, "plugin=org.kde.plasma.kicker")
                               : std::regex_replace(line, to_kickoff, "plugin=org.kde.plasma.kickoff");
        out += (first ? "" : "\n") + l;
        first = false;
    }
    if (!text->empty() && text->back() == '\n') out += "\n";
    if (out != *text) {
        write_file(p, out);
        std::puts(enable ? "launcher plugin -> kicker (classic menu)" : "launcher plugin -> kickoff");
    } else std::puts("launcher plugin already as requested");
    if (enable) restore_launcher_power(true);
    return 0;
}

static std::vector<std::string> find_panels() {
    std::vector<std::string> out;
    auto text = read_file(cfg_path());
    if (!text) return out;
    static const std::regex hdr(R"(^\[Containments\]\[(\d+)\](\[.+\])?$)");
    std::string cur;
    bool have = false, is_panel = false;
    for (const auto& line : split_lines(*text)) {
        std::smatch m;
        if (std::regex_match(line, m, hdr) && !m[2].matched) {
            if (have && is_panel) out.push_back(cur);
            cur = m[1];
            have = true;
            is_panel = false;
        } else if (trim(line) == "plugin=org.kde.panel" && have) is_panel = true;
    }
    if (have && is_panel) out.push_back(cur);
    return out;
}

static std::vector<std::vector<std::string>> view_groups(const std::string& cid) {
    std::vector<std::string> base{"PlasmaViews", "Panel " + cid};
    std::vector<std::vector<std::string>> groups{base};
    auto rc = read_file(config_home() + "/plasmashellrc");
    if (rc) {
        std::regex pat("^\\[PlasmaViews\\]\\[Panel " + cid + "\\]\\[([^\\]]+)\\]$");
        for (const auto& line : split_lines(*rc)) {
            std::smatch m;
            if (std::regex_match(line, m, pat) && m[1] != "Defaults") {
                auto g = base;
                g.push_back(m[1]);
                groups.push_back(g);
            }
        }
    }
    return groups;
}

static int panel_floating(bool enable) {
    auto panels = find_panels();
    if (panels.empty()) { std::fputs("no panel containment in the desktop config\n", stderr); return 0; }
    for (const auto& cid : panels) {
        kwrite({"Containments", cid, "General"}, "floating", enable ? "true" : "false");
        for (const auto& chain : view_groups(cid)) kwrite(chain, "floating", enable ? "1" : "0", "plasmashellrc");
    }
    std::printf("panel floating -> %s on %zu panel(s)\n", enable ? "on" : "off (flush)", panels.size());
    return 0;
}

int main(const std::vector<std::string>& args) {
    if (args.size() != 2 || (args[1] != "on" && args[1] != "off")) {
        std::puts("usage: kde-panel {clock-seconds|classic-menu|panel-floating|battery-tray|launcher-power} {on|off}");
        return 2;
    }
    bool state = args[1] == "on";
    int rc;
    if (args[0] == "clock-seconds") rc = clock_seconds(state);
    else if (args[0] == "classic-menu") rc = classic_menu(state);
    else if (args[0] == "panel-floating") rc = panel_floating(state);
    else if (args[0] == "battery-tray") rc = systray_show("org.kde.plasma.battery", state);
    else if (args[0] == "launcher-power") rc = launcher_power(state);
    else { std::fprintf(stderr, "unknown action: %s\n", args[0].c_str()); return 2; }
    restart_plasmashell();
    return rc;
}

}  // namespace tt::kdepanel

namespace tt::automount {

static const char* kFstab = "/etc/fstab";
static const char* kMnt = "/mnt";
static const std::string kBegin = "# >>> tuxthrottle AutoMountDrives (managed) - edit above/below, not inside";
static const std::string kEnd = "# <<< tuxthrottle AutoMountDrives";
static const char* kCommon = "rw,noatime,nofail,x-systemd.device-timeout=10,x-gvfs-show";
static const std::set<std::string> kSupported = {"ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "ntfs", "exfat", "vfat"};

static bool is_ours_begin(const std::string& l) {
    return starts_with(l, "# >>> tuxthrottle AutoMountDrives (managed)") || starts_with(l, "# >>> dellg15-toolkit AutoMountDrives (managed)");
}
static bool is_ours_end(const std::string& l) {
    return starts_with(l, "# <<< tuxthrottle AutoMountDrives") || starts_with(l, "# <<< dellg15-toolkit AutoMountDrives");
}

// splits fstab text into {text without our block, our block (or "")}
static std::pair<std::string, std::string> split_block(const std::string& text) {
    auto lines = split_lines(text);
    std::string body, block;
    bool in = false;
    for (const auto& l : lines) {
        if (!in && is_ours_begin(l)) { in = true; block += l + "\n"; continue; }
        if (in) {
            block += l + "\n";
            if (is_ours_end(l)) in = false;
            continue;
        }
        body += l + "\n";
    }
    return {body, block};
}

static std::set<std::string> fstab_uuids(const std::string& text) {
    std::set<std::string> uu;
    for (const auto& raw : split_lines(split_block(text).first)) {
        std::string line = trim(raw);
        if (line.empty() || line[0] == '#') continue;
        std::string tok = split_ws(line)[0];
        if (starts_with(lower(tok), "uuid=")) uu.insert(tok.substr(5));
    }
    return uu;
}

static std::string sanitize(const std::string& label) {
    std::string s;
    bool us = false;
    for (char c : label) {
        if (std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '_' || c == '-') { s += c; us = false; }
        else if (!us) { s += '_'; us = true; }
    }
    size_t a = s.find_first_not_of("_."), b = s.find_last_not_of("_.");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

static void flatten(const Json& nodes, std::vector<Json>& out) {
    for (const auto& n : nodes.array()) {
        out.push_back(n);
        flatten(n["children"], out);
    }
}

std::vector<Candidate> candidates_from(const std::string& lsblk_json, const std::string& fstab_text) {
    std::vector<Json> rows;
    try {
        flatten(Json::parse(lsblk_json)["blockdevices"], rows);
    } catch (const std::exception&) {
    }
    auto fstab = fstab_uuids(fstab_text);
    std::set<std::string> osd;
    for (const auto& r : rows) {
        std::string mp = r["mountpoint"].str();
        if (mp == "/" || mp == "/home" || mp == "/var" || starts_with(mp, "/boot") || r["fstype"].str() == "swap")
            osd.insert(!r["pkname"].str().empty() ? r["pkname"].str() : r["name"].str());
    }
    auto truthy = [](const Json& v) { return v.is_bool() ? v.as_bool() : (v.is_number() ? v.as_number() != 0 : v.str() == "1"); };
    std::set<std::string> used;
    std::vector<Candidate> out;
    for (const auto& r : rows) {
        if (r["type"].str() != "part") continue;
        std::string fs = lower(r["fstype"].str()), uuid = r["uuid"].str();
        if (!kSupported.count(fs) || uuid.empty()) continue;
        if (!r["mountpoint"].str().empty()) continue;
        if (fstab.count(uuid)) continue;
        if (osd.count(r["pkname"].str())) continue;
        if (truthy(r["rm"]) || truthy(r["hotplug"])) continue;
        std::string name = sanitize(r["label"].str());
        if (name.empty()) name = fs + "-" + uuid.substr(0, 8);
        struct stat st;
        bool mounted_here = ::stat((std::string(kMnt) + "/" + name).c_str(), &st) == 0 && false;
        if (used.count(name) || mounted_here) name += "-" + uuid.substr(0, 8);
        used.insert(name);
        out.push_back({r["path"].str(), uuid, fs, r["label"].str(), r["size"].str(), std::string(kMnt) + "/" + name});
    }
    return out;
}

std::string fstab_line(const Candidate& c, int uid, int gid) {
    std::string typ, opts, ids = "uid=" + std::to_string(uid) + ",gid=" + std::to_string(gid);
    if (c.fstype == "ntfs") { typ = "ntfs3"; opts = ids + ",umask=022," + kCommon; }
    else if (c.fstype == "exfat") { typ = "exfat"; opts = ids + ",umask=022," + kCommon; }
    else if (c.fstype == "vfat") { typ = "vfat"; opts = ids + ",umask=022,utf8," + kCommon; }
    else { typ = c.fstype; opts = kCommon; }
    return "UUID=" + c.uuid + "  " + c.target + "  " + typ + "  " + opts + "  0 0";
}

std::string drop_windows_names(const std::string& line) {
    auto f = split_ws(line);
    if (f.size() < 4 || f[2] != "ntfs3") return line;
    std::vector<std::string> kept;
    std::string cur;
    for (char c : f[3] + ",") {
        if (c == ',') { if (cur != "windows_names" && !cur.empty()) kept.push_back(cur); cur.clear(); }
        else cur += c;
    }
    std::string opts = kept.empty() ? "defaults" : join(kept, ",");
    size_t p = line.find(f[3]);
    return line.substr(0, p) + opts + line.substr(p + f[3].size());
}

static std::string lsblk_json() {
    return run_argv_stdout({"lsblk", "-J", "-o", "NAME,PATH,FSTYPE,UUID,LABEL,SIZE,MOUNTPOINT,TYPE,PKNAME,RM,HOTPLUG"}).out;
}

static bool is_mount(const std::string& path) {
    for (const auto& l : split_lines(read_file("/proc/self/mountinfo").value_or(""))) {
        auto f = split_ws(l);
        if (f.size() > 4 && f[4] == path) return true;
    }
    return false;
}

static void remount(const std::string& target) {
    auto r = run_argv({"umount", target});
    if (r.code != 0) {
        std::fprintf(stderr, "    ! %s is in use (%s); the new options apply after a reboot, or close what uses it and run this again\n",
                     target.c_str(), trim(r.out).c_str());
        return;
    }
    r = run_argv({"mount", target});
    if (r.code != 0) std::fprintf(stderr, "    ! mount %s failed: %s\n", target.c_str(), trim(r.out).c_str());
}

static void cleanup_legacy(const std::string& user) {
    std::string marker = "/etc/nobara/automount/.tuxthrottle-managed";
    auto t = read_file(marker);
    if (!t) return;
    size_t n = 0;
    for (const auto& l : split_lines(*t)) {
        std::string u = trim(l);
        if (u.empty()) continue;
        ++n;
        run_argv({"systemctl", "disable", "--now", "nobara-automount@" + u + ".service"});
        run_argv({"systemd-umount", "/run/media/" + user + "/" + u});
        ::rmdir(("/run/media/" + user + "/" + u).c_str());
        ::unlink(("/etc/udev/rules.d/99-tuxthrottle-automount-" + u + ".rules").c_str());
        ::unlink(("/etc/nobara/automount/" + u + ".env").c_str());
        auto conf = read_file("/etc/nobara/automount/enabled.conf");
        if (conf) {
            std::string out;
            for (const auto& cl : split_lines(*conf)) if (cl.find(u) == std::string::npos) out += cl + "\n";
            write_file("/etc/nobara/automount/enabled.conf", out);
        }
    }
    ::unlink(marker.c_str());
    run_argv({"udevadm", "control", "--reload"});
    std::printf("  (migrated %zu drive(s) off the old /run/media setup)\n", n);
}

static int enable_all(const std::string& user) {
    struct passwd* pw = getpwnam(user.c_str());
    if (!pw) { std::fprintf(stderr, "no such user: %s\n", user.c_str()); return 1; }
    cleanup_legacy(user);
    std::string ftext = read_file(kFstab).value_or("");
    auto [body, old] = split_block(ftext);
    std::vector<std::string> kept, fixed;
    std::set<std::string> seen;
    for (const auto& raw : split_lines(old)) {
        std::string line = trim(raw);
        if (!starts_with(lower(line), "uuid=")) continue;
        std::string nl = drop_windows_names(line);
        if (nl != line) {
            fixed.push_back(split_ws(nl)[1]);
            std::printf("  ~ %s  removed windows_names (Wine/Proton prefixes need ':' in names)\n", split_ws(nl)[1].c_str());
        }
        kept.push_back(nl);
        seen.insert(split_ws(line)[0].substr(5));
    }
    std::vector<Candidate> cands;
    for (auto& c : candidates_from(lsblk_json(), ftext))
        if (!seen.count(c.uuid)) cands.push_back(c);
    if (cands.empty() && fixed.empty()) { std::puts("No unmounted internal data partitions found - nothing to do."); return 0; }
    while (!body.empty() && body.back() == '\n') body.pop_back();
    body += "\n";
    std::vector<std::string> lines{kBegin};
    lines.insert(lines.end(), kept.begin(), kept.end());
    for (const auto& c : cands) {
        run_shell("mkdir -p " + shell_quote(c.target));
        lines.push_back(fstab_line(c, static_cast<int>(pw->pw_uid), static_cast<int>(pw->pw_gid)));
        std::printf("  + %s  %s  -> %s  (%s)\n", c.path.c_str(), c.fstype.c_str(), c.target.c_str(), c.size.c_str());
    }
    lines.push_back(kEnd);
    write_file(kFstab, body + join(lines, "\n") + "\n");
    run_argv({"systemctl", "daemon-reload"});
    for (const auto& t : fixed) {
        if (is_mount(t)) remount(t);
        else run_argv({"mount", t});
    }
    for (const auto& c : cands) {
        auto r = run_argv({"mount", c.target});
        if (r.code != 0) std::fprintf(stderr, "    ! mount %s failed: %s\n", c.target.c_str(), trim(r.out).c_str());
    }
    std::printf("\nDone. %zu drive(s) added, %zu fixed; all mount at /mnt on every boot.\n", cands.size(), fixed.size());
    return 0;
}

static int disable_all(const std::string& user) {
    cleanup_legacy(user);
    auto [body, block] = split_block(read_file(kFstab).value_or(""));
    if (block.empty()) {
        std::puts("No managed fstab block - nothing to remove.");
        write_file(kFstab, body);
        return 0;
    }
    for (const auto& raw : split_lines(block)) {
        std::string line = trim(raw);
        if (!starts_with(line, "UUID=")) continue;
        std::string target = split_ws(line)[1];
        run_argv({"umount", target});
        ::rmdir(target.c_str());
        std::printf("  - %s unmounted, fstab entry removed\n", target.c_str());
    }
    write_file(kFstab, body);
    run_argv({"systemctl", "daemon-reload"});
    return 0;
}

static int list_drives() {
    auto cands = candidates_from(lsblk_json(), read_file(kFstab).value_or(""));
    if (cands.empty()) { std::puts("No unmounted internal data partitions."); return 0; }
    std::puts("Would mount permanently:");
    for (const auto& c : cands) std::printf("  %-16s %-7s %8s  -> %s   (%s)\n", c.path.c_str(), c.fstype.c_str(), c.size.c_str(), c.target.c_str(), c.uuid.c_str());
    return 0;
}

int main(const std::vector<std::string>& args) {
    std::string user, action;
    for (size_t i = 0; i < args.size(); ++i) {
        if (args[i] == "--user" && i + 1 < args.size()) user = args[++i];
        else action = args[i];
    }
    if (user.empty() || (action != "list" && action != "enable-all" && action != "disable-all")) {
        std::puts("usage: automount --user NAME {list|enable-all|disable-all}");
        return 2;
    }
    if (action != "list" && !is_root()) { std::fputs("enable-all / disable-all need root.\n", stderr); return 1; }
    if (action == "list") return list_drives();
    return action == "enable-all" ? enable_all(user) : disable_all(user);
}

}  // namespace tt::automount
