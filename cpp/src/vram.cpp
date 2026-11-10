#include "vram.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <regex>
#include <set>

#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::vram {

const std::vector<std::string> kTiers = {"regular", "medium", "extreme"};

namespace {
struct Row { std::string file; std::vector<std::string> groups; std::string key, value; };

const std::vector<Row>& tier_keys(const std::string& tier) {
    static const std::vector<Row> medium = {
        {"kwinrc", {"Compositing"}, "HiddenPreviews", "5"},
        {"kwinrc", {"Compositing"}, "GLTextureFilter", "1"},
        {"kwinrc", {"Plugins"}, "blurEnabled", "false"},
        {"kwinrc", {"Plugins"}, "contrastEnabled", "false"},
        {"kdeglobals", {"KDE"}, "AnimationDurationFactor", "0.25"},
    };
    static const std::vector<Row> extreme = {
        {"kwinrc", {"Compositing"}, "HiddenPreviews", "4"},
        {"kwinrc", {"Compositing"}, "GLTextureFilter", "0"},
        {"kwinrc", {"Compositing"}, "LatencyPolicy", "Low"},
        {"kwinrc", {"Plugins"}, "blurEnabled", "false"},
        {"kwinrc", {"Plugins"}, "contrastEnabled", "false"},
        {"kwinrc", {"Plugins"}, "overviewEnabled", "false"},
        {"kwinrc", {"Plugins"}, "windowviewEnabled", "false"},
        {"kwinrc", {"Plugins"}, "desktopgridEnabled", "false"},
        {"kwinrc", {"Plugins"}, "slideEnabled", "false"},
        {"kwinrc", {"Plugins"}, "slidingpopupsEnabled", "false"},
        {"kwinrc", {"Plugins"}, "kwin4_effect_translucencyEnabled", "false"},
        {"kdeglobals", {"KDE"}, "AnimationDurationFactor", "0"},
        {"kwinrc", {"Wayland"}, "InputMethod", ""},
    };
    static const std::vector<Row> none;
    return tier == "medium" ? medium : tier == "extreme" ? extreme : none;
}

std::vector<std::string> effects_off(const std::string& tier) {
    if (tier == "medium") return {"blur", "contrast"};
    if (tier == "extreme")
        return {"blur", "contrast", "overview", "windowview", "desktopgrid", "slide", "slidingpopups", "kwin4_effect_translucency"};
    return {};
}

const char* kApplets = "plasma-org.kde.plasma.desktop-appletsrc";
const char* kEnvFile = "09-tuxthrottle-gpu.sh";

std::string real_home_dir() { return home_of(real_user("")); }
std::string cfg_path() { return real_home_dir() + "/.config/tuxthrottle/vram.json"; }
std::string baseline_path() { return real_home_dir() + "/.config/tuxthrottle/vram-baseline.json"; }

Json load(const std::string& p) { return read_json_file(p); }
void save(const std::string& p, const Json& d) {
    mkdir_p(dir_name(p));
    write_file(p, d.dump(2, true));
    chown_to_user(p, "");
}

std::string shq(const std::string& s) { return shell_quote(s); }

std::pair<int, std::string> run_session_script(const std::string& script, int timeout_ms = 120000) {
    auto r = run_argv_stdout(sensors::session_cmd({"bash", "-lc", script}), timeout_ms);
    return {r.code, r.out};
}

std::string kwrite_cmd(const std::string& file, const std::vector<std::string>& groups, const std::string& key,
                       const std::optional<std::string>& value) {
    std::string c = "kwriteconfig6 --file " + shq(file);
    for (const auto& g : groups) c += " --group " + shq(g);
    c += " --key " + shq(key);
    c += value ? " " + shq(*value) : std::string(" --delete");
    return c;
}

struct KItem { std::string file; std::vector<std::string> groups; std::string key; };

std::vector<std::string> kread_batch(const std::vector<KItem>& items) {
    if (items.empty()) return {};
    std::vector<std::string> cmds;
    for (const auto& it : items) {
        std::string c = "kreadconfig6 --file " + shq(it.file);
        for (const auto& g : it.groups) c += " --group " + shq(g);
        c += " --key " + shq(it.key) + " ; printf \"\\0\"";
        cmds.push_back(c);
    }
    auto [rc, out] = run_session_script(join(cmds, " ; "), 60000);
    (void)rc;
    std::vector<std::string> parts;
    std::string cur;
    for (char ch : out) {
        if (ch == '\0') { parts.push_back(cur); cur.clear(); }
        else cur += ch;
    }
    parts.push_back(cur);
    std::vector<std::string> res;
    for (size_t i = 0; i < items.size(); ++i) {
        std::string v = i < parts.size() ? parts[i] : "";
        while (!v.empty() && v.front() == '\n') v.erase(0, 1);
        while (!v.empty() && v.back() == '\n') v.pop_back();
        res.push_back(v);
    }
    return res;
}

std::vector<std::string> effects_cmds(const std::string& action, const std::vector<std::string>& names) {
    std::vector<std::string> out;
    for (const auto& n : names) {
        std::string q = shq(n);
        out.push_back("{ qdbus-qt6 org.kde.KWin /Effects org.kde.kwin.Effects." + action + "Effect " + q +
                      " || qdbus org.kde.KWin /Effects org.kde.kwin.Effects." + action + "Effect " + q +
                      " || dbus-send --session --type=method_call --dest=org.kde.KWin /Effects org.kde.kwin.Effects." + action +
                      "Effect string:" + q + "; } >/dev/null 2>&1 || true");
    }
    return out;
}

std::string reload_kwin_cmd() {
    return "{ qdbus-qt6 org.kde.KWin /KWin reconfigure || qdbus org.kde.KWin /KWin reconfigure || dbus-send --session "
           "--type=method_call --dest=org.kde.KWin /KWin org.kde.KWin.reconfigure; } >/dev/null 2>&1 || true";
}

void restart_plasmashell() {
    run_session_script("systemctl --user restart --no-block plasma-plasmashell.service || (setsid kstart plasmashell >/dev/null 2>&1 &)", 20000);
}

std::vector<std::string> desktop_containments() {
    std::string p = real_home_dir() + "/.config/" + kApplets;
    auto text = read_file(p);
    std::vector<std::string> out;
    if (!text) return out;
    static const std::regex hdr(R"(^\[Containments\]\[(\d+)\](\[.+\])?$)");
    std::string cur;
    bool have = false, is_desktop = false;
    for (const auto& line : split_lines(*text)) {
        std::smatch m;
        if (std::regex_match(line, m, hdr) && !m[2].matched) {
            if (have && is_desktop) out.push_back(cur);
            cur = m[1];
            have = true;
            is_desktop = false;
        } else if (have && starts_with(trim(line), "plugin=")) {
            std::string v = trim(line).substr(7);
            if (v == "org.kde.desktopcontainment" || v == "org.kde.plasma.folder") is_desktop = true;
        }
    }
    if (have && is_desktop) out.push_back(cur);
    return out;
}

std::vector<KItem> all_tier_keys() {
    std::set<std::string> seen;
    std::vector<KItem> out;
    for (const std::string t : {"medium", "extreme"})
        for (const auto& r : tier_keys(t)) {
            std::string sig = r.file + "|" + join(r.groups, "/") + "|" + r.key;
            if (seen.insert(sig).second) out.push_back({r.file, r.groups, r.key});
        }
    return out;
}

void capture_baseline() {
    if (path_exists(baseline_path())) return;
    auto keys = all_tier_keys();
    auto vals = kread_batch(keys);
    auto conts = desktop_containments();
    std::vector<KItem> wk;
    for (const auto& c : conts) wk.push_back({kApplets, {"Containments", c}, "wallpaperplugin"});
    auto wp = kread_batch(wk);
    Json data = Json::obj();
    Json karr = Json::arr();
    for (size_t i = 0; i < keys.size(); ++i) {
        Json row = Json::arr();
        row.push(keys[i].file);
        Json g = Json::arr();
        for (const auto& s : keys[i].groups) g.push(s);
        row.push(g);
        row.push(keys[i].key);
        row.push(vals[i]);
        karr.push(row);
    }
    data.set("keys", karr);
    Json wall = Json::obj();
    for (size_t i = 0; i < conts.size(); ++i) wall.set(conts[i], wp[i].empty() ? "org.kde.image" : wp[i]);
    data.set("wallpaper", wall);
    save(baseline_path(), data);
}

std::string current_detect() {
    std::string f = real_home_dir() + "/.config/plasma-workspace/env/" + kEnvFile;
    auto txt = read_file(f);
    if (!txt) return "auto";
    static const std::regex re(R"re(KWIN_DRM_DEVICES="?([^"\n]+))re");
    std::smatch m;
    if (!std::regex_search(*txt, m, re)) return "auto";
    std::string first = m[1].str().substr(0, m[1].str().find(':'));
    for (const auto& g : sensors::drm_gpus()) {
        std::string link = "/dev/dri/by-path/pci-" + g.pci + "-card";
        std::string node = path_exists(link) ? real_path(link) : "";
        if (first == node) return g.kind == "integrated" ? "igpu" : "dgpu";
    }
    return "auto";
}

bool is_muxless() {
    for (const auto& g : sensors::drm_gpus())
        if (g.kind == "integrated" && g.boot_vga) return true;
    return false;
}

std::string card_node(const std::string& pci) {
    std::string link = "/dev/dri/by-path/pci-" + pci + "-card";
    return path_exists(link) ? real_path(link) : "";
}

std::vector<std::string> debugfs_dirs(const std::string& driver) {
    std::set<std::string> seen;
    std::vector<std::string> out;
    for (const auto& name : glob_paths("/sys/kernel/debug/dri/*/name")) {
        std::string d = dir_name(name);
        std::string txt = read_file(name).value_or("");
        if (txt.find(driver) == std::string::npos) continue;
        static const std::regex re(R"(dev=(\S+))");
        std::smatch m;
        std::string dev = std::regex_search(txt, m, re) ? m[1].str() : d;
        if (!seen.insert(dev).second) continue;
        out.push_back(d);
    }
    return out;
}

Json gpu_rows_json(const std::vector<sensors::VramRow>& rows) {
    Json a = Json::arr();
    for (const auto& r : rows) {
        Json o = Json::obj();
        auto ol = [](const std::optional<long>& v) { return v ? Json(static_cast<double>(*v)) : Json(); };
        o.set("name", r.name);
        o.set("pci", r.pci);
        o.set("driver", r.driver);
        o.set("kind", r.kind);
        o.set("used_mb", ol(r.used_mb));
        o.set("total_mb", ol(r.total_mb));
        o.set("pct", r.pct ? Json(*r.pct) : Json());
        o.set("gtt_used_mb", ol(r.gtt_used_mb));
        o.set("asleep", r.asleep);
        a.push(o);
    }
    return a;
}

Json consumers_json() {
    Json a = Json::arr();
    for (const auto& c : sensors::vram_consumers()) {
        Json o = Json::obj();
        o.set("pid", static_cast<double>(c.pid));
        o.set("comm", c.comm);
        o.set("driver", c.driver);
        o.set("vram_mb", c.vram_mb);
        a.push(o);
    }
    return a;
}
}  // namespace

std::string current_tier() {
    std::string t = load(cfg_path())["tier"].str();
    return std::find(kTiers.begin(), kTiers.end(), t) != kTiers.end() ? t : "regular";
}

std::string current_compositor_gpu() {
    std::string c = load(cfg_path())["compositor_gpu"].str();
    return c.empty() ? current_detect() : c;
}

std::vector<std::string> compositor_gpu_modes() {
    return is_muxless() ? std::vector<std::string>{"auto", "igpu"} : std::vector<std::string>{"auto", "igpu", "dgpu"};
}

std::pair<bool, std::string> apply_tier(const std::string& tier) {
    if (std::find(kTiers.begin(), kTiers.end(), tier) == kTiers.end()) return {false, "unknown tier '" + tier + "' (regular|medium|extreme)"};
    std::vector<std::string> cmds;
    bool wallpaper_changed = false;
    if (tier != "regular") {
        capture_baseline();
        for (const auto& r : tier_keys(tier)) cmds.push_back(kwrite_cmd(r.file, r.groups, r.key, r.value));
        for (const auto& c : effects_cmds("unload", effects_off(tier))) cmds.push_back(c);
        if (tier == "extreme")
            for (const auto& cid : desktop_containments()) {
                cmds.push_back(kwrite_cmd(kApplets, {"Containments", cid}, "wallpaperplugin", "org.kde.color"));
                cmds.push_back(kwrite_cmd(kApplets, {"Containments", cid, "Wallpaper", "org.kde.color", "General"}, "Color", "0,0,0"));
                wallpaper_changed = true;
            }
    } else {
        Json base = load(baseline_path());
        std::set<std::string> known;
        for (const auto& row : base["keys"].array()) {
            const auto& a = row.array();
            if (a.size() < 4) continue;
            std::vector<std::string> groups;
            for (const auto& g : a[1].array()) groups.push_back(g.str());
            known.insert(a[0].str() + "|" + join(groups, "/") + "|" + a[2].str());
            std::string old = a[3].str();
            cmds.push_back(kwrite_cmd(a[0].str(), groups, a[2].str(), old.empty() ? std::nullopt : std::optional<std::string>(old)));
        }
        for (const auto& k : all_tier_keys())
            if (!known.count(k.file + "|" + join(k.groups, "/") + "|" + k.key)) cmds.push_back(kwrite_cmd(k.file, k.groups, k.key, std::nullopt));
        std::set<std::string> names;
        for (const char* t : {"medium", "extreme"}) for (const auto& n : effects_off(t)) names.insert(n);
        for (const auto& c : effects_cmds("load", std::vector<std::string>(names.begin(), names.end()))) cmds.push_back(c);
        std::vector<std::pair<std::string, std::string>> wp_base;
        for (const auto& kv : base["wallpaper"].object()) wp_base.emplace_back(kv.first, kv.second.str());
        std::vector<KItem> wk;
        for (const auto& p : wp_base) wk.push_back({kApplets, {"Containments", p.first}, "wallpaperplugin"});
        auto now = kread_batch(wk);
        for (size_t i = 0; i < wp_base.size(); ++i)
            if (now[i] == "org.kde.color") {
                cmds.push_back(kwrite_cmd(kApplets, {"Containments", wp_base[i].first}, "wallpaperplugin",
                                          wp_base[i].second.empty() ? "org.kde.image" : wp_base[i].second));
                wallpaper_changed = true;
            }
    }
    cmds.push_back(reload_kwin_cmd());
    run_session_script(join(cmds, " ; "), 120000);
    if (wallpaper_changed) restart_plasmashell();
    Json cfg = load(cfg_path());
    cfg.set("tier", tier);
    save(cfg_path(), cfg);
    return {true, "VRAM budget -> " + tier + (wallpaper_changed ? " - the panel restarts in the background for the wallpaper change" : "")};
}

Json free_vram(bool restart_compositor) {
    auto before = sensors::vram_info();
    Json evicted = Json::arr(), errors = Json::arr();
    for (const auto& d : debugfs_dirs("amdgpu"))
        for (const char* node : {"amdgpu_evict_vram", "amdgpu_evict_gtt"}) {
            std::string path = d + "/" + node;
            if (!path_exists(path)) continue;
            bool done = read_file(path).has_value();
            if (!done) {
                int fd = open(path.c_str(), O_WRONLY);
                if (fd >= 0) {
                    done = write(fd, "1", 1) == 1;
                    close(fd);
                }
                if (!done) errors.push(std::string(node) + ": cannot write (need root)");
            }
            if (done) evicted.push(base_name(d) + "/" + node);
        }
    for (auto [drv, node] : {std::pair<const char*, const char*>{"i915", "i915_gem_drop_caches"}, {"xe", "xe_drop_caches"}})
        for (const auto& d : debugfs_dirs(drv)) {
            std::string path = d + "/" + node;
            if (!path_exists(path)) continue;
            if (write_text(path, "0xf")) evicted.push(base_name(d) + "/" + node);
            else errors.push(std::string(node) + ": cannot write");
        }
    if (evicted.array().empty() && !restart_compositor)
        errors.push("no driver-level eviction available on this GPU - try Restart compositor to release KWin's own allocations");
    if (restart_compositor) {
        auto [rc, out] = run_session_script("systemctl --user restart plasma-kwin_wayland.service", 45000);
        (void)out;
        if (rc != 0) errors.push("kwin restart failed (rc " + std::to_string(rc) + ")");
        else evicted.push("compositor restarted");
    }
    Json res = Json::obj();
    res.set("before", gpu_rows_json(before));
    res.set("after", gpu_rows_json(sensors::vram_info()));
    res.set("evicted", evicted);
    res.set("errors", errors);
    res.set("consumers", consumers_json());
    return res;
}

std::pair<bool, std::string> set_compositor_gpu(const std::string& mode) {
    if (mode != "auto" && mode != "igpu" && mode != "dgpu") return {false, "mode must be auto|igpu|dgpu"};
    std::string env_dir = real_home_dir() + "/.config/plasma-workspace/env";
    std::string env_file = env_dir + "/" + kEnvFile;
    Json cfg = load(cfg_path());
    if (mode == "auto") {
        ::unlink(env_file.c_str());
        cfg.set("compositor_gpu", "auto");
        save(cfg_path(), cfg);
        return {true, "desktop GPU -> auto (KWin picks; log out to apply)"};
    }
    auto gpus = sensors::drm_gpus();
    if (mode == "dgpu" && is_muxless())
        return {false, "this laptop's panel is wired to the integrated GPU (muxless) - KWin cannot render the whole desktop on the dGPU. "
                       "Use per-app PRIME offload for games / DaVinci Resolve instead."};
    std::vector<std::string> cards;
    auto add = [&](const char* kind) {
        for (const auto& g : gpus)
            if (g.kind == kind) { std::string n = card_node(g.pci); if (!n.empty()) cards.push_back(n); }
    };
    if (mode == "igpu") add("integrated");
    else { add("discrete"); add("integrated"); }
    if (cards.empty()) return {false, "no matching /dev/dri/card node found"};
    std::string lines;
    for (const auto& c : cards) lines += "[ -e " + c + " ] && _d=\"${_d:+$_d:}" + c + "\"\n";
    mkdir_p(env_dir);
    write_file(env_file,
               "# written by tuxthrottle_vram - pin the KDE compositor's GPU.\n"
               "# If the desktop fails to start: switch to a TTY (Ctrl+Alt+F3),\n"
               "# log in, and run:  rm " + env_file + "\n_d=\"\"\n" + lines +
               "[ -n \"$_d\" ] && export KWIN_DRM_DEVICES=\"$_d\"\nunset _d\n");
    chown_to_user(env_file, "");
    chown_to_user(env_dir, "");
    cfg.set("compositor_gpu", mode);
    save(cfg_path(), cfg);
    return {true, "desktop GPU -> " + mode + "  (" + join(cards, ":") + "); log out and back in to apply"};
}

Json status() {
    Json s = Json::obj();
    s.set("tier", current_tier());
    s.set("compositor_gpu", current_compositor_gpu());
    s.set("gpus", gpu_rows_json(sensors::vram_info()));
    s.set("consumers", consumers_json());
    auto pm = sensors::nvidia_runtime_pm();
    if (pm) {
        Json p = Json::obj();
        p.set("control", pm->control);
        p.set("status", pm->status);
        s.set("nvidia_runtime_pm", p);
    } else s.set("nvidia_runtime_pm", Json());
    return s;
}

std::string format_status(const Json& s) {
    char b[256];
    std::string out = "VRAM budget tier : " + s["tier"].str() + "\ndesktop GPU      : " + s["compositor_gpu"].str() + "\n";
    const Json& pm = s["nvidia_runtime_pm"];
    if (pm.is_object()) out += "dGPU runtime PM  : " + pm["control"].str() + " (" + pm["status"].str() + ")\n";
    out += "\n";
    for (const auto& g : s["gpus"].array()) {
        if (g["asleep"].as_bool()) {
            std::snprintf(b, sizeof b, "  %-28s %-10s (dGPU asleep)\n", g["name"].str().c_str(), g["kind"].str().c_str());
            out += b;
            continue;
        }
        if (!g["used_mb"].is_number() || !g["total_mb"].as_number()) {
            std::snprintf(b, sizeof b, "  %-28s %-10s n/a\n", g["name"].str().c_str(), g["kind"].str().c_str());
            out += b;
            continue;
        }
        double used = g["used_mb"].as_number(), total = g["total_mb"].as_number();
        int n = static_cast<int>(std::lround(20 * used / total));
        std::string bar(static_cast<size_t>(n), '#');
        bar += std::string(static_cast<size_t>(20 - n), '-');
        std::string gtt = g["gtt_used_mb"].as_number() > 0 ? "  +GTT " + std::to_string(static_cast<long>(g["gtt_used_mb"].as_number())) + " MiB" : "";
        std::snprintf(b, sizeof b, "  %-28s %-10s [%s] %ld/%ld MiB (%d%%)%s\n", g["name"].str().c_str(), g["kind"].str().c_str(), bar.c_str(),
                      static_cast<long>(used), static_cast<long>(total), static_cast<int>(g["pct"].as_number()), gtt.c_str());
        out += b;
    }
    out += "\n  top VRAM consumers:\n";
    size_t shown = 0;
    for (const auto& c : s["consumers"].array()) {
        if (shown++ >= 10) break;
        std::snprintf(b, sizeof b, "    %8.1f MiB  %-20s pid %ld [%s]\n", c["vram_mb"].as_number(), c["comm"].str().c_str(),
                      static_cast<long>(c["pid"].as_number()), c["driver"].str().c_str());
        out += b;
    }
    if (!out.empty() && out.back() == '\n') out.pop_back();
    return out;
}

int main(const std::vector<std::string>& args) {
    std::string cmd = args.empty() ? "status" : args[0];
    bool as_json = std::find(args.begin(), args.end(), "--json") != args.end();
    if (cmd == "status") {
        Json s = status();
        std::puts((as_json ? s.dump(2, true) : format_status(s)).c_str());
        return 0;
    }
    if (cmd == "profile") {
        if (args.size() < 2) return 2;
        auto r = apply_tier(args[1]);
        std::puts(r.second.c_str());
        return r.first ? 0 : 1;
    }
    if (cmd == "free") {
        Json res = free_vram(std::find(args.begin(), args.end(), "--restart-compositor") != args.end());
        if (as_json) { std::puts(res.dump(2, true).c_str()); return 0; }
        const auto& b = res["before"].array();
        const auto& a = res["after"].array();
        for (size_t i = 0; i < b.size() && i < a.size(); ++i) {
            if (!b[i]["used_mb"].is_number()) continue;
            std::printf("  %-28s %ld -> %ld MiB", b[i]["name"].str().c_str(), static_cast<long>(b[i]["used_mb"].as_number()),
                        static_cast<long>(a[i]["used_mb"].as_number()));
            if (b[i]["gtt_used_mb"].is_number())
                std::printf("  (GTT %ld -> %ld)", static_cast<long>(b[i]["gtt_used_mb"].as_number()), static_cast<long>(a[i]["gtt_used_mb"].as_number()));
            std::puts("");
        }
        std::vector<std::string> ev;
        for (const auto& e : res["evicted"].array()) ev.push_back(e.str());
        if (!ev.empty()) std::printf("  evicted: %s\n", join(ev, ", ").c_str());
        for (const auto& e : res["errors"].array()) std::printf("  ! %s\n", e.str().c_str());
        return 0;
    }
    if (cmd == "compositor-gpu") {
        if (args.size() < 2) return 2;
        auto r = set_compositor_gpu(args[1]);
        std::puts(r.second.c_str());
        return r.first ? 0 : 1;
    }
    return 2;
}

}  // namespace tt::vram
