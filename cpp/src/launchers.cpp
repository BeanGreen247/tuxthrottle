#include "launchers.hpp"

#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <set>

#include "formats.hpp"
#include "steam.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::launchers {

namespace fs = std::filesystem;

static std::string home() { return env_or("HOME", "/root"); }

std::string expand_vars(const std::string& s) {
    std::string out;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] != '$' || i + 1 >= s.size()) { out += s[i]; continue; }
        size_t j = i + 1;
        std::string name;
        if (s[j] == '{') {
            size_t e = s.find('}', j);
            if (e == std::string::npos) { out += s[i]; continue; }
            name = s.substr(j + 1, e - j - 1);
            j = e + 1;
        } else {
            while (j < s.size() && (std::isalnum(static_cast<unsigned char>(s[j])) || s[j] == '_')) name += s[j++];
            if (name.empty()) { out += s[i]; continue; }
        }
        const char* v = std::getenv(name.c_str());
        if (v) out += v;
        else out += s.substr(i, j - i);   // unknown variables stay as written (os.path.expandvars)
        i = j - 1;
    }
    return out;
}

ParsedOpts parse_opts(const std::string& opts) {
    ParsedOpts p;
    auto toks = shlex_split(opts);
    if (!toks.empty() && toks[0] == "gamescope") {
        auto it = std::find(toks.begin(), toks.end(), "--");
        toks = it != toks.end() ? std::vector<std::string>(it + 1, toks.end()) : std::vector<std::string>{};
    }
    for (const auto& t : toks) {
        if (t == "%command%") break;
        size_t eq = t.find('=');
        bool env = false;
        if (eq != std::string::npos && !starts_with(t, "-")) {
            std::string k = t.substr(0, eq), kk;
            for (char c : k) if (c != '_') kk += c;
            env = !kk.empty() && std::all_of(kk.begin(), kk.end(), [](char c) { return std::isalnum(static_cast<unsigned char>(c)); });
        }
        if (env) {
            std::string v = t.substr(eq + 1);
            if (starts_with(v, "~")) v = "$HOME" + v.substr(1);
            p.env[t.substr(0, eq)] = expand_vars(v);
        } else if (t == "gamemoderun") p.flags["useGameMode"] = true;
        else if (t == "mangohud") p.flags["showMangohud"] = true;
        else if (t != "gamescope" && t != "env" && t != "--" && t != "-f") p.wrappers.push_back(t);
    }
    return p;
}

static std::string backup_stamp() { return std::to_string(std::time(nullptr)); }

static bool copy_backup(const std::string& path) {
    std::error_code ec;
    fs::copy_file(path, path + ".tuxthrottle-bak-" + backup_stamp(), fs::copy_options::overwrite_existing, ec);
    return !ec;
}

static std::string decode_arg(const std::vector<std::string>& rest, bool* ok, bool* missing) {
    *ok = true;
    *missing = false;
    auto it = std::find(rest.begin(), rest.end(), "--b64");
    std::vector<std::string> pos;
    for (const auto& r : rest) if (!starts_with(r, "-")) pos.push_back(r);
    if (it != rest.end()) {
        if (pos.empty()) { *ok = false; return ""; }
        try { return base64_decode(pos[0]); } catch (const std::exception&) { *ok = false; return ""; }
    }
    if (!pos.empty()) return pos[0];
    *missing = true;
    return "";
}

// ---------------------------------------------------------------- Steam launch options
namespace {
std::vector<std::string> steam_roots() {
    std::string h = home();
    std::vector<std::string> out;
    std::set<std::string> seen;
    std::error_code ec;
    for (const std::string& c : {h + "/.steam/steam", h + "/.steam/root", h + "/.local/share/Steam", h + "/.var/app/com.valvesoftware.Steam/data/Steam"}) {
        fs::path rp = fs::weakly_canonical(c, ec);
        if (ec || !fs::is_directory(rp, ec)) continue;
        if (seen.insert(rp.string()).second) out.push_back(rp.string());
    }
    return out;
}

std::vector<std::string> find_localconfigs() {
    std::vector<std::string> out;
    std::set<std::string> seen;
    for (const auto& root : steam_roots()) {
        auto g = glob_paths(root + "/userdata/*/config/localconfig.vdf");
        std::sort(g.begin(), g.end());
        for (const auto& lc : g) {
            std::error_code ec;
            std::string rp = fs::weakly_canonical(lc, ec).string();
            if (seen.insert(rp).second) out.push_back(lc);
        }
    }
    return out;
}

// Steam wraps the tree in one root key; some files do not. Returns the "apps" block or nullptr.
Vdf* descend(Vdf* node, const char* const* path, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        if (!node || !node->is_block()) return nullptr;
        node = node->find_ci(path[i]);
    }
    return node && node->is_block() ? node : nullptr;
}

Vdf* apps_dict(Vdf& cfg) {
    static const char* path[] = {"Software", "Valve", "Steam", "apps"};
    for (auto& kv : cfg.children())
        if (kv.second.is_block())
            if (Vdf* got = descend(&kv.second, path, 4)) return got;
    return descend(&cfg, path, 4);
}

std::string account_of(const std::string& lc) { return base_name(dir_name(dir_name(lc))); }

bool all_digits(const std::string& s) { return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; }); }

std::string launch_options(const Vdf& entry) {
    const Vdf* v = entry.find_ci("LaunchOptions");
    return v && !v->is_block() ? v->str() : "";
}

void set_launch_options(Vdf& entry, const std::string& value) {
    for (auto it = entry.children().begin(); it != entry.children().end();) {
        if (lower(it->first) == "launchoptions" && it->first != "LaunchOptions") it = entry.children().erase(it);
        else ++it;
    }
    entry.set("LaunchOptions", Vdf(value));
}

std::vector<std::pair<std::string, Vdf*>> games_of(Vdf& apps) {
    std::vector<std::pair<std::string, Vdf*>> out;
    for (auto& kv : apps.children()) if (all_digits(kv.first) && kv.second.is_block()) out.emplace_back(kv.first, &kv.second);
    return out;
}

bool steam_running_now() { return steam::steam_running(); }

Vdf load_vdf(const std::string& path) { return Vdf::parse(read_file(path).value_or("")); }
}  // namespace

std::string steam_baseline(const std::vector<std::string>& opts) {
    std::map<std::string, int> counts;
    for (const auto& o : opts) if (!o.empty()) ++counts[o];
    std::string best;
    int bc = 0;
    for (const auto& kv : counts) if (kv.second > bc) { best = kv.first; bc = kv.second; }
    return best;
}

static std::string baseline_of(Vdf& apps) {
    std::vector<std::string> opts;
    for (auto& g : games_of(apps)) opts.push_back(launch_options(*g.second));
    return steam_baseline(opts);
}

