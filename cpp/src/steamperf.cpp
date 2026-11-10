#include "steamperf.hpp"

#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <regex>
#include <set>

#include "steam.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::steamperf {

namespace fs = std::filesystem;

static const char* kMarker = "X-TuxThrottle-LowResource";
static const char* kIgpuMarker = "X-TuxThrottle-SteamIgpu";
static const char* kFlags = "-silent -cef-disable-gpu -cef-disable-gpu-compositing -cef-disable-breakpad -cef-disable-extra-info-spew";
static const char* kUnsafeFlags[] = {"-noverifyfiles", "-nobootstrapupdate", "-norepairfiles"};
static const char* kRemovedFlags[] = {"-cef-single-process", "-no-cef-sandbox", "-no-browser"};
static const char* kGpuKeys[] = {"PrefersNonDefaultGPU", "X-KDE-RunOnDiscreteGpu"};
static const int kMemHighMb = 1200;
static const char* kSysDesktop = "/usr/share/applications/steam.desktop";
static const char* kMountwaitBin = "/usr/local/bin/tuxthrottle-wait-mounts";
static const char* kRoots[] = {"~/.steam/steam", "~/.local/share/Steam", "~/.steam/root"};
static const char* kShaderBgKey = "EnableShaderBackgroundProcessing";

static const char* kManualHints[] = {
    "Steam → Settings → Library → enable “Low Bandwidth Mode” (no animated capsule / hero artwork downloads)",
    "Steam → Settings → Library → enable “Low Performance Mode” (no library animations / background video)",
    "Steam → Settings → Interface → disable “Enable smooth scrolling in web views”",
    "Steam → Settings → Downloads → disable “Enable Shader Pre-Caching” for the leanest possible (skips all pipeline pre-compilation)",
};

static std::string data_home() { return env_or("XDG_DATA_HOME", env_or("HOME", "/root") + "/.local/share"); }
static std::string config_home() { return env_or("XDG_CONFIG_HOME", env_or("HOME", "/root") + "/.config"); }
static std::string user_desktop() { return data_home() + "/applications/steam.desktop"; }
static std::string autostart() { return config_home() + "/autostart/steam.desktop"; }
static std::string autostart_bak(const std::string& suffix) { return data_home() + "/tuxthrottle/steam.desktop." + suffix; }

static std::string read(const std::string& p) { return read_file(p).value_or(""); }
static bool is_file(const std::string& p) { std::error_code ec; return fs::is_regular_file(p, ec); }
static bool has(const std::string& text, const std::string& sub) { return text.find(sub) != std::string::npos; }

static bool write(const std::string& p, const std::string& body) {
    mkdir_p(dir_name(p));
    return write_file(p, body);
}

static std::string join_lines(const std::vector<std::string>& lines, bool trailing_nl) { return join(lines, "\n") + (trailing_nl ? "\n" : ""); }
static bool ends_nl(const std::string& s) { return !s.empty() && s.back() == '\n'; }

std::string stamp(const std::string& text, const std::string& marker, const std::string& val) {
    std::vector<std::string> lines;
    for (const auto& ln : split_lines(text)) if (!starts_with(ln, marker + "=")) lines.push_back(ln);
    std::string txt = join_lines(lines, ends_nl(text));
    size_t p = txt.find("[Desktop Entry]\n");
    if (p == std::string::npos) return txt;
    return txt.substr(0, p) + "[Desktop Entry]\n" + marker + "=" + val + "\n" + txt.substr(p + 16);
}

static std::string steam_bin() { std::string s = which("steam"); return s.empty() ? "/usr/bin/steam" : s; }

static std::string scope_prefix(int mem) { return "systemd-run --user --scope --quiet --collect -p MemoryHigh=" + std::to_string(mem) + "M -- "; }

std::string patch_exec_lines(const std::string& text, const std::string& flags, int mem, bool have_run) {
    static const std::regex bin_re(R"(^Exec=(.*?/)?steam(\s+%[uU]\s*)$)");
    static const std::regex pre_re(R"((\S*/)?steam\b)");
    std::string scope = scope_prefix(mem);
    std::vector<std::string> out;
    for (const auto& ln : split_lines(text)) {
        std::string raw = trim(ln);
        if (starts_with(raw, "Exec=") && has(raw, "steam") && (has(raw, "-cef-disable-gpu") || has(raw, "MemoryHigh="))) {
            std::smatch m;
            std::string pre = std::regex_search(raw, m, pre_re) && m[1].matched ? m[1].str() : "";
            raw = "Exec=" + pre + "steam %U";
        }
        std::smatch m;
        if (std::regex_match(raw, m, bin_re)) {
            std::string pre = m[1].matched ? m[1].str() : "";
            std::string post = m[2].str();
            while (!post.empty() && std::isspace(static_cast<unsigned char>(post.back()))) post.pop_back();
            std::string core = pre + "steam " + flags + post;
            out.push_back("Exec=" + (have_run ? scope + core : core));
        } else out.push_back(ln);
    }
    return join_lines(out, ends_nl(text));
}

