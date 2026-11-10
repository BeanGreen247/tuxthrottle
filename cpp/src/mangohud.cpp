#include "mangohud.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <regex>

#include "formats.hpp"
#include "launchers.hpp"
#include "steam.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::mangohud {

namespace fs = std::filesystem;

const std::vector<std::string> kPositions = {"top-left", "top-center", "top-right", "middle-left", "middle-right", "bottom-left", "bottom-center", "bottom-right"};
const std::vector<std::string> kDetails = {"global", "fps_only", "full"};

const char* kGateScript = R"GATE(#!/usr/bin/env bash
# tuxthrottle-mangohud-gate - per-game MangoHud switch (TuxThrottle → MangoHud tab).
# Sits ahead of /usr/bin/mangohud on PATH: a Steam game only gets the overlay
# when ~/.config/tuxthrottle/mangohud-games/<appid>.on exists. Outside Steam
# (no AppID in the environment) it behaves exactly like the real mangohud.
real=/usr/bin/mangohud
appid="${SteamAppId:-${SteamGameId:-${STEAM_COMPAT_APP_ID:-}}}"
if [ -z "$appid" ] || [ "$#" -eq 0 ] || [ "${1#--}" != "$1" ]; then
    exec "$real" "$@"
fi
# per-game "force the dedicated GPU" switch (<appid>.dgpu): PRIME render offload
if [ -e "$HOME/.config/tuxthrottle/mangohud-games/$appid.dgpu" ]; then
    if [ -e /proc/driver/nvidia/version ]; then
        export __NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only \
               __GLX_VENDOR_LIBRARY_NAME=nvidia
    else
        export DRI_PRIME=1
    fi
fi
if [ -e "$HOME/.config/tuxthrottle/mangohud-games/$appid.on" ]; then
    own="$HOME/.config/MangoHud/tuxthrottle-$appid.conf"
    [ -f "$own" ] && export MANGOHUD_CONFIGFILE="$own"
    exec "$real" "$@"
fi
exec "$@"
)GATE";

static const char* kGatePath = "/usr/local/bin/mangohud";
static const char* kGateMark = "tuxthrottle-mangohud-gate";
static const char* kOwned[] = {"position", "offset_x", "offset_y", "font_size", "fps_limit", "fps_only", "full"};

static std::string home() { return env_or("HOME", "/root"); }

// ---------------------------------------------------------------- string surgery
static std::string squeeze(const std::string& s) {
    std::string out;
    bool prev_space = false;
    for (char c : s) {
        if (c == ' ') { if (!prev_space) out += c; prev_space = true; }
        else { out += c; prev_space = false; }
    }
    return trim(out);   // note: Python strip() also trims other whitespace; opts rarely contain it
}

// Removes every whitespace-delimited token for which pred(token) holds (the token only; spaces stay).
static std::string remove_tokens(const std::string& s, const std::function<bool(const std::string&)>& pred) {
    std::string out;
    size_t i = 0;
    while (i < s.size()) {
        if (std::isspace(static_cast<unsigned char>(s[i]))) { out += s[i++]; continue; }
        size_t j = i;
        while (j < s.size() && !std::isspace(static_cast<unsigned char>(s[j]))) ++j;
        std::string tok = s.substr(i, j - i);
        if (!pred(tok)) out += tok;
        i = j;
    }
    return out;
}

// finds `MANGOHUD_CONFIGFILE=("..."|\S+)` at a token start; returns the value (quotes stripped) and the span.
struct ConfVar { size_t begin = std::string::npos, end = 0; std::string value; };
static ConfVar find_confvar(const std::string& s) {
    static const std::string key = "MANGOHUD_CONFIGFILE=";
    size_t pos = 0;
    while ((pos = s.find(key, pos)) != std::string::npos) {
        if (pos == 0 || std::isspace(static_cast<unsigned char>(s[pos - 1]))) {
            size_t v = pos + key.size();
            if (v < s.size() && s[v] == '"') {
                size_t q = s.find('"', v + 1);
                if (q != std::string::npos) return {pos, q + 1, s.substr(v + 1, q - v - 1)};
            }
            size_t e = v;
            while (e < s.size() && !std::isspace(static_cast<unsigned char>(s[e]))) ++e;
            if (e > v) return {pos, e, s.substr(v, e - v)};
        }
        pos += key.size();
    }
    return {};
}

static std::string strip_confvar(const std::string& s) {
    std::string cur = s;
    while (true) {
        ConfVar c = find_confvar(cur);
        if (c.begin == std::string::npos) return cur;
        cur = cur.substr(0, c.begin) + cur.substr(c.end);
    }
}