static int steam_list_custom() {
    auto files = find_localconfigs();
    if (files.empty()) { std::puts("no localconfig.vdf found - is Steam installed for this user?"); return 1; }
    for (const auto& lc : files) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        std::string acct = account_of(lc);
        if (!apps || apps->children().empty()) { std::printf("[%s] no apps block in %s\n", acct.c_str(), lc.c_str()); continue; }
        std::string base = baseline_of(*apps);
        std::vector<std::pair<std::string, std::string>> rows;
        for (auto& g : games_of(*apps)) rows.emplace_back(g.first, launch_options(*g.second));
        int on_base = 0;
        std::vector<std::pair<std::string, std::string>> custom;
        for (auto& r : rows) { if (!r.second.empty() && r.second == base) ++on_base; if (!r.second.empty() && r.second != base) custom.push_back(r); }
        std::printf("[%s] default (%d games): %s\n", acct.c_str(), on_base, base.empty() ? "-" : base.c_str());
        std::printf("[%s] %zu game(s) with their own options:\n", acct.c_str(), custom.size());
        std::sort(custom.begin(), custom.end(), [](auto& a, auto& b) { return std::stol(a.first) < std::stol(b.first); });
        auto base_toks = split_ws(base);
        std::set<std::string> base_set(base_toks.begin(), base_toks.end());
        for (auto& [aid, opt] : custom) {
            auto toks = split_ws(opt);
            std::vector<std::string> added, dropped;
            for (auto& t : toks) if (!base_set.count(t)) added.push_back(t);
            for (auto& t : base_toks) if (std::find(toks.begin(), toks.end(), t) == toks.end()) dropped.push_back(t);
            std::printf("  %-8s  %s\n", aid.c_str(), opt.c_str());
            if (!added.empty()) std::printf("            + %s\n", join(added, " ").c_str());
            if (!dropped.empty()) std::printf("            - %s\n", join(dropped, " ").c_str());
        }
    }
    return 0;
}

static int steam_list() {
    auto files = find_localconfigs();
    if (files.empty()) { std::puts("no localconfig.vdf found - is Steam installed for this user?"); return 1; }
    for (const auto& lc : files) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        std::string acct = account_of(lc);
        if (!apps || apps->children().empty()) { std::printf("[%s] no apps block in %s\n", acct.c_str(), lc.c_str()); continue; }
        auto games = games_of(*apps);
        std::vector<std::pair<std::string, std::string>> rows;
        for (auto& g : games) rows.emplace_back(g.first, launch_options(*g.second));
        std::sort(rows.begin(), rows.end(), [](auto& a, auto& b) { return std::stol(a.first) < std::stol(b.first); });
        std::printf("[%s] %zu games in %s\n", acct.c_str(), rows.size(), lc.c_str());
        for (auto& [aid, opt] : rows) std::printf("  %-8s  %s\n", aid.c_str(), opt.empty() ? "-" : opt.c_str());
    }
    return 0;
}

static int steam_write_all(const std::string& value, bool only_empty, bool dry, bool keep_custom) {
    if (steam_running_now()) { std::puts("Steam is running - quit Steam completely first (it overwrites localconfig.vdf on exit)."); return 2; }
    auto files = find_localconfigs();
    if (files.empty()) { std::puts("no localconfig.vdf found - is Steam installed for this user?"); return 1; }
    int total = 0;
    for (const auto& lc : files) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps || apps->children().empty()) { std::printf("  skip %s (no apps block)\n", lc.c_str()); continue; }
        int changed = 0, kept = 0;
        std::string base = keep_custom ? baseline_of(*apps) : "";
        for (auto& g : games_of(*apps)) {
            std::string cur = launch_options(*g.second);
            if (only_empty && !cur.empty()) continue;
            if (keep_custom && !cur.empty() && cur != base) { ++kept; continue; }
            if (cur == value && g.second->find_ci("LaunchOptions")) continue;
            set_launch_options(*g.second, value);
            ++changed;
        }
        std::string acct = account_of(lc);
        if (kept) std::printf("  %s: left %d game(s) with their own options untouched\n", acct.c_str(), kept);
        if (!changed) { std::printf("  %s: nothing to change\n", acct.c_str()); continue; }
        if (dry) std::printf("  %s: would set %d game(s)\n", acct.c_str(), changed);
        else {
            std::string bak = lc + ".tuxthrottle-bak-" + backup_stamp();
            copy_backup(lc);
            write_file(lc, cfg.dump());
            std::printf("  %s: set %d game(s)  (backup %s)\n", acct.c_str(), changed, base_name(bak).c_str());
        }
        total += changed;
    }
    std::printf("%s LaunchOptions on %d game(s)%s\n", dry ? "would set" : "set", total, dry ? "" : " - restart Steam to pick it up");
    return 0;
}

static int steam_remove_token(const std::string& token, bool dry) {
    if (trim(token).empty()) { std::puts("empty token - nothing to remove"); return 1; }
    if (steam_running_now()) { std::puts("Steam is running - quit Steam completely first (it overwrites localconfig.vdf on exit)."); return 2; }
    auto files = find_localconfigs();
    if (files.empty()) { std::puts("no localconfig.vdf found - is Steam installed for this user?"); return 1; }
    int total = 0;
    for (const auto& lc : files) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps || apps->children().empty()) continue;
        int changed = 0;
        for (auto& g : games_of(*apps)) {
            std::string cur = launch_options(*g.second);
            if (cur.find(token) == std::string::npos) continue;
            std::vector<std::string> parts;
            for (auto& p : split_ws(cur)) if (p != token) parts.push_back(p);
            set_launch_options(*g.second, join(parts, " "));
            ++changed;
        }
        if (!changed) continue;
        std::string acct = account_of(lc);
        if (dry) std::printf("  %s: would strip token from %d game(s)\n", acct.c_str(), changed);
        else {
            std::string bak = lc + ".tuxthrottle-bak-" + backup_stamp();
            copy_backup(lc);
            write_file(lc, cfg.dump());
            std::printf("  %s: stripped token from %d game(s)  (backup %s)\n", acct.c_str(), changed, base_name(bak).c_str());
        }
        total += changed;
    }
    std::printf("%s '%s' from %d game(s)%s\n", dry ? "would strip" : "stripped", token.c_str(), total, dry ? "" : " - restart Steam to pick it up");
    return 0;
}