std::string unpatch_exec_lines(const std::string& text) {
    static const std::regex pre_re(R"((\S*/)?steam\b)");
    std::vector<std::string> out;
    for (const auto& ln : split_lines(text)) {
        std::string s = trim(ln);
        if (starts_with(s, "Exec=") && has(s, "steam") && (has(s, "-cef-disable-gpu") || has(s, "MemoryHigh="))) {
            std::smatch m;
            std::string pre = std::regex_search(s, m, pre_re) && m[1].matched ? m[1].str() : "";
            out.push_back("Exec=" + pre + "steam %U");
        } else out.push_back(ln);
    }
    return join_lines(out, ends_nl(text));
}

static std::string base_desktop_body() {
    if (is_file(kSysDesktop)) return read(kSysDesktop);
    return "[Desktop Entry]\nType=Application\nName=Steam\nExec=" + steam_bin() + " %U\nIcon=steam\nTerminal=false\nCategories=Network;FileTransfer;Game;\n";
}

std::string force_igpu_keys(const std::string& text) {
    std::vector<std::string> out;
    std::map<std::string, bool> seen = {{kGpuKeys[0], false}, {kGpuKeys[1], false}};
    bool in_entry = false;
    for (const auto& ln : split_lines(text)) {
        std::string s = trim(ln);
        if (starts_with(s, "[") && ends_with(s, "]")) {
            if (in_entry) for (auto& [k, ok] : seen) if (!ok) { out.push_back(k + "=false"); ok = true; }
            in_entry = s == "[Desktop Entry]";
            out.push_back(ln);
            continue;
        }
        std::string key = s.find('=') != std::string::npos ? trim(s.substr(0, s.find('='))) : "";
        if (in_entry && (key == kGpuKeys[0] || key == kGpuKeys[1])) { out.push_back(key + "=false"); seen[key] = true; }
        else out.push_back(ln);
    }
    if (in_entry) for (auto& [k, ok] : seen) if (!ok) out.push_back(k + "=false");
    return join_lines(out, ends_nl(text));
}

std::string flip_block_keys(const std::string& txt_in, const std::string& block, const std::vector<std::string>& keys, const std::string& value, bool insert_missing) {
    std::string txt = txt_in;
    for (const auto& k : keys) {
        std::regex pat("(\"" + k + "\"\\s+\")[01](\")");
        if (std::regex_search(txt, pat)) txt = regex_replace_fn(txt, pat, [&](const std::smatch& m) { return m[1].str() + value + m[2].str(); });
        else if (insert_missing) {
            std::regex blk("(\"" + block + "\"\\s*\\n\\s*\\{)");
            txt = regex_replace_fn(txt, blk, [&](const std::smatch& m) { return m[1].str() + "\n\t\t\t\"" + k + "\"\t\t\"" + value + "\""; }, true);
        }
    }
    return txt;
}

// ---------------------------------------------------------------- client settings
static std::vector<std::string> localconfig_files() {
    std::vector<std::string> out;
    std::set<std::string> seen;
    for (const char* root : kRoots)
        for (const auto& f : glob_paths(steam::expand_home(root) + "/userdata/*/config/localconfig.vdf")) {
            std::error_code ec;
            std::string key = fs::weakly_canonical(f, ec).string();
            if (seen.insert(key).second && is_file(f)) out.push_back(f);
        }
    return out;
}

static void write_atomic(const std::string& p, const std::string& orig, const std::string& txt) {
    std::string bak = p + ".tuxthrottle-bak";
    if (!path_exists(bak)) write_file(bak, orig);
    std::string tmp = p + ".tux-tmp-" + std::to_string(std::time(nullptr));
    write_file(tmp, txt);
    ::rename(tmp.c_str(), p.c_str());
}