OptState state(const std::string& opts) {
    OptState st;
    for (size_t i = 0; i < opts.size();) {
        if (std::isspace(static_cast<unsigned char>(opts[i]))) { ++i; continue; }
        size_t j = i;
        while (j < opts.size() && !std::isspace(static_cast<unsigned char>(opts[j]))) ++j;
        if (opts.substr(i, j - i) == "mangohud") st.enabled = true;
        i = j;
    }
    ConfVar c = find_confvar(opts);
    if (c.begin != std::string::npos) {
        st.conf = c.value;
        std::string t = opts.substr(c.begin + 20, c.end - c.begin - 20);
        if (!t.empty() && t.front() == '"') st.conf = t.substr(1, t.size() - 2);
    }
    return st;
}

std::string put_env(const std::string& s, const std::string& var) {
    std::string padded = " " + s + " ";
    if (padded.find(" -- env ") != std::string::npos) {
        size_t p = s.find("-- env ");
        if (p != std::string::npos) return s.substr(0, p) + "-- env " + var + " " + s.substr(p + 7);
    }
    if (padded.find(" -- ") != std::string::npos) {
        size_t p = s.find("-- ");
        if (p != std::string::npos) return s.substr(0, p) + "-- env " + var + " " + s.substr(p + 3);
    }
    return var + " " + s;
}

std::string transform(const std::string& opts, bool enabled, const std::string& conffile) {
    std::string s = strip_confvar(remove_tokens(opts, [](const std::string& t) { return t == "mangohud"; }));
    if (!enabled) return squeeze(s);
    s = squeeze(remove_tokens(s, [](const std::string& t) { return t == "MANGOHUD=0" || t == "MANGOHUD=1"; }));
    if (s.find("%command%") == std::string::npos) s = squeeze(s + " %command%");
    size_t p = s.find("%command%");
    s = s.substr(0, p) + "mangohud %command%" + s.substr(p + 9);
    if (!conffile.empty()) {
        std::string var = conffile.find(' ') != std::string::npos ? "MANGOHUD_CONFIGFILE=\"" + conffile + "\"" : "MANGOHUD_CONFIGFILE=" + conffile;
        s = put_env(s, var);
    }
    return squeeze(s);
}

static bool is_dgpu_token(const std::string& t) {
    return starts_with(t, "__NV_PRIME_RENDER_OFFLOAD=") || starts_with(t, "__VK_LAYER_NV_optimus=") || t == "__GLX_VENDOR_LIBRARY_NAME=nvidia" || starts_with(t, "DRI_PRIME=");
}

std::string dgpu_vars(bool nvidia) {
    return nvidia ? "__NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only __GLX_VENDOR_LIBRARY_NAME=nvidia" : "DRI_PRIME=1";
}

bool has_dgpu(const std::string& opts) {
    for (const auto& t : split_ws(opts)) if (t == "__NV_PRIME_RENDER_OFFLOAD=1" || t == "DRI_PRIME=1") return true;
    return false;
}

std::string transform_dgpu(const std::string& opts, bool on, bool nvidia) {
    std::string s = squeeze(remove_tokens(opts, is_dgpu_token));
    s = squeeze(replace_all(s, "-- env %command%", "-- %command%"));
    if (!on) return s == "%command%" ? "" : s;
    if (s.find("%command%") == std::string::npos) s = squeeze(s + " %command%");
    return squeeze(put_env(s, dgpu_vars(nvidia)));
}

static std::string key_of(const std::string& line) {
    size_t e = line.find('=');
    return trim(e == std::string::npos ? line : line.substr(0, e));
}

std::string render_conf(const std::string& appid, const Json& settings, const std::string& base_text) {
    bool own_pos = !settings["position"].str().empty();
    std::set<std::string> drop;
    for (const char* k : kOwned) drop.insert(k);
    if (!own_pos) { drop.erase("position"); drop.erase("offset_x"); drop.erase("offset_y"); }
    std::vector<std::string> out{"# MangoHud config for Steam AppID " + appid + " (managed by TuxThrottle)"};
    for (const auto& ln : split_lines(base_text)) {
        std::string t = trim(ln);
        if (t.empty() || t[0] == '#' || drop.count(key_of(t))) continue;
        out.push_back(t);
    }
    if (own_pos) out.push_back("position=" + settings["position"].str());
    for (const char* k : {"font_size", "fps_limit"}) {
        std::string v = trim(settings[k].is_number() ? std::to_string(static_cast<long long>(settings[k].as_number())) : settings[k].str());
        if (!v.empty() && std::all_of(v.begin(), v.end(), [](char c) { return c >= '0' && c <= '9'; }) && std::atoll(v.c_str()) > 0) out.push_back(std::string(k) + "=" + v);
    }
    std::string d = settings["detail"].str();
    if (d == "fps_only" || d == "full") out.push_back(d);
    return join(out, "\n") + "\n";
}