int launchopts_main(const std::vector<std::string>& argv) {
    if (is_root() && env_or("TUXTHROTTLE_ALLOW_ROOT").empty()) { std::fputs("run this as your normal user, not root\n", stderr); return 1; }
    if (argv.empty()) { std::puts("usage: launchopts {list [--custom]|set-all <str>|clear-all|remove-token <tok>} [--b64] [--only-empty] [--keep-custom] [--dry-run]"); return 2; }
    std::string cmd = argv[0];
    std::vector<std::string> rest(argv.begin() + 1, argv.end());
    auto has = [&](const char* f) { return std::find(rest.begin(), rest.end(), f) != rest.end(); };
    bool dry = has("--dry-run");
    if (cmd == "list") return has("--custom") ? steam_list_custom() : steam_list();
    if (cmd == "clear-all") return steam_write_all("", false, dry, false);
    if (cmd == "set-all" || cmd == "remove-token") {
        bool ok, missing;
        std::string v = decode_arg(rest, &ok, &missing);
        if (!ok) { std::fputs("bad --b64 argument\n", stderr); return 1; }
        if (missing) { std::fprintf(stderr, "%s needs the string (or --b64 <blob>)\n", cmd.c_str()); return 1; }
        return cmd == "set-all" ? steam_write_all(v, has("--only-empty"), dry, has("--keep-custom")) : steam_remove_token(v, dry);
    }
    std::fprintf(stderr, "unknown command '%s' (list | set-all | clear-all | remove-token)\n", cmd.c_str());
    return 1;
}

std::vector<std::string> steam_game_ids() {
    std::vector<std::string> ids;
    for (const auto& lc : find_localconfigs()) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps) continue;
        for (auto& g : games_of(*apps)) if (std::find(ids.begin(), ids.end(), g.first) == ids.end()) ids.push_back(g.first);
    }
    return ids;
}

std::string steam_get_launch_options(const std::string& appid) {
    for (const auto& lc : find_localconfigs()) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps) continue;
        for (auto& g : games_of(*apps)) if (g.first == appid) return launch_options(*g.second);
    }
    return "";
}

bool steam_update_launch_options(const std::string& appid, const std::function<std::string(const std::string&)>& transform, bool dry) {
    bool changed = false;
    for (const auto& lc : find_localconfigs()) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps) continue;
        for (auto& g : games_of(*apps)) {
            if (g.first != appid) continue;
            std::string old = launch_options(*g.second), nw = transform(old);
            if (nw == old) continue;
            set_launch_options(*g.second, nw);
            changed = true;
            if (!dry) { copy_backup(lc); write_file(lc, cfg.dump()); }
        }
    }
    return changed;
}

std::vector<std::string> steam_roots_list() { return steam_roots(); }
bool steam_is_running() { return steam_running_now(); }

int steam_rewrite(const std::function<std::vector<std::string>(const std::vector<std::string>&)>& appids,
                  const std::function<std::string(const std::string&, const std::string&)>& change, bool dry) {
    if (steam_running_now()) { std::puts("Steam is running - quit Steam completely first (it overwrites localconfig.vdf on exit)."); return 2; }
    auto files = find_localconfigs();
    if (files.empty()) { std::puts("no localconfig.vdf found - is Steam installed for this user?"); return 1; }
    int total = 0;
    for (const auto& lc : files) {
        Vdf cfg = load_vdf(lc);
        Vdf* apps = apps_dict(cfg);
        if (!apps) continue;
        std::vector<std::string> existing;
        for (auto& g : games_of(*apps)) existing.push_back(g.first);
        int changed = 0;
        for (const auto& aid : appids(existing)) {
            Vdf* entry = apps->find_ci(aid);
            if (!entry) entry = &apps->set(aid, Vdf::block());
            std::string cur = launch_options(*entry);
            std::string nw = change(aid, cur);
            if (nw == cur) continue;
            set_launch_options(*entry, nw);
            ++changed;
        }
        if (changed && !dry) { copy_backup(lc); write_file(lc, cfg.dump()); }
        total += changed;
    }
    std::printf("%s LaunchOptions on %d game(s)%s\n", dry ? "would change" : "changed", total, (dry || !total) ? "" : " - restart Steam to pick it up");
    return 0;
}

// ---------------------------------------------------------------- Heroic
namespace {
const std::set<std::string> kPrimeVars = {"__NV_PRIME_RENDER_OFFLOAD", "__GLX_VENDOR_LIBRARY_NAME", "__VK_LAYER_NV_optimus", "DRI_PRIME"};

// Heroic's "Force use of NVIDIA Optimus or AMD Dynamic Switchable Graphics GPU" (nvidiaPrime) sets these itself,
// so they become that flag instead of environment entries.
void prime_to_flag(ParsedOpts& po) {
    for (auto it = po.env.begin(); it != po.env.end();) {
        if (kPrimeVars.count(it->first)) { po.flags["nvidiaPrime"] = true; it = po.env.erase(it); }
        else ++it;
    }
}

const char* kGamescopeFlags[] = {"enableUpscaling", "enableLimiter", "enableForceGrabCursor"};

std::vector<std::string> heroic_config_dirs() {
    std::vector<std::string> out;
    std::error_code ec;
    for (const std::string& d : {home() + "/.config/heroic/GamesConfig", home() + "/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig"})
        if (fs::is_directory(d, ec)) out.push_back(d);
    return out;
}

std::vector<std::string> heroic_game_files() {
    std::vector<std::string> out;
    for (const auto& d : heroic_config_dirs()) {
        auto g = glob_paths(d + "/*.json");
        std::sort(g.begin(), g.end());
        for (const auto& p : g) if (!starts_with(base_name(p), "gog-")) out.push_back(p);
    }
    return out;
}

bool heroic_running() { return run_argv({"pgrep", "-x", "heroic"}, 3000).code == 0; }

struct HTarget { std::string label, file; Json data; std::string key; bool global = false; };

// the settings dict of a target (mutable reference into its data)
Json* settings_of(HTarget& t) {
    if (t.global) {
        for (auto& kv : t.data.make_object()) if (kv.first == "defaultSettings") return &kv.second;
        return nullptr;
    }
    for (auto& kv : t.data.make_object()) if (kv.first == t.key) return &kv.second;
    return nullptr;
}

bool load_game_json(const std::string& path, HTarget& t) {
    auto txt = read_file(path);
    if (!txt) return false;
    try { t.data = Json::parse(*txt); } catch (const std::exception&) { return false; }
    if (!t.data.is_object()) return false;
    for (const auto& kv : t.data.object())
        if (kv.second.is_object() && (kv.second.has("winePrefix") || kv.second.has("enviromentOptions"))) { t.key = kv.first; t.label = kv.first; t.file = path; return true; }
    return false;
}

std::vector<HTarget> heroic_targets() {
    std::vector<HTarget> out;
    for (const auto& d : heroic_config_dirs()) {
        std::string cfg = dir_name(d) + "/config.json";
        auto txt = read_file(cfg);
        if (!txt) continue;
        try {
            Json data = Json::parse(*txt);
            if (data["defaultSettings"].is_object()) { HTarget t; t.label = "GLOBAL"; t.file = cfg; t.data = data; t.global = true; out.push_back(t); }
        } catch (const std::exception&) {}
    }
    for (const auto& p : heroic_game_files()) { HTarget t; if (load_game_json(p, t)) out.push_back(std::move(t)); }
    return out;
}

void heroic_save(const HTarget& t) {
    copy_backup(t.file);
    write_file(t.file, t.data.dump(2));
}

std::map<std::string, std::string> env_map(const Json& g) {
    std::map<std::string, std::string> cur;
    for (const auto& e : g["enviromentOptions"].array()) if (e.has("key")) cur[e["key"].str()] = e["value"].str();
    return cur;
}

void put_env(Json& g, const std::map<std::string, std::string>& cur) {
    Json arr = Json::arr();
    for (const auto& kv : cur) {
        Json e = Json::obj();
        e.set("key", kv.first);
        e.set("value", kv.second);
        arr.push(e);
    }
    g.set("enviromentOptions", arr);
}

bool gamescope_on(const Json& g) {
    const Json& gs = g["gamescope"];
    if (!gs.is_object()) return false;
    for (const char* k : kGamescopeFlags) {
        const Json& v = gs[k];
        if (v.is_bool() ? v.as_bool() : (v.is_number() ? v.as_number() != 0 : !v.str().empty())) return true;
    }
    return false;
}

void gamescope_off(Json& g) {
    Json* gs = nullptr;
    for (auto& kv : g.make_object()) if (kv.first == "gamescope") gs = &kv.second;
    if (!gs || !gs->is_object()) return;
    for (const char* k : kGamescopeFlags) if (gs->has(k)) gs->set(k, false);
}
}  // namespace