static std::string steam_config_vdf() {
    for (const char* root : kRoots) {
        std::string p = steam::expand_home(root) + "/config/config.vdf";
        if (is_file(p)) return p;
    }
    return "";
}

static std::vector<std::string> apply_client_settings(bool low) {
    if (steam::steam_running()) return {"(Steam is running - client settings left unchanged; close Steam and press Enable again)"};
    std::string value = low ? "0" : "1";
    std::vector<std::string> done;
    for (const auto& p : localconfig_files()) {
        auto txt = read_file(p);
        if (!txt) continue;
        std::string orig = *txt, t = *txt;
        t = flip_block_keys(t, "friends", {"SignIntoFriends", "AnimatedAvatars", "AnimatedGameArt"}, value);
        t = flip_block_keys(t, "system", {"EnableGameOverlay"}, "1", false);
        if (t != orig) { write_atomic(p, orig, t); done.push_back(p); }
    }
    if (low) {
        std::string cf = steam_config_vdf();
        if (!cf.empty()) {
            auto txt = read_file(cf);
            if (txt) {
                std::regex pat(std::string("(\"") + kShaderBgKey + "\"\\s+\")[01](\")");
                std::string nw = regex_replace_fn(*txt, pat, [](const std::smatch& m) { return m[1].str() + "0" + m[2].str(); });
                if (nw != *txt) { write_atomic(cf, *txt, nw); done.push_back(cf + " (background shaders off)"); }
            }
        }
    }
    return done.empty() ? std::vector<std::string>{"(no config files needed changing)"} : done;
}

// ---------------------------------------------------------------- stray autostart files
std::vector<std::string> stray_autostart_files() {
    std::string au = autostart();
    std::string prefix = base_name(au) + ".";
    std::vector<std::string> out;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(dir_name(au), ec))
        if (e.is_regular_file(ec) && starts_with(e.path().filename().string(), prefix)) out.push_back(e.path().string());
    std::sort(out.begin(), out.end());
    return out;
}

std::vector<std::string> migrate_stray_bak() {
    std::vector<std::string> moved;
    std::string au = autostart();
    for (const auto& stray : stray_autostart_files()) {
        std::string dst = autostart_bak(base_name(stray).substr(base_name(au).size() + 1));
        if (!path_exists(dst)) write(dst, read(stray));
        if (::unlink(stray.c_str()) == 0) moved.push_back(stray);
    }
    return moved;
}

static std::string reapply_mountwait(const std::string& text) {
    if (has(text, kMountwaitBin)) return text;
    static const std::regex tail(R"(%[uU]['"]?$)");
    std::vector<std::string> out;
    for (const auto& ln : split_lines(text)) {
        std::string r = ln;
        while (!r.empty() && std::isspace(static_cast<unsigned char>(r.back()))) r.pop_back();
        if (starts_with(ln, "Exec=") && std::regex_search(r, tail)) out.push_back(std::string("Exec=") + kMountwaitBin + " " + ln.substr(5));
        else out.push_back(ln);
    }
    return join_lines(out, ends_nl(text));
}

// ---------------------------------------------------------------- iGPU pin
std::string status_igpu() {
    for (const auto& p : {user_desktop(), autostart()}) if (is_file(p) && has(read(p), kIgpuMarker)) return "on";
    return "off";
}

static std::string drop_lines(const std::string& text, const std::function<bool(const std::string&)>& drop) {
    std::vector<std::string> keep;
    for (const auto& ln : split_lines(text)) if (!drop(ln)) keep.push_back(ln);
    std::string txt = join(keep, "\n");
    if (!ends_with(txt, "\n")) txt += "\n";
    return txt;
}

std::pair<bool, std::string> enable_igpu() {
    std::string dst = user_desktop();
    if (is_file(dst) && has(read(dst), kMarker)) {
        write(dst, stamp(force_igpu_keys(read(dst)), kIgpuMarker, "true"));
        return {true, "Steam client pinned to the iGPU (folded into the existing low-resource shadow " + dst + ").\nFully quit Steam and relaunch it from the application menu."};
    }
    write(dst, stamp(force_igpu_keys(base_desktop_body()), kIgpuMarker, "true"));
    migrate_stray_bak();
    std::string au = autostart();
    std::vector<std::string> did{dst};
    if (is_file(au) && !has(read(au), kIgpuMarker) && !has(read(au), kMarker)) {
        std::string bak = autostart_bak("tuxthrottle-igpu-bak");
        if (!path_exists(bak)) write(bak, read(au));
        write(au, stamp(force_igpu_keys(read(au)), kIgpuMarker, "true"));
        did.push_back(au + " (patched)");
    }
    return {true, "Steam client pinned to the iGPU - " + join(did, ", ") + "\n(games still PRIME-offload to the dGPU via their own launch options).\n\n"
                  "Fully QUIT Steam (tray → Quit) and relaunch it from the application menu - a running Steam, or one started from a pinned taskbar entry, keeps the old GPU assignment."};
}