// ---------------------------------------------------------------- gate / flags / profiles
bool gate_installed() { return read_file(kGatePath).value_or("").find(kGateMark) != std::string::npos; }
bool gate_current() { return read_file(kGatePath).value_or("") == kGateScript; }
std::string flags_dir() { return home() + "/.config/tuxthrottle/mangohud-games"; }
bool flag_on(const std::string& appid) { return path_exists(flags_dir() + "/" + appid + ".on"); }

static void mark(const std::string& appid, const std::string& kind, bool on) {
    std::string f = flags_dir() + "/" + appid + "." + kind;
    if (on) { mkdir_p(flags_dir()); write_file(f, ""); }
    else ::unlink(f.c_str());
}
void set_flag(const std::string& appid, bool on) { mark(appid, "on", on); }

static std::set<std::string> marked(const std::string& kind) {
    std::set<std::string> out;
    for (const auto& f : glob_paths(flags_dir() + "/*." + kind)) { std::string b = base_name(f); out.insert(b.substr(0, b.size() - kind.size() - 1)); }
    return out;
}

static std::string conf_dir() { return home() + "/.config/MangoHud"; }
std::string conf_path(const std::string& appid) { return conf_dir() + "/tuxthrottle-" + appid + ".conf"; }
static std::string off_path(const std::string& appid) { return conf_path(appid) + ".off"; }
static bool is_file(const std::string& p) { std::error_code ec; return fs::is_regular_file(p, ec); }

std::string profile_state(const std::string& appid) {
    if (is_file(conf_path(appid))) return "on";
    return is_file(off_path(appid)) ? "off" : "none";
}

Json read_settings(const std::string& appid) {
    Json res = Json::obj();
    res.set("position", "");
    res.set("detail", "global");
    res.set("font_size", "");
    res.set("fps_limit", "");
    auto t = read_file(conf_path(appid));
    if (!t) return res;
    for (const auto& ln : split_lines(*t)) {
        std::string tt_ = trim(ln), k = key_of(tt_);
        if ((k == "position" || k == "font_size" || k == "fps_limit") && tt_.find('=') != std::string::npos) res.set(k, trim(tt_.substr(tt_.find('=') + 1)));
        else if (tt_ == "fps_only" || tt_ == "full") res.set("detail", tt_);
    }
    return res;
}

static void set_keys(const std::string& path, const std::map<std::string, std::string>& keys) {
    std::vector<std::string> lines;
    for (const auto& ln : split_lines(read_file(path).value_or(""))) if (!keys.count(key_of(trim(ln)))) lines.push_back(ln);
    for (const auto& [k, v0] : keys) {
        std::string v = trim(v0);
        if (!v.empty() && std::all_of(v.begin(), v.end(), [](char c) { return c >= '0' && c <= '9'; }) && std::atoll(v.c_str()) > 0) lines.push_back(k + "=" + v);
    }
    std::string text = join(lines, "\n");
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.back()))) text.pop_back();
    write_file(path, text + "\n");
}

std::string write_conf(const std::string& appid, const Json& settings) {
    std::string p = conf_path(appid);
    mkdir_p(dir_name(p));
    write_file(p, render_conf(appid, settings, read_file(conf_dir() + "/MangoHud.conf").value_or("")));
    return p;
}

std::string ensure_profile(const std::string& appid, const Json& settings) {
    std::string p = conf_path(appid), off = off_path(appid);
    if (!is_file(p)) {
        if (is_file(off)) ::rename(off.c_str(), p.c_str());
        else return write_conf(appid, settings);
    }
    std::map<std::string, std::string> keys;
    for (const char* k : {"font_size", "fps_limit"})
        if (settings.has(k)) keys[k] = settings[k].is_number() ? std::to_string(static_cast<long long>(settings[k].as_number())) : settings[k].str();
    set_keys(p, keys);
    return p;
}

void disable_profile(const std::string& appid) {
    if (is_file(conf_path(appid))) ::rename(conf_path(appid).c_str(), off_path(appid).c_str());
}

void delete_profile(const std::string& appid) {
    ::unlink(conf_path(appid).c_str());
    ::unlink(off_path(appid).c_str());
}

// ---------------------------------------------------------------- Steam side
static bool is_tool_name(const std::string& name) {
    std::string l = lower(name);
    if (starts_with(l, "proton") && (l.size() == 6 || l[6] == ' ')) return true;
    return starts_with(l, "steam linux runtime") || starts_with(l, "steamworks common") || (ends_with(l, "steamvr") && l == "steamvr");
}