static int heroic_list() {
    for (const auto& p : heroic_game_files()) {
        HTarget t;
        if (!load_game_json(p, t)) continue;
        const Json& g = t.data[t.key];
        std::vector<std::string> keys;
        for (const auto& e : g["enviromentOptions"].array()) keys.push_back(e["key"].str());
        auto b = [&](const char* k) { return g.has(k) ? (g[k].as_bool() ? "True" : "False") : "None"; };
        std::printf("%s\tgamemode=%s\tmangohud=%s\twrappers=%zu\tenv=[%s]\n", t.key.c_str(), b("useGameMode"), b("showMangohud"),
                    g["wrapperOptions"].array().size(), join(keys, ",").c_str());
    }
    return 0;
}

static int heroic_set_all(const std::string& opts, bool only_empty, bool dry, bool no_wayland) {
    ParsedOpts po = parse_opts(opts);
    prime_to_flag(po);
    int n = 0;
    for (auto& t : heroic_targets()) {
        Json* g = settings_of(t);
        if (!g) continue;
        if (only_empty && (!(*g)["enviromentOptions"].array().empty() || !(*g)["wrapperOptions"].array().empty())) { std::printf("skip %s (already has options)\n", t.label.c_str()); continue; }
        auto cur = env_map(*g);
        for (const auto& kv : po.env) cur[kv.first] = kv.second;
        if ((po.flags.count("nvidiaPrime") || cur.count("__NV_PRIME_RENDER_OFFLOAD")) && gamescope_on(*g)) {
            gamescope_off(*g);
            std::printf("  %s: PRIME offload is set, gamescope switched off\n", t.label.c_str());
        }
        if (po.flags.count("nvidiaPrime")) for (const auto& v : kPrimeVars) cur.erase(v);
        put_env(*g, cur);
        for (const auto& kv : po.flags) g->set(kv.first, kv.second);
        if (no_wayland) g->set("enableWineWayland", false);
        std::set<std::string> known;
        for (const auto& w : (*g)["wrapperOptions"].array()) known.insert(w["exe"].str());
        for (const auto& w : po.wrappers) {
            if (known.count(w)) continue;
            Json e = Json::obj();
            e.set("exe", w);
            e.set("args", "");
            Json arr = (*g)["wrapperOptions"].is_array() ? (*g)["wrapperOptions"] : Json::arr();
            arr.push(e);
            g->set("wrapperOptions", arr);
        }
        std::printf("%s %s\n", dry ? "would set" : "set", t.label.c_str());
        if (!dry) heroic_save(t);
        ++n;
    }
    std::printf("%d game(s) %supdated\n", n, dry ? "would be " : "");
    return 0;
}

static int heroic_remove_token(const std::string& opts, bool dry, bool no_wayland) {
    ParsedOpts po = parse_opts(opts);
    bool had_prime = false;
    for (const auto& kv : po.env) if (kPrimeVars.count(kv.first)) had_prime = true;
    prime_to_flag(po);
    if (had_prime) for (const auto& v : kPrimeVars) po.env[v] = "";   // also strip any old PRIME env entries
    auto is_prime_entry = [&](const Json& e) { return had_prime && kPrimeVars.count(e["key"].str()) > 0; };
    int n = 0;
    for (auto& t : heroic_targets()) {
        Json* g = settings_of(t);
        if (!g) continue;
        std::string before = g->dump(0, true);
        Json envs = Json::arr();
        for (const auto& e : (*g)["enviromentOptions"].array()) {
            if (is_prime_entry(e)) continue;
            auto it = po.env.find(e["key"].str());
            if (it != po.env.end() && it->second == e["value"].str()) continue;
            envs.push(e);
        }
        g->set("enviromentOptions", envs);
        for (const auto& kv : po.flags) g->set(kv.first, false);
        if (no_wayland) g->set("enableWineWayland", true);
        Json wr = Json::arr();
        for (const auto& w : (*g)["wrapperOptions"].array())
            if (std::find(po.wrappers.begin(), po.wrappers.end(), w["exe"].str()) == po.wrappers.end()) wr.push(w);
        g->set("wrapperOptions", wr);
        if (g->dump(0, true) != before) {
            std::printf("%s %s\n", dry ? "would strip" : "stripped", t.label.c_str());
            if (!dry) heroic_save(t);
            ++n;
        }
    }
    std::printf("%d game(s) %schanged\n", n, dry ? "would be " : "");
    return 0;
}