std::pair<bool, std::string> disable_igpu() {
    std::vector<std::string> done;
    std::string dst = user_desktop();
    if (is_file(dst) && has(read(dst), kIgpuMarker)) {
        if (has(read(dst), kMarker)) {
            write(dst, drop_lines(read(dst), [](const std::string& ln) {
                std::string key = ln.find('=') != std::string::npos ? trim(ln.substr(0, ln.find('='))) : "";
                return starts_with(ln, std::string(kIgpuMarker) + "=") || key == kGpuKeys[0] || key == kGpuKeys[1];
            }));
            done.push_back("un-pinned GPU keys in " + dst + " (low-resource shadow kept)");
        } else { ::unlink(dst.c_str()); done.push_back("removed " + dst); }
    }
    migrate_stray_bak();
    std::string au = autostart(), bak = autostart_bak("tuxthrottle-igpu-bak");
    if (is_file(bak)) { write(au, read(bak)); ::unlink(bak.c_str()); done.push_back("restored " + au); }
    else if (is_file(au) && has(read(au), kIgpuMarker)) {
        write(au, drop_lines(read(au), [](const std::string& ln) { return starts_with(ln, std::string(kIgpuMarker) + "="); }));
        done.push_back("cleaned " + au);
    }
    if (done.empty()) return {true, "Steam client iGPU pin was not set - nothing to do"};
    return {true, "Steam client iGPU pin removed - " + join(done, "; ") + "\nRestart Steam for it to take effect."};
}

// ---------------------------------------------------------------- low-resource mode
std::pair<bool, std::string> enable(bool autostart_flag) {
    bool igpu = status_igpu() == "on";
    bool have_run = !which("systemd-run").empty();
    auto patch = [&](const std::string& text, bool had_mountwait) {
        std::string t = patch_exec_lines(text, kFlags, kMemHighMb, have_run);
        if (igpu) t = stamp(force_igpu_keys(t), kIgpuMarker, "true");
        if (had_mountwait) t = reapply_mountwait(t);
        return stamp(t, kMarker, "true");
    };
    auto add_created = [](const std::string& s) {
        size_t p = s.find("[Desktop Entry]\n");
        return p == std::string::npos ? s : s.substr(0, p) + "[Desktop Entry]\nX-TuxThrottle-Created=true\n" + s.substr(p + 16);
    };
    std::string dst = user_desktop();
    write(dst, patch(base_desktop_body(), false));
    std::vector<std::string> did{dst};
    migrate_stray_bak();
    std::string au = autostart(), bak = autostart_bak("tuxthrottle-bak");
    if (is_file(au) && !has(read(au), kMarker)) {
        bool mw = has(read(au), kMountwaitBin);
        if (!path_exists(bak)) write(bak, read(au));
        write(au, patch(read(au), mw));
        did.push_back(au + " (patched)");
    } else if (is_file(au) && has(read(au), kMarker)) {
        bool mw = has(read(au), kMountwaitBin), created = has(read(au), "X-TuxThrottle-Created=true");
        std::string base = is_file(bak) ? read(bak) : base_desktop_body();
        std::string nw = patch(base, mw);
        if (created) nw = add_created(nw);
        write(au, nw);
        did.push_back(au + " (updated)");
    } else if (autostart_flag) {
        write(au, add_created(patch(base_desktop_body(), false)));
        did.push_back(au + " (created - Steam autostarts to the tray, -silent)");
    }
    auto settings = apply_client_settings(true);
    std::string cap = have_run ? "memory scope: MemoryHigh=" + std::to_string(kMemHighMb) + "M (soft - reclaims, never kills)" : "memory scope: (systemd-run absent - skipped)";
    std::vector<std::string> hints;
    for (const char* h : kManualHints) hints.push_back(std::string("    • ") + h);
    return {true, "Steam low-resource mode ON - " + join(did, ", ") + "\n  " + cap +
                      "\n  client settings (no auto chat / no friends animations / no bg shaders; overlay kept): " + join(settings, "; ") +
                      "\n\nFully QUIT Steam (tray → Quit) and relaunch it from the application menu - a Steam that's still running, or one started from a pinned taskbar entry, keeps the old settings."
                      "\n\nAlso tick these by hand (Steam keeps them in its own store, can't be scripted):\n" + join(hints, "\n")};
}