static void scan_acf(const std::function<void(const std::string& lib, const std::string& appid, const std::string& text)>& fn) {
    for (const auto& root : launchers::steam_roots_list()) {
        std::error_code ec;
        for (const auto& lib : steam::all_libraries(root)) {
            auto g = glob_paths(lib.string() + "/steamapps/appmanifest_*.acf");
            for (const auto& acf : g) {
                auto t = read_file(acf);
                if (!t) continue;
                static const std::regex a(R"re("appid"\s+"(\d+)")re");
                std::smatch m;
                if (std::regex_search(*t, m, a)) fn(lib.string(), m[1], *t);
            }
        }
    }
}

std::map<std::string, std::string> installed_games() {
    std::map<std::string, std::string> names;
    scan_acf([&](const std::string&, const std::string& appid, const std::string& t) {
        static const std::regex n(R"re("name"\s+"([^"]*)")re");
        std::smatch m;
        if (!names.count(appid)) names[appid] = std::regex_search(t, m, n) ? m[1].str() : appid;
    });
    return names;
}

std::map<std::string, std::string> steam_install_dirs() {
    std::map<std::string, std::string> dirs;
    scan_acf([&](const std::string& lib, const std::string& appid, const std::string& t) {
        static const std::regex d(R"re("installdir"\s+"([^"]*)")re");
        std::smatch m;
        if (std::regex_search(t, m, d) && !dirs.count(appid)) dirs[appid] = lib + "/steamapps/common/" + m[1].str();
    });
    return dirs;
}

static std::string force_file() { return home() + "/.config/tuxthrottle/mangohud-force.json"; }

static std::set<std::string> forced() {
    auto t = read_file(force_file());
    std::set<std::string> out;
    if (!t) return out;
    try {
        Json d = Json::parse(*t);
        for (const auto& x : d.array()) out.insert(x.str());
    } catch (const std::exception&) {}
    return out;
}

static void save_forced(const std::set<std::string>& ids) {
    Json a = Json::arr();
    for (const auto& i : ids) a.push(i);
    mkdir_p(dir_name(force_file()));
    write_file(force_file(), a.dump());
}

static std::string gid(const std::string& raw) {
    return !raw.empty() && std::all_of(raw.begin(), raw.end(), [](char c) { return c >= '0' && c <= '9'; }) ? "steam:" + raw : raw;
}

static std::map<std::string, std::string> anticheat_map(const std::set<std::string>& ids) {
    std::map<std::string, std::string> paths;
    for (const auto& [a, p] : steam_install_dirs()) if (ids.count("steam:" + a)) paths["steam:" + a] = p;
    for (const auto& g : launchers::other_games()) if (ids.count(g.id)) paths[g.id] = g.path;
    std::map<std::string, std::string> out;
    for (const auto& [k, v] : launchers::anticheat_for(paths)) if (!v.empty()) out[k] = v;
    return out;
}

static std::map<std::string, std::string> launch_options_map() {
    std::map<std::string, std::string> opts;
    for (const auto& id : launchers::steam_game_ids()) opts.emplace(id, launchers::steam_get_launch_options(id));
    return opts;
}

static bool has_nvidia() { return path_exists("/proc/driver/nvidia/version"); }

Json list_games() {
    auto opts = launch_options_map();
    bool gate = gate_installed();
    Json rows = Json::arr();
    for (const auto& [aid, name] : installed_games()) {
        if (is_tool_name(name)) continue;
        std::string o = opts.count(aid) ? opts[aid] : "";
        OptState st = state(o);
        bool own = gate ? is_file(conf_path(aid)) : !st.conf.empty();
        bool flag = path_exists(flags_dir() + "/" + aid + ".dgpu"), off_pending = path_exists(flags_dir() + "/" + aid + ".nodgpu"), in_opts = has_dgpu(o);
        Json r = Json::obj();
        r.set("id", "steam:" + aid);
        r.set("source", "Steam");
        r.set("appid", aid);
        r.set("name", name);
        r.set("hook", st.enabled);
        r.set("wanted", gate ? flag_on(aid) : st.enabled);
        r.set("enabled", st.enabled && (flag_on(aid) || !gate));
        r.set("own_settings", own);
        r.set("profile", profile_state(aid));
        r.set("options", o);
        r.set("dgpu", flag || (in_opts && !off_pending));
        r.set("dgpu_active", in_opts || (flag && gate && st.enabled));
        r.set("settings", read_settings(aid));
        rows.push(r);
    }
    std::map<std::string, std::string> paths;
    for (const auto& g : launchers::other_games()) {
        Json r = Json::obj();
        r.set("id", g.id);
        r.set("source", g.source);
        r.set("appid", g.appid);
        r.set("name", g.name);
        r.set("hook", true);
        r.set("wanted", g.wanted);
        r.set("enabled", g.enabled);
        r.set("own_settings", false);
        r.set("profile", "none");
        r.set("options", "");
        r.set("settings", Json::obj());
        rows.push(r);
        if (!g.path.empty()) paths[g.id] = g.path;
    }
    for (const auto& [a, p] : steam_install_dirs()) paths["steam:" + a] = p;
    auto ac = launchers::anticheat_for(paths);
    auto force = forced();
    Json out = Json::arr();
    std::vector<Json> v(rows.array().begin(), rows.array().end());
    for (auto& r : v) {
        std::string id = r["id"].str();
        r.set("anticheat", ac.count(id) ? ac[id] : "");
        r.set("forced", force.count(id) > 0);
    }
    std::sort(v.begin(), v.end(), [](const Json& a, const Json& b) { return lower(a["name"].str()) < lower(b["name"].str()); });
    for (auto& r : v) out.push(r);
    return out;
}

// ---------------------------------------------------------------- Steam launch-option writers
using Fn = std::function<std::string(const std::string&)>;

static int rewrite_one(const std::string& appid, const Fn& fn, bool dry) {
    return launchers::steam_rewrite([&](const std::vector<std::string>&) { return std::vector<std::string>{appid}; },
                                    [&](const std::string&, const std::string& cur) { return fn(cur); }, dry);
}

static int set_game_gated(const std::string& appid, bool enabled, const Json* settings, bool dry) {
    if (!enabled) {
        if (!dry) set_flag(appid, false);
        std::printf("MangoHud off for %s - applies at its next launch\n", appid.c_str());
        return 0;
    }
    auto opts = launch_options_map();
    bool hooked = state(opts.count(appid) ? opts[appid] : "").enabled;
    if (!hooked) {
        int rc = rewrite_one(appid, [](const std::string& c) { return transform(c, true); }, dry);
        if (rc) {
            std::printf("AppID %s has no `mangohud` wrapper in its Launch Options yet - adding it is the one step that needs Steam closed.\n", appid.c_str());
            return rc;
        }
    }
    if (!dry) {
        if (settings) ensure_profile(appid, *settings);
        else disable_profile(appid);
        set_flag(appid, true);
    }
    std::printf("MangoHud on for %s - applies at its next launch\n", appid.c_str());
    return 0;
}

int set_game(const std::string& appid, bool enabled, const Json* settings, bool dry) {
    if (gate_installed()) return set_game_gated(appid, enabled, settings, dry);
    std::string conffile;
    if (enabled && settings) {
        conffile = conf_path(appid);
        if (!dry && !launchers::steam_is_running()) ensure_profile(appid, *settings);
    }
    return rewrite_one(appid, [&](const std::string& c) { return transform(c, enabled, conffile); }, dry);
}

struct Edits {
    std::map<std::string, std::vector<Fn>> edits;
    void add(const std::string& aid, Fn fn) { edits[aid].push_back(std::move(fn)); }
    bool empty() const { return edits.empty(); }
};

static std::vector<std::string> plan_dgpu(const Json& plan, Edits& edits, const std::map<std::string, std::string>& opts, bool gate, bool dry) {
    auto digits = [](const Json& arr) {
        std::vector<std::string> out;
        for (const auto& a : arr.array()) {
            std::string s = a.is_number() ? std::to_string(static_cast<long long>(a.as_number())) : a.str();
            if (!s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; })) out.push_back(s);
        }
        return out;
    };
    auto on = digits(plan["dgpu_on"]), off = digits(plan["dgpu_off"]);
    if (!dry) {
        for (const auto& a : on) { mark(a, "dgpu", true); mark(a, "nodgpu", false); }
        for (const auto& a : off) { mark(a, "dgpu", false); mark(a, "nodgpu", true); }
    }
    std::set<std::string> want_on = marked("dgpu"), want_off = marked("nodgpu");
    want_on.insert(on.begin(), on.end());
    want_off.insert(off.begin(), off.end());
    for (const auto& a : off) want_on.erase(a);
    for (const auto& a : on) want_off.erase(a);
    bool nvidia = has_nvidia();
    std::vector<std::string> need;
    for (const auto& aid : want_on) {
        std::string cur = opts.count(aid) ? opts.at(aid) : "";
        bool live = gate && state(cur).enabled;
        if (!live && !has_dgpu(cur)) { edits.add(aid, [nvidia](const std::string& c) { return transform_dgpu(c, true, nvidia); }); need.push_back(aid); }
    }
    for (const auto& aid : want_off) {
        if (has_dgpu(opts.count(aid) ? opts.at(aid) : "")) { edits.add(aid, [nvidia](const std::string& c) { return transform_dgpu(c, false, nvidia); }); need.push_back(aid); }
        else if (!dry) mark(aid, "nodgpu", false);
    }
    if (!on.empty() || !off.empty()) std::printf("Dedicated GPU: %zu game(s) on, %zu off\n", on.size(), off.size());
    return need;
}

static int run_edits(const Edits& edits, bool dry) {
    return launchers::steam_rewrite(
        [&](const std::vector<std::string>&) { std::vector<std::string> ids; for (const auto& kv : edits.edits) ids.push_back(kv.first); return ids; },
        [&](const std::string& aid, const std::string& cur) { std::string c = cur; for (const auto& fn : edits.edits.at(aid)) c = fn(c); return c; }, dry);
}

int apply_plan(const Json& plan, bool dry) {
    std::map<std::string, Json> all_on;
    for (const auto& kv : plan["on"].object()) all_on[gid(kv.first)] = kv.second;
    std::vector<std::string> all_off;
    for (const auto& a : plan["off"].array()) all_off.push_back(gid(a.is_number() ? std::to_string(static_cast<long long>(a.as_number())) : a.str()));
    std::set<std::string> plan_force;
    for (const auto& a : plan["force"].array()) plan_force.insert(gid(a.str()));
    std::set<std::string> force = forced();
    force.insert(plan_force.begin(), plan_force.end());
    std::set<std::string> on_ids;
    for (const auto& kv : all_on) on_ids.insert(kv.first);
    for (const auto& [g, ac] : anticheat_map(on_ids)) {
        if (!all_on.count(g) || force.count(g)) continue;
        std::printf("skipped %s: %s detected - an injected overlay can get the account flagged. Tick \xE2\x80\x9C" "force\xE2\x80\x9D for that game to override.\n", g.c_str(), ac.c_str());
        all_on.erase(g);
    }
    if (!dry) {
        std::set<std::string> nf = forced();
        for (const auto& g : all_off) nf.erase(g);
        std::set<std::string> keep;
        for (const auto& g : plan_force) if (all_on.count(g)) keep.insert(g);
        nf.insert(keep.begin(), keep.end());
        save_forced(nf);
        for (const auto& g : all_off) if (!starts_with(g, "steam:")) launchers::set_game_mangohud(g, false);
        for (const auto& kv : all_on) if (!starts_with(kv.first, "steam:") && !launchers::set_game_mangohud(kv.first, true)) std::printf("could not switch %s on (launcher config not writable)\n", kv.first.c_str());
    }
    int others = 0;
    for (const auto& kv : all_on) others += !starts_with(kv.first, "steam:");
    for (const auto& g : all_off) others += !starts_with(g, "steam:");
    if (others) std::printf("%d Heroic/Lutris game(s) updated in the launcher's own per-game MangoHud setting\n", others);
    std::map<std::string, Json> on;
    for (const auto& kv : all_on) if (starts_with(kv.first, "steam:")) on[kv.first.substr(6)] = kv.second;
    std::vector<std::string> off;
    for (const auto& g : all_off) if (starts_with(g, "steam:")) off.push_back(g.substr(6));
    bool gate = gate_installed();
    auto opts = launch_options_map();
    Edits edits;
    auto dgpu_need = plan_dgpu(plan, edits, opts, gate, dry);
    if (!dry)
        for (const auto& [aid, v] : on)
            if (v.is_object()) { if (v["own"].as_bool()) ensure_profile(aid, v); else disable_profile(aid); }
    std::vector<std::string> hook_need;
    if (!gate) {
        for (const auto& aid : off) edits.add(aid, [](const std::string& c) { return transform(c, false); });
        for (const auto& [aid, v] : on) {
            bool own = !v.is_object() ? is_file(conf_path(aid)) : v["own"].as_bool();
            std::string conf = own ? conf_path(aid) : "";
            edits.add(aid, [conf](const std::string& c) { return transform(c, true, conf); });
        }
    } else {
        if (!dry) {
            for (const auto& aid : off) set_flag(aid, false);
            for (const auto& kv : on) set_flag(kv.first, true);
        }
        if (!on.empty() || !off.empty()) std::printf("MangoHud: %zu game(s) on, %zu off - applies at each game's next launch\n", on.size(), off.size());
        std::set<std::string> cand = marked("on");
        for (const auto& kv : on) cand.insert(kv.first);
        for (const auto& a : cand)
            if (std::find(off.begin(), off.end(), a) == off.end() && !state(opts.count(a) ? opts[a] : "").enabled) hook_need.push_back(a);
        for (const auto& aid : hook_need) edits.add(aid, [](const std::string& c) { return transform(c, true, ""); });
    }
    if (edits.empty()) return 0;
    int rc = run_edits(edits, dry);
    if (rc == 0 && !dry) for (const auto& aid : marked("nodgpu")) mark(aid, "nodgpu", false);
    if (rc == 2) {
        auto names = installed_games();
        auto nm = [&](const std::string& a) { return names.count(a) ? names[a] : a; };
        if (gate && !hook_need.empty()) {
            std::vector<std::string> v;
            for (const auto& a : hook_need) v.push_back(nm(a));
            std::printf("%zu game(s) don't have the `mangohud` hook in their Launch Options yet: %s\n", hook_need.size(), join(v, ", ").c_str());
        }
        if (!dgpu_need.empty()) {
            std::vector<std::string> v;
            for (const auto& a : dgpu_need) v.push_back(nm(a));
            std::printf("%zu game(s) need their Launch Options edited for the dedicated-GPU switch: %s\n", dgpu_need.size(), join(v, ", ").c_str());
        }
        std::puts("They are recorded - close Steam once, press Apply again, and all of them are done in a single write.");
        return 3;
    }
    return rc;
}

int hook_all(bool dry) {
    if (!gate_installed()) { std::puts("install the live per-game switch first - without it the hook would turn the overlay ON in every game"); return 1; }
    auto installed = installed_games();
    return launchers::steam_rewrite(
        [&](const std::vector<std::string>& existing) {
            std::set<std::string> s(existing.begin(), existing.end());
            for (const auto& kv : installed) s.insert(kv.first);
            std::vector<std::string> v(s.begin(), s.end());
            std::sort(v.begin(), v.end());
            return v;
        },
        [](const std::string&, const std::string& cur) { return state(cur).enabled ? cur : transform(cur, true); }, dry);
}

int disable_all(bool dry) {
    if (gate_installed()) {
        auto flags = glob_paths(flags_dir() + "/*.on");
        if (!dry) for (const auto& f : flags) ::unlink(f.c_str());
        std::printf("MangoHud switched off for %zu game(s) - no game shows the overlay until you enable it\n", flags.size());
        return 0;
    }
    return launchers::steam_rewrite([](const std::vector<std::string>& existing) { return existing; },
                                    [](const std::string&, const std::string& cur) { return transform(cur, false); }, dry);
}

std::vector<std::string> global_confs() {
    std::vector<std::string> hits;
    for (const std::string& d : {home() + "/.config/environment.d", std::string("/etc/environment.d")}) {
        auto g = glob_paths(d + "/*.conf");
        std::sort(g.begin(), g.end());
        for (const auto& f : g) {
            auto t = read_file(f);
            if (!t) continue;
            bool on = false;
            for (const auto& ln : split_lines(*t)) if (trim(ln) == "MANGOHUD=1") on = true;
            if (on || t->find("libMangoHud") != std::string::npos) hits.push_back(f);
        }
    }
    return hits;
}

int global_off() {
    int rc = 0;
    for (const auto& f : global_confs()) {
        auto t = read_file(f);
        if (!t) { std::printf("could not edit %s\n", f.c_str()); rc = 1; continue; }
        std::vector<std::string> keep;
        for (const auto& ln : split_lines(*t)) if (trim(ln) != "MANGOHUD=1" && ln.find("libMangoHud") == std::string::npos) keep.push_back(ln);
        bool any = false;
        for (const auto& ln : keep) { std::string s = trim(ln); if (!s.empty() && s[0] != '#') any = true; }
        bool ok = any ? write_file(f, join(keep, "\n") + "\n") : ::unlink(f.c_str()) == 0;
        if (!ok) { std::printf("could not edit %s: permission denied\n", f.c_str()); rc = 1; continue; }
        std::printf("removed session-wide MangoHud from %s\n", f.c_str());
    }
    if (rc == 0) std::puts("session-wide MangoHud is off - takes full effect at next login");
    return rc;
}

// ---------------------------------------------------------------- status line
namespace status_line {
static std::string cfg_home(const std::string& user) {
    if (!user.empty()) return home_of(user) + "/.config";
    return env_or("XDG_CONFIG_HOME", home() + "/.config");
}
static std::string conf_p(const std::string& u) { return cfg_home(u) + "/MangoHud/MangoHud.conf"; }
static std::string state_p(const std::string& u) { return cfg_home(u) + "/tuxthrottle/mangohud_status_line.json"; }
static const char* kKey = "custom_text_center";

static void atomic_write_lines(const std::string& path, const std::vector<std::string>& lines) {
    mkdir_p(dir_name(path));
    std::string tmp = path + ".tuxthrottle-tmp";
    write_file(tmp, lines.empty() ? "" : join(lines, "\n") + "\n");
    ::rename(tmp.c_str(), path.c_str());
}

bool is_enabled(const std::string& user) { return read_json_file(state_p(user))["enabled"].as_bool(); }

void clear_status_line(const std::string& user) {
    std::string p = conf_p(user);
    if (!is_file(p)) return;
    std::vector<std::string> lines;
    for (const auto& ln : split_lines(read_file(p).value_or(""))) if (key_of(ln) != kKey) lines.push_back(ln);
    atomic_write_lines(p, lines);
}

void set_enabled(bool value, const std::string& user) {
    Json o = Json::obj();
    o.set("enabled", value);
    mkdir_p(dir_name(state_p(user)));
    write_file(state_p(user), o.dump());
    if (!user.empty()) { chown_to_user(state_p(user), user); }
    if (!value) clear_status_line(user);
}

void set_status_line(const std::string& text, const std::string& user) {
    if (!is_enabled(user)) return;
    std::string p = conf_p(user);
    std::vector<std::string> out;
    bool replaced = false;
    std::string nl = std::string(kKey) + "=" + text;
    for (const auto& ln : split_lines(read_file(p).value_or(""))) {
        if (key_of(ln) == kKey) { if (!replaced) { out.push_back(nl); replaced = true; } continue; }
        out.push_back(ln);
    }
    if (!replaced) out.push_back(nl);
    atomic_write_lines(p, out);
}
}  // namespace status_line

int status_main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: mangohud-status [--user U] {enable|disable|status|update <text>}"); return 2; }
    std::string user;
    std::vector<std::string> rest;
    for (size_t i = 0; i < a.size(); ++i) { if (a[i] == "--user" && i + 1 < a.size()) user = a[++i]; else rest.push_back(a[i]); }
    if (rest.empty()) return 2;
    if (rest[0] == "enable") status_line::set_enabled(true, user);
    else if (rest[0] == "disable") status_line::set_enabled(false, user);
    else if (rest[0] == "status") std::puts(status_line::is_enabled(user) ? "on" : "off");
    else if (rest[0] == "update" && rest.size() > 1) status_line::set_status_line(rest[1], user);
    else return 2;
    return 0;
}

// ---------------------------------------------------------------- CLI
int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: mangohud-games {list [--json]|enable <appid> [...]|disable <appid>|disable-all|apply --plan B64|profile <appid> on|off|delete|status|global-status|global-off|hook-all|gate-script|gate-status}"); return 2; }
    const std::string& cmd = a[0];
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    auto opt = [&](const char* f, const std::string& def) { for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1]; return def; };
    bool dry = has("--dry-run");
    auto numeric = [](const std::string& s) { return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; }); };
    if (cmd == "gate-script") { std::fputs(kGateScript, stdout); return 0; }
    if (cmd == "gate-status") { std::puts(gate_installed() ? "installed" : "not installed"); return gate_installed() ? 0 : 1; }
    if (cmd == "profile") {
        if (a.size() < 3 || !numeric(a[1])) { std::fputs("appid must be numeric\n", stderr); return 1; }
        if (a[2] == "on") ensure_profile(a[1]);
        else if (a[2] == "off") disable_profile(a[1]);
        else if (a[2] == "delete") delete_profile(a[1]);
        std::printf("profile for %s: %s\n", a[1].c_str(), profile_state(a[1]).c_str());
        return 0;
    }
    if (cmd == "apply") {
        try { return apply_plan(Json::parse(base64_decode(opt("--plan", ""))), dry); }
        catch (const std::exception& e) { std::fprintf(stderr, "bad --plan: %s\n", e.what()); return 1; }
    }
    if (cmd == "hook-all") return hook_all(dry);
    if (cmd == "list") {
        Json rows = list_games();
        if (has("--json")) { std::puts(rows.dump().c_str()); return 0; }
        for (const auto& r : rows.array())
            std::printf("  [%s] %-8s %s%s\n", r["enabled"].as_bool() ? "on " : "off", r["appid"].str().c_str(), r["name"].str().c_str(), r["own_settings"].as_bool() ? "  (own settings)" : "");
        return 0;
    }
    if ((cmd == "enable" || cmd == "disable") && (a.size() < 2 || !numeric(a[1]))) { std::fputs("appid must be numeric\n", stderr); return 1; }
    if (cmd == "enable") {
        Json s = Json::obj();
        const Json* sp = nullptr;
        if (has("--own-settings")) {
            s.set("position", opt("--position", ""));
            s.set("detail", opt("--detail", "global"));
            s.set("font_size", opt("--font-size", ""));
            s.set("fps_limit", opt("--fps-limit", ""));
            sp = &s;
        }
        return set_game(a[1], true, sp, dry);
    }
    if (cmd == "disable") return set_game(a[1], false, nullptr, dry);
    if (cmd == "disable-all") return disable_all(dry);
    if (cmd == "global-status") {
        auto hits = global_confs();
        std::puts(hits.empty() ? "off" : join(hits, "\n").c_str());
        return hits.empty() ? 0 : 1;
    }
    if (cmd == "global-off") return global_off();
    return 2;
}

}  // namespace tt::mangohud