static int heroic_fix(bool dry) {
    if (heroic_running() && !dry) { std::puts("Heroic is running - close it first (it would overwrite the change)"); return 1; }
    int n = 0;
    for (auto& t : heroic_targets()) {
        Json* g = settings_of(t);
        if (!g) continue;
        auto cur = env_map(*g);
        bool stale_env = false;
        for (const auto& v : kPrimeVars) if (cur.count(v)) stale_env = true;
        bool missing = !(g->has("nvidiaPrime") && (*g)["nvidiaPrime"].as_bool()) || stale_env;
        bool gs = gamescope_on(*g);
        if (!missing && !gs) continue;
        for (const auto& v : kPrimeVars) cur.erase(v);
        put_env(*g, cur);
        g->set("nvidiaPrime", true);
        gamescope_off(*g);
        std::printf("%s %s%s%s\n", dry ? "would fix" : "fixed", t.label.c_str(), gs ? " (gamescope off)" : "", missing ? " (+nvidiaPrime)" : "");
        if (!dry) heroic_save(t);
        ++n;
    }
    std::printf("%d target(s) %sfixed\n", n, dry ? "would be " : "");
    return 0;
}

int heroicopts_main(const std::vector<std::string>& a) {
    if (a.empty() || (a[0] != "list" && a[0] != "set-all" && a[0] != "remove-token" && a[0] != "fix")) {
        std::puts("usage: heroicopts {list|set-all <str>|remove-token <str>|fix} [--b64] [--only-empty] [--no-wayland] [--dry-run]");
        return 2;
    }
    if (heroic_config_dirs().empty()) { std::puts("no Heroic GamesConfig directory found"); return 1; }
    if (a[0] == "list") return heroic_list();
    bool dry = std::find(a.begin(), a.end(), "--dry-run") != a.end();
    if (a[0] == "fix") return heroic_fix(dry);
    std::vector<std::string> rest;
    for (size_t i = 1; i < a.size(); ++i) if (a[i] != "--dry-run" && a[i] != "--only-empty" && a[i] != "--no-wayland") rest.push_back(a[i]);
    std::string opts;
    auto b64 = std::find(rest.begin(), rest.end(), "--b64");
    if (!rest.empty() && rest[0] == "--b64" && rest.size() > 1) {
        try { opts = base64_decode(rest[1]); } catch (const std::exception&) { std::puts("bad --b64 argument"); return 2; }
    } else if (!rest.empty()) opts = rest[0];
    else { std::puts("missing launch-options string"); return 2; }
    (void)b64;
    if (heroic_running() && !dry) { std::puts("Heroic is running - close it first (it would overwrite the change)"); return 1; }
    try {
        bool no_wayland = std::find(a.begin(), a.end(), "--no-wayland") != a.end();
        if (a[0] == "set-all") return heroic_set_all(opts, std::find(a.begin(), a.end(), "--only-empty") != a.end(), dry, no_wayland);
        return heroic_remove_token(opts, dry, no_wayland);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 1;
    }
}

// ---------------------------------------------------------------- Lutris
namespace {
std::string lutris_games_dir() { return home() + "/.local/share/lutris/games"; }
std::string lutris_global() { return home() + "/.config/lutris/system.yml"; }

std::vector<std::pair<std::string, std::string>> lutris_targets() {
    std::vector<std::pair<std::string, std::string>> out;
    std::error_code ec;
    if (fs::is_directory(dir_name(lutris_global()), ec)) out.emplace_back("GLOBAL", lutris_global());
    if (fs::is_directory(lutris_games_dir(), ec)) {
        auto g = glob_paths(lutris_games_dir() + "/*.yml");
        std::sort(g.begin(), g.end());
        for (const auto& p : g) { std::string b = base_name(p); out.emplace_back(b.substr(0, b.size() - 4), p); }
    }
    return out;
}

Yaml load_yaml(const std::string& p) {
    auto t = read_file(p);
    if (!t) return Yaml::make_map();
    Yaml y = Yaml::parse(*t);
    return y.is_null() ? Yaml::make_map() : y;
}

void save_yaml(const std::string& p, const Yaml& y) {
    if (path_exists(p)) copy_backup(p);
    write_file(p, y.dump());
}

Yaml& system_of(Yaml& data) {
    Yaml* s = data.get("system");
    if (!s || !s->is_map()) { data.set("system", Yaml::make_map()); s = data.get("system"); }
    return *s;
}
}  // namespace

static bool lutris_running() { return run_argv({"pgrep", "-x", "lutris"}, 3000).code == 0; }

static int lutris_list() {
    for (const auto& [label, p] : lutris_targets()) {
        Yaml data;
        try { data = load_yaml(p); } catch (const std::exception&) { continue; }
        const Yaml* s = data.get("system");
        auto show = [&](const char* k) {
            const Yaml* v = s ? s->get(k) : nullptr;
            return !v ? std::string("None") : (v->is_true() ? "True" : (v->kind == Yaml::Kind::Scalar && !v->quoted && (v->scalar == "false" || v->scalar == "False") ? "False" : v->scalar));
        };
        std::vector<std::string> keys;
        if (s && s->get("env") && s->get("env")->is_map()) for (const auto& kv : s->get("env")->map) keys.push_back("'" + kv.first + "'");
        std::sort(keys.begin(), keys.end());
        const Yaml* pc = s ? s->get("prefix_command") : nullptr;
        std::printf("%s\tgamemode=%s\tmangohud=%s\tprefix_command=%s\tenv=[%s]\n", label.c_str(), show("gamemode").c_str(), show("mangohud").c_str(),
                    pc ? ("'" + pc->scalar + "'").c_str() : "None", join(keys, ", ").c_str());
    }
    return 0;
}

static int lutris_set_all(const std::string& opts, bool only_empty, bool dry) {
    ParsedOpts po = parse_opts(opts);
    int n = 0;
    for (const auto& [label, p] : lutris_targets()) {
        Yaml data = load_yaml(p);
        Yaml& s = system_of(data);
        const Yaml* env0 = s.get("env");
        const Yaml* pc0 = s.get("prefix_command");
        if (only_empty && ((env0 && env0->truthy()) || (pc0 && pc0->truthy()))) { std::printf("skip %s (already has options)\n", label.c_str()); continue; }
        if (!s.get("env") || !s.get("env")->is_map()) s.set("env", Yaml::make_map());
        Yaml& env = *s.get("env");
        for (const auto& kv : po.env) env.set(kv.first, Yaml::make_scalar(kv.second, true));
        const Yaml* gs = s.get("gamescope");
        if (gs && gs->truthy() && env.get("__NV_PRIME_RENDER_OFFLOAD")) s.set("gamescope", Yaml::make_bool(false));
        if (po.flags.count("useGameMode")) s.set("gamemode", Yaml::make_bool(true));
        if (po.flags.count("showMangohud")) s.set("mangohud", Yaml::make_bool(true));
        auto cur = split_ws(s.get("prefix_command") ? s.get("prefix_command")->str() : "");
        for (const auto& w : po.wrappers) if (std::find(cur.begin(), cur.end(), w) == cur.end()) cur.push_back(w);
        if (!cur.empty()) s.set("prefix_command", Yaml::make_scalar(join(cur, " "), true));
        std::printf("%s %s\n", dry ? "would set" : "set", label.c_str());
        if (!dry) save_yaml(p, data);
        ++n;
    }
    std::printf("%d target(s) %supdated\n", n, dry ? "would be " : "");
    return 0;
}