std::pair<bool, std::string> disable() {
    std::vector<std::string> done;
    std::string dst = user_desktop();
    if (is_file(dst) && has(read(dst), kMarker)) {
        if (has(read(dst), kIgpuMarker)) {
            write(dst, stamp(force_igpu_keys(base_desktop_body()), kIgpuMarker, "true"));
            done.push_back("stripped low-resource flags from " + dst + " (iGPU pin kept)");
        } else { ::unlink(dst.c_str()); done.push_back("removed " + dst); }
    }
    migrate_stray_bak();
    std::string au = autostart(), bak = autostart_bak("tuxthrottle-bak");
    if (is_file(au) && has(read(au), "X-TuxThrottle-Created=true")) {
        ::unlink(au.c_str());
        if (is_file(bak)) ::unlink(bak.c_str());
        done.push_back("removed " + au + " (we created it)");
    } else if (is_file(bak)) {
        write(au, read(bak));
        ::unlink(bak.c_str());
        done.push_back("restored " + au);
    } else if (is_file(au) && has(read(au), kMarker)) {
        write(au, unpatch_exec_lines(drop_lines(read(au), [](const std::string& ln) { return starts_with(ln, std::string(kMarker) + "="); })));
        done.push_back("cleaned " + au);
    }
    bool desktop_changed = !done.empty();
    auto settings = apply_client_settings(false);
    bool settings_changed = !(starts_with(settings[0], "(no ") || starts_with(settings[0], "(Steam is running"));
    if (!desktop_changed && !settings_changed) return {true, "Steam low-resource mode was not enabled - nothing to do"};
    done.push_back("client settings restored: " + join(settings, "; "));
    return {true, "Steam low-resource mode OFF - " + join(done, "; ") +
                      "\n(the background-shader setting is left as-is - use its own toggle in the shader-cache box)\nRestart Steam for it to take effect."};
}

std::string status() {
    std::string dst = user_desktop();
    if (!(is_file(dst) && has(read(dst), kMarker))) return "off";
    std::string val = "on", au = autostart();
    if (is_file(au) && has(read(au), "X-TuxThrottle-Created=true")) val += "+autostart";
    return val;
}