static int lutris_remove_token(const std::string& opts, bool dry) {
    ParsedOpts po = parse_opts(opts);
    int n = 0;
    for (const auto& [label, p] : lutris_targets()) {
        Yaml data = load_yaml(p);
        Yaml* s = data.get("system");
        if (!s || !s->is_map()) continue;
        std::string before = s->dump();
        if (Yaml* e = s->get("env")) if (e->is_map())
            for (const auto& kv : po.env) { Yaml* cur = e->get(kv.first); if (cur && cur->str() == kv.second) e->erase(kv.first); }
        if (po.flags.count("useGameMode")) s->erase("gamemode");
        if (po.flags.count("showMangohud")) s->erase("mangohud");
        std::vector<std::string> pc;
        for (const auto& w : split_ws(s->get("prefix_command") ? s->get("prefix_command")->str() : ""))
            if (std::find(po.wrappers.begin(), po.wrappers.end(), w) == po.wrappers.end()) pc.push_back(w);
        if (!pc.empty()) s->set("prefix_command", Yaml::make_scalar(join(pc, " "), true));
        else s->erase("prefix_command");
        if (s->dump() != before) {
            std::printf("%s %s\n", dry ? "would strip" : "stripped", label.c_str());
            if (!dry) save_yaml(p, data);
            ++n;
        }
    }
    std::printf("%d target(s) %schanged\n", n, dry ? "would be " : "");
    return 0;
}

int lutrisopts_main(const std::vector<std::string>& a) {
    if (a.empty() || (a[0] != "list" && a[0] != "set-all" && a[0] != "remove-token")) {
        std::puts("usage: lutrisopts {list|set-all|remove-token} --b64 <base64-of-string> [--only-empty] [--dry-run]");
        return 2;
    }
    if (a[0] == "list") return lutris_list();
    bool dry = std::find(a.begin(), a.end(), "--dry-run") != a.end();
    std::vector<std::string> rest;
    for (size_t i = 1; i < a.size(); ++i) if (a[i] != "--dry-run" && a[i] != "--only-empty") rest.push_back(a[i]);
    std::string opts;
    if (!rest.empty() && rest[0] == "--b64" && rest.size() > 1) {
        try { opts = base64_decode(rest[1]); } catch (const std::exception&) { std::puts("bad --b64 argument"); return 2; }
    } else if (!rest.empty()) opts = rest[0];
    else { std::puts("missing launch-options string"); return 2; }
    if (!dry && lutris_running()) { std::puts("Lutris is running - close it first"); return 1; }
    try {
        return a[0] == "set-all" ? lutris_set_all(opts, std::find(a.begin(), a.end(), "--only-empty") != a.end(), dry) : lutris_remove_token(opts, dry);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 1;
    }
}

// ---------------------------------------------------------------- Lutris shader-cache env
std::map<std::string, std::string> lutris_wanted_env(const std::string& cache) {
    return {{"DXVK_HUD", "compiler"}, {"DXVK_STATE_CACHE_PATH", cache}, {"STAGING_SHARED_MEMORY", "1"}, {"__GL_SHADER_DISK_CACHE", "1"},
            {"__GL_SHADER_DISK_CACHE_PATH", cache}, {"__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "1"}};
}

namespace {
struct LutrisDirs { std::string db; std::vector<std::string> dirs; };

std::vector<LutrisDirs> lutris_dirs() {
    std::vector<LutrisDirs> out;
    std::string h = home();
    for (auto [data, conf] : {std::pair<std::string, std::string>{h + "/.local/share/lutris", h + "/.config/lutris"},
                              {h + "/.var/app/net.lutris.Lutris/data/lutris", h + "/.var/app/net.lutris.Lutris/config/lutris"}}) {
        std::string db = data + "/pga.db";
        if (path_exists(db)) out.push_back({db, {conf + "/games", data + "/games"}});
    }
    return out;
}

std::string lutris_yml(const std::vector<std::string>& dirs, const std::string& configpath) {
    for (const auto& d : dirs) {
        std::string p = d + "/" + configpath + ".yml";
        if (path_exists(p)) return p;
    }
    return "";
}

struct LutrisRow { std::string id, name, runner, directory, configpath; };

std::vector<LutrisRow> lutris_rows(const std::string& db, bool installed_only = true) {
    std::vector<LutrisRow> out;
    SqliteReader r(db);
    auto t = r.read_table("games");
    auto col = [&](const char* n) {
        for (size_t i = 0; i < t.columns.size(); ++i) if (t.columns[i] == n) return static_cast<int>(i);
        return -1;
    };
    int ci = col("id"), cn = col("name"), cr = col("runner"), cd = col("directory"), cc = col("configpath"), cs = col("installed");
    for (const auto& row : t.rows) {
        auto get = [&](int i) { return i >= 0 && static_cast<size_t>(i) < row.size() ? row[static_cast<size_t>(i)].str() : std::string(); };
        if (installed_only && cs >= 0 && row[static_cast<size_t>(cs)].integer() != 1) continue;
        out.push_back({get(ci), get(cn), get(cr), get(cd), get(cc)});
    }
    return out;
}

struct WineGame { std::string name, yml, dir; };

std::vector<WineGame> lutris_wine_games() {
    std::vector<WineGame> out;
    for (const auto& ld : lutris_dirs())
        for (const auto& r : lutris_rows(ld.db)) {
            if (r.runner != "wine" || r.configpath.empty()) continue;
            std::string yml = lutris_yml(ld.dirs, r.configpath);
            if (!yml.empty()) out.push_back({r.name.empty() ? r.configpath : r.name, yml, r.directory});
        }
    return out;
}

std::string cache_dir_for(const Yaml& data, const std::string& directory) {
    const Yaml* game = data.get("game");
    std::string prefix = game && game->is_map() && game->get("prefix") ? game->get("prefix")->str() : "";
    return !prefix.empty() ? prefix : directory;
}
}  // namespace

static int lutris_env_process(const std::string& action, bool overwrite, bool dry) {
    auto games = lutris_wine_games();
    if (games.empty()) { std::puts("No installed Lutris Wine games found."); return 0; }
    std::string stamp = backup_stamp();
    int touched = 0;
    for (const auto& g : games) {
        Yaml data;
        try { data = load_yaml(g.yml); } catch (const std::exception& e) { std::fprintf(stderr, "  ! %s: can't read %s: %s\n", g.name.c_str(), g.yml.c_str(), e.what()); continue; }
        if (!data.is_map()) continue;
        std::string cache = cache_dir_for(data, g.dir);
        if (cache.empty()) { std::printf("  - %s: no prefix or install directory, skipped\n", g.name.c_str()); continue; }
        Yaml* sys = data.get("system");
        if (!sys || sys->is_null()) { data.set("system", Yaml::make_map()); sys = data.get("system"); }
        if (!sys->is_map()) { std::fprintf(stderr, "  ! %s: unexpected 'system' section, skipped\n", g.name.c_str()); continue; }
        Yaml* env = sys->get("env");
        if (!env || env->is_null()) { sys->set("env", Yaml::make_map()); env = sys->get("env"); }
        if (!env->is_map()) { std::fprintf(stderr, "  ! %s: unexpected 'env' section, skipped\n", g.name.c_str()); continue; }
        auto want = lutris_wanted_env(cache);
        std::vector<std::string> keys;
        if (action == "apply") {
            for (const auto& kv : want) {
                Yaml* cur = env->get(kv.first);
                if (cur && (!overwrite || cur->str() == kv.second)) continue;
                env->set(kv.first, Yaml::make_scalar(kv.second, true));
                keys.push_back(kv.first);
            }
        } else {
            for (const auto& kv : want) {
                Yaml* cur = env->get(kv.first);
                if (cur && cur->str() == kv.second) { env->erase(kv.first); keys.push_back(kv.first); }
            }
            if (env->map.empty()) sys->erase("env");
        }
        if (keys.empty()) { std::printf("  = %s: nothing to change\n", g.name.c_str()); continue; }
        std::printf("  %s %s: %s  (%s)\n", action == "apply" ? "+" : "-", g.name.c_str(), join(keys, ", ").c_str(), cache.c_str());
        ++touched;
        if (dry) continue;
        std::error_code ec;
        fs::copy_file(g.yml, g.yml + ".tuxthrottle-bak-" + stamp, fs::copy_options::overwrite_existing, ec);
        write_file(g.yml, data.dump());
    }
    std::printf("\nDone. %s %d of %zu Lutris Wine game(s).\n", dry ? "would change" : "changed", touched, games.size());
    return 0;
}

static int lutris_env_list() {
    auto games = lutris_wine_games();
    if (games.empty()) { std::puts("No installed Lutris Wine games found."); return 0; }
    for (const auto& g : games) {
        std::string state = "?";
        try {
            Yaml data = load_yaml(g.yml);
            const Yaml* sys = data.get("system");
            const Yaml* env = sys && sys->is_map() ? sys->get("env") : nullptr;
            auto want = lutris_wanted_env(cache_dir_for(data, g.dir));
            int have = 0;
            for (const auto& kv : want) { const Yaml* v = env && env->is_map() ? env->get(kv.first) : nullptr; if (v && v->str() == kv.second) ++have; }
            state = std::to_string(have) + "/" + std::to_string(want.size());
        } catch (const std::exception&) {}
        std::printf("  %4s  %s  (%s)\n", state.c_str(), g.name.c_str(), g.yml.c_str());
    }
    return 0;
}

int lutris_env_main(const std::vector<std::string>& a) {
    if (a.empty() || (a[0] != "list" && a[0] != "apply" && a[0] != "remove")) { std::puts("usage: lutris-env {list|apply|remove} [--overwrite] [--dry-run]"); return 2; }
    bool dry = std::find(a.begin(), a.end(), "--dry-run") != a.end(), overwrite = std::find(a.begin(), a.end(), "--overwrite") != a.end();
    if (a[0] == "list") return lutris_env_list();
    return lutris_env_process(a[0], overwrite, dry);
}

// ---------------------------------------------------------------- anti-cheat scan
namespace {
struct AntiCheat { const char* name; std::vector<std::string> pats; };
const std::vector<AntiCheat>& anticheats() {
    static const std::vector<AntiCheat> v = {
        {"EasyAntiCheat", {"easyanticheat", "easyanticheat_eos", "easyanticheat*", "start_protected_game.exe"}},
        {"BattlEye", {"battleye", "beservice*", "beclient*", "*_be.exe"}},
        {"Vanguard", {"vgk.sys", "vgc.exe", "riot vanguard"}},
        {"nProtect GameGuard", {"gameguard", "gameguard.des"}},
        {"XIGNCODE3", {"xigncode", "x3.xem", "xigncode*"}},
        {"PunkBuster", {"pnkbstra.exe", "pnkbstrb.exe", "pb"}},
        {"EQU8", {"equ8", "equ8_conf.json"}},
        {"Denuvo Anti-Cheat", {"denuvo-anti-cheat*"}},
    };
    return v;
}
bool match_pat(const std::string& name, const std::string& pat) {
    if (!pat.empty() && pat.back() == '*') return starts_with(name, pat.substr(0, pat.size() - 1));
    if (!pat.empty() && pat.front() == '*') return ends_with(name, pat.substr(1));
    return name == pat;
}
}  // namespace

std::string scan_anticheat(const std::string& path) {
    std::error_code ec;
    if (path.empty() || !fs::is_directory(path, ec)) return "";
    int seen = 0;
    std::vector<std::pair<std::string, int>> stack{{path, 0}};
    while (!stack.empty()) {
        auto [d, depth] = stack.back();
        stack.pop_back();
        std::vector<fs::directory_entry> entries;
        for (auto it = fs::directory_iterator(d, ec); !ec && it != fs::directory_iterator(); it.increment(ec)) entries.push_back(*it);
        ec.clear();
        for (const auto& e : entries) {
            if (++seen > 6000) return "";
            std::string name = lower(e.path().filename().string());
            bool is_dir = e.is_directory(ec) && !e.is_symlink(ec);
            for (const auto& ac : anticheats())
                for (const auto& p : ac.pats)
                    if (match_pat(name, p) && (p != "pb" || (depth == 0 && e.is_directory(ec)))) return ac.name;
            if (depth + 1 < 3 && is_dir) stack.emplace_back(e.path().string(), depth + 1);
        }
    }
    return "";
}

std::map<std::string, std::string> anticheat_for(const std::map<std::string, std::string>& paths) {
    std::string cf = home() + "/.cache/tuxthrottle/anticheat.json";
    Json cache = read_json_file(cf);
    std::map<std::string, std::string> out;
    bool dirty = false;
    for (const auto& [key, p] : paths) {
        struct stat st;
        long long mt = !p.empty() && ::stat(p.c_str(), &st) == 0 ? static_cast<long long>(st.st_mtime) : -1;
        if (mt < 0) { out[key] = ""; continue; }
        const Json& hit = cache[p];
        if (!(hit.is_object() && static_cast<long long>(hit["mtime"].as_number(-1)) == mt)) {
            Json h = Json::obj();
            h.set("mtime", static_cast<double>(mt));
            h.set("ac", scan_anticheat(p));
            cache.set(p, h);
            dirty = true;
            out[key] = scan_anticheat(p) ;
            continue;
        }
        out[key] = hit["ac"].str();
    }
    if (dirty) {
        mkdir_p(dir_name(cf));
        write_file(cf, cache.dump());
    }
    return out;
}

// ---------------------------------------------------------------- MangoHud-tab game discovery
namespace {
std::vector<std::string> heroic_dirs() {
    std::vector<std::string> out;
    std::error_code ec;
    for (const std::string& d : {home() + "/.config/heroic", home() + "/.var/app/com.heroicgameslauncher.hgl/config/heroic"})
        if (fs::is_directory(d, ec)) out.push_back(d);
    return out;
}

Json read_json_loose(const std::string& p) {
    auto t = read_file(p);
    if (!t) return Json();
    try { return Json::parse(*t); } catch (const std::exception&) { return Json(); }
}

struct HeroicInstalled { std::string store, app, title, path; };

std::vector<HeroicInstalled> heroic_installed(const std::string& base) {
    std::vector<HeroicInstalled> out;
    Json d = read_json_loose(base + "/legendaryConfig/legendary/installed.json");
    if (d.is_object())
        for (const auto& kv : d.object()) if (kv.second.is_object()) out.push_back({"Epic", kv.first, kv.second["title"].str().empty() ? kv.first : kv.second["title"].str(), kv.second["install_path"].str()});
    std::map<std::string, std::string> titles;
    Json lib = read_json_loose(base + "/store_cache/gog_library.json");
    if (lib.is_object()) for (const auto& g : lib["games"].array()) if (g.is_object()) titles[g["app_name"].str()] = g["title"].str();
    d = read_json_loose(base + "/gog_store/installed.json");
    if (d.is_object())
        for (const auto& e : d["installed"].array())
            if (e.is_object() && !e["appName"].str().empty()) {
                std::string app = e["appName"].str(), path = e["install_path"].str();
                std::string title = titles[app].empty() ? base_name(path) : titles[app];
                out.push_back({"GOG", app, title.empty() ? app : title, path});
            }
    d = read_json_loose(base + "/nile_config/nile/installed.json");
    if (d.is_array())
        for (const auto& e : d.array())
            if (e.is_object() && !e["id"].str().empty()) { std::string path = e["path"].str(); out.push_back({"Amazon", e["id"].str(), base_name(path).empty() ? e["id"].str() : base_name(path), path}); }
    d = read_json_loose(base + "/sideload_apps/library.json");
    if (d.is_object())
        for (const auto& e : d["games"].array())
            if (e.is_object() && !e["app_name"].str().empty() && (!e.has("is_installed") || e["is_installed"].as_bool())) {
                std::string exe = e["install"]["executable"].str();
                out.push_back({"Sideload", e["app_name"].str(), e["title"].str().empty() ? e["app_name"].str() : e["title"].str(), exe.empty() ? "" : dir_name(exe)});
            }
    return out;
}
}  // namespace

std::vector<OtherGame> heroic_games() {
    std::vector<OtherGame> rows;
    for (const auto& base : heroic_dirs())
        for (const auto& h : heroic_installed(base)) {
            Json cfg = read_json_loose(base + "/GamesConfig/" + h.app + ".json");
            bool on = cfg.is_object() && cfg[h.app]["showMangohud"].as_bool();
            OtherGame g;
            g.id = "heroic:" + h.app;
            g.source = "Heroic · " + h.store;
            g.appid = h.app;
            g.name = h.title;
            g.wanted = g.enabled = on;
            g.path = h.path;
            g.base = base;
            rows.push_back(g);
        }
    return rows;
}

std::vector<OtherGame> lutris_games() {
    std::vector<OtherGame> rows;
    for (const auto& ld : lutris_dirs())
        for (const auto& r : lutris_rows(ld.db)) {
            if (r.runner == "steam" || r.configpath.empty()) continue;
            std::string yml = lutris_yml(ld.dirs, r.configpath);
            bool on = false;
            if (!yml.empty()) {
                try {
                    Yaml data = load_yaml(yml);
                    const Yaml* s = data.get("system");
                    on = s && s->is_map() && s->get("mangohud") && s->get("mangohud")->truthy();
                } catch (const std::exception&) {}
            }
            OtherGame g;
            g.id = "lutris:" + r.id;
            g.source = "Lutris · " + (r.runner.empty() ? std::string("?") : r.runner);
            g.appid = r.id;
            g.name = r.name.empty() ? r.configpath : r.name;
            g.wanted = g.enabled = on;
            g.path = r.directory;
            g.yml = yml;
            rows.push_back(g);
        }
    return rows;
}

std::vector<OtherGame> other_games() {
    auto a = heroic_games();
    auto b = lutris_games();
    a.insert(a.end(), b.begin(), b.end());
    return a;
}

static bool heroic_set(const std::string& app, bool on) {
    bool done = false;
    for (const auto& base : heroic_dirs()) {
        bool found = false;
        for (const auto& h : heroic_installed(base)) found = found || h.app == app;
        if (!found) continue;
        std::string cf = base + "/GamesConfig/" + app + ".json";
        Json cfg = read_json_loose(cf);
        if (!cfg.is_object()) { cfg = Json::obj(); cfg.set("version", "v0"); }
        Json inner = cfg[app].is_object() ? cfg[app] : Json::obj();
        inner.set("showMangohud", on);
        cfg.set(app, inner);
        mkdir_p(dir_name(cf));
        write_file(cf, cfg.dump(2));
        done = true;
    }
    return done;
}

static bool lutris_set(const std::string& gid, bool on) {
    for (const auto& g : lutris_games()) {
        if (g.appid != gid || g.yml.empty()) continue;
        try {
            Yaml data = load_yaml(g.yml);
            if (!data.is_map()) return false;
            Yaml& s = system_of(data);
            s.set("mangohud", Yaml::make_bool(on));
            write_file(g.yml, data.dump());
            return true;
        } catch (const std::exception&) { return false; }
    }
    return false;
}

bool set_game_mangohud(const std::string& game_id, bool on) {
    size_t c = game_id.find(':');
    if (c == std::string::npos) return false;
    std::string src = game_id.substr(0, c), rest = game_id.substr(c + 1);
    if (src == "heroic") return heroic_set(rest, on);
    if (src == "lutris") return lutris_set(rest, on);
    return false;
}

}  // namespace tt::launchers