std::vector<std::pair<std::string, std::string>> diagnose(const std::string& user) {
    std::vector<std::pair<std::string, std::string>> res;
    bool forced = false;
    if (status_igpu() != "on") {
        std::string txt = is_file(kSysDesktop) ? read(kSysDesktop) : "";
        forced = has(txt, "PrefersNonDefaultGPU=true") || has(txt, "X-KDE-RunOnDiscreteGpu=true");
    }
    if (forced)
        res.push_back({"bad", "Steam's launcher forces the client onto the discrete GPU (PrefersNonDefaultGPU=true) - on a muxless laptop this crash-loops steamwebhelper. "
                              "Fix: Game Tools -> Steam client low-resource mode -> pin Steam client to the iGPU."});
    else res.push_back({"ok", "Steam client is not being forced onto the discrete GPU."});

    std::string shadow = is_file(user_desktop()) ? read(user_desktop()) : "";
    std::vector<std::string> stale;
    for (const char* f : kRemovedFlags) if (has(shadow, f)) stale.push_back(f);
    for (const char* f : kUnsafeFlags) if (has(shadow, f)) stale.push_back(f);
    if (!stale.empty())
        res.push_back({"bad", "Steam launcher still has removed/unsafe flags set: " + join(stale, ", ") +
                                  " - these have been seen to crash-loop steamwebhelper or SIGSEGV steamclient.so on launch. "
                                  "Fix: turn Steam low-resource mode off then back on to regenerate the launcher without them."});
    else res.push_back({"ok", "No known-broken flags in the Steam launcher."});

    std::string au = autostart();
    std::vector<std::string> strays;
    std::error_code ec;
    if (fs::is_directory(dir_name(au), ec))
        for (const auto& e : fs::directory_iterator(dir_name(au), ec)) {
            std::string n = e.path().filename().string();
            if (n == base_name(au) || !e.is_regular_file(ec)) continue;
            if (starts_with(n, "steam.desktop.") || (starts_with(n, "steam") && e.path().extension() == ".desktop")) strays.push_back(n);
        }
    std::sort(strays.begin(), strays.end());
    if (!strays.empty())
        res.push_back({"bad", "Extra Steam autostart file(s) in ~/.config/autostart: " + join(strays, ", ") +
                                  " - these launch a second Steam at login that fights the first for the single-instance lock: one client crashes a few seconds after login, "
                                  "or its steamwebhelper is left respawning ('not responding'). Fix: Game Tools -> Fix Steam starting twice at login."});
    else res.push_back({"ok", "No duplicate Steam autostart entry."});

    std::string home = user.empty() ? env_or("HOME", "/root") : home_of(user);
    std::string lf = home + "/.local/share/Steam/steamapps/libraryfolders.vdf", default_lib = home + "/.local/share/Steam";
    std::string text = is_file(lf) ? read(lf) : "";
    std::vector<std::string> missing;
    for (const auto& p0 : steam::parse_library_paths(text)) {
        std::string p = replace_all(p0, "\\\\", "/");
        if (p == default_lib) continue;
        if (!fs::is_directory(p + "/steamapps", ec)) missing.push_back(p);
    }
    if (!missing.empty())
        res.push_back({"bad", "Steam library folder(s) look unmounted or missing: " + join(missing, ", ") +
                                  " - its games will show as 'missing' until the drive is mounted. Check Diagnostics/Fixes -> unmounted drives, or fstab for that mount."});
    else if (!text.empty()) res.push_back({"ok", "All declared Steam library folders are present."});

    auto j = run_argv_stdout({"journalctl", "--since", "-1d", "--no-pager", "-o", "cat"}, 15000);
    static const std::regex dirty("ntfs3.*volume is dirty", std::regex::icase);
    if (std::regex_search(j.out, dirty))
        res.push_back({"bad", "An NTFS drive was refused as 'dirty' by the kernel in the last day - any Steam library on it silently vanishes. Fix: enable the NtfsForceMount tweak (Stability tab)."});
    return res;
}

int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: steamperf {on [--no-autostart]|off|status|igpu-on|igpu-off|igpu-status|diagnose [--json]|sweep-autostart}"); return 2; }
    if (is_root()) { std::puts("run as the real user, not root"); return 2; }
    std::string st = a[0];
    bool as_json = std::find(a.begin(), a.end(), "--json") != a.end(), no_auto = std::find(a.begin(), a.end(), "--no-autostart") != a.end();
    if (st == "diagnose") {
        auto r = diagnose();
        bool all_ok = true;
        if (as_json) {
            std::string out = "[";
            for (size_t i = 0; i < r.size(); ++i) {
                all_ok = all_ok && r[i].first == "ok";
                out += std::string(i ? ", " : "") + "[\"" + r[i].first + "\", " + [&] { std::string e = "\""; for (char c : r[i].second) e += c == '"' ? std::string("\\\"") : c == '\\' ? std::string("\\\\") : std::string(1, c); return e + "\""; }() + "]";
            }
            std::puts((out + "]").c_str());
        } else for (const auto& x : r) { all_ok = all_ok && x.first == "ok"; std::printf("[%s] %s\n", x.first.c_str(), x.second.c_str()); }
        return all_ok ? 0 : 1;
    }
    if (st == "sweep-autostart") {
        auto moved = migrate_stray_bak();
        for (const auto& m : moved) std::printf("moved %s out of the autostart folder\n", m.c_str());
        auto left = stray_autostart_files();
        if (moved.empty() && left.empty()) std::puts("no stray Steam autostart backups");
        else std::printf("%zu moved, %zu could not be moved\n", moved.size(), left.size());
        return left.empty() ? 0 : 1;
    }
    if (st == "status") { std::puts(status().c_str()); return 0; }
    if (st == "igpu-status") { std::puts(status_igpu().c_str()); return 0; }
    std::pair<bool, std::string> r;
    if (st == "igpu-on") r = enable_igpu();
    else if (st == "igpu-off") r = disable_igpu();
    else if (st == "on") r = enable(!no_auto);
    else if (st == "off") r = disable();
    else return 2;
    std::puts(r.second.c_str());
    return r.first ? 0 : 1;
}

}  // namespace tt::steamperf
