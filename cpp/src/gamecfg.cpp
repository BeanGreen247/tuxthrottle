#include "gamecfg.hpp"

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <set>

#include "formats.hpp"
#include "launchers.hpp"
#include "sensors.hpp"
#include "steam.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace fs = std::filesystem;

namespace tt::fpslimit {

static const char* kLimitKeys[] = {"STRANGLE_FPS", "ENABLE_VK_LAYER_TORKEL104_libstrangle", "DXVK_FRAME_RATE"};
static bool is_limit_key(const std::string& k) { for (const char* l : kLimitKeys) if (k == l) return true; return false; }
static std::string home() { return env_or("HOME", "/root"); }
static std::string overrides_path() { return home() + "/.config/tuxthrottle/fpslimits.json"; }

std::map<std::string, std::string> env_for(int fps, const std::string& method) {
    if (method == "strangle") return {{"STRANGLE_FPS", std::to_string(fps)}, {"ENABLE_VK_LAYER_TORKEL104_libstrangle", "1"}};
    return {{"DXVK_FRAME_RATE", std::to_string(fps)}};
}

std::string env_string(int fps, const std::string& method) {
    // dict order of the Python reference: STRANGLE_FPS first, then the layer switch
    if (method == "strangle") return "STRANGLE_FPS=" + std::to_string(fps) + " ENABLE_VK_LAYER_TORKEL104_libstrangle=1";
    return "DXVK_FRAME_RATE=" + std::to_string(fps);
}

static std::set<std::string> words(const std::string& s) {
    std::string t;
    for (char c : lower(s)) t += std::isalnum(static_cast<unsigned char>(c)) ? c : ' ';
    auto w = split_ws(t);
    return std::set<std::string>(w.begin(), w.end());
}

std::string norm_name(const std::string& name) {
    auto w = words(name);
    return join(std::vector<std::string>(w.begin(), w.end()), " ");
}

std::pair<std::optional<int>, std::string> recommended(const std::string& name) {
    Json data = read_json_file(sensors::app_dir() + "/config/fps_limits.json");
    auto w = words(name);
    for (const auto& g : data["games"].array()) {
        bool all = true;
        for (const auto& m : g["match"].array()) all = all && w.count(m.str());
        if (all) return {static_cast<int>(g["fps"].as_number()), g["note"].str()};
    }
    return {std::nullopt, ""};
}

static Json overrides() {
    Json o = read_json_file(overrides_path());
    return o;
}

static void save_overrides(const Json& o) {
    mkdir_p(dir_name(overrides_path()));
    write_file(overrides_path(), o.dump(2, true));
}

std::string steam_opts_with(const std::string& opts, const std::map<std::string, std::string>* env) {
    std::vector<std::string> toks;
    for (const auto& t : split_ws(opts)) if (!is_limit_key(t.substr(0, t.find('=')))) toks.push_back(t);
    if (env && !env->empty()) {
        std::vector<std::string> add;
        for (const auto& kv : *env) add.push_back(kv.first + "=" + kv.second);
        // keep the strangle-then-layer order of the reference implementation
        std::sort(add.begin(), add.end(), [](const std::string& a, const std::string& b) {
            auto rank = [](const std::string& s) { return starts_with(s, "STRANGLE_FPS") ? 0 : starts_with(s, "ENABLE_VK") ? 1 : 2; };
            return rank(a) < rank(b);
        });
        bool has_cmd = std::find(toks.begin(), toks.end(), "%command%") != toks.end();
        std::vector<std::string> out = add;
        if (!has_cmd) out.push_back("%command%");
        out.insert(out.end(), toks.begin(), toks.end());
        toks = out;
    }
    return join(toks, " ");
}

struct Game { std::string launcher, id, name, store, base, yml; };

static std::vector<Game> collect_games() {
    std::vector<Game> games;
    for (const auto& g : launchers::heroic_games()) games.push_back({"heroic", g.appid, g.name, g.source, g.base, ""});
    for (const auto& g : launchers::lutris_games()) if (!g.yml.empty()) games.push_back({"lutris", g.appid, g.name, g.source, "", g.yml});
    auto root = steam::steam_root_opt();
    if (root) {
        std::set<std::string> seen;
        for (const auto& aid : launchers::steam_game_ids()) {
            std::string name = steam::appid_name(*root, aid);
            if (starts_with(name, "Proton") || starts_with(name, "Steam Linux Runtime") || starts_with(name, "Steamworks Common")) continue;
            if (!name.empty() && seen.insert(aid).second) games.push_back({"steam", aid, name, "Steam", "", ""});
        }
    }
    return games;
}

static std::map<std::string, std::string> current_env(const Game& g) {
    std::map<std::string, std::string> cur;
    if (g.launcher == "heroic") {
        Json cfg = read_json_file(g.base + "/GamesConfig/" + g.id + ".json");
        for (const auto& e : cfg[g.id]["enviromentOptions"].array()) if (e.has("key")) cur[e["key"].str()] = e["value"].str();
    } else if (g.launcher == "lutris") {
        try {
            Yaml data = Yaml::parse(read_file(g.yml).value_or(""));
            const Yaml* s = data.get("system");
            const Yaml* env = s && s->is_map() ? s->get("env") : nullptr;
            if (env && env->is_map()) for (const auto& kv : env->map) cur[kv.first] = kv.second.str();
        } catch (const std::exception&) {}
    } else {
        for (const auto& t : split_ws(launchers::steam_get_launch_options(g.id)))
            if (t.find('=') != std::string::npos && !starts_with(t, "-")) cur[t.substr(0, t.find('='))] = t.substr(t.find('=') + 1);
    }
    return cur;
}

Json list_games() {
    Json out = Json::arr();
    Json ov = overrides();
    for (const auto& g : collect_games()) {
        auto [rec, note] = recommended(g.name);
        Json o = Json::obj();
        o.set("launcher", g.launcher);
        o.set("id", g.id);
        o.set("name", g.name);
        o.set("store", g.store);
        if (!g.base.empty()) o.set("base", g.base);
        if (!g.yml.empty()) o.set("yml", g.yml);
        o.set("recommended", rec ? Json(*rec) : Json());
        o.set("note", note);
        const Json& ovr = ov[norm_name(g.name)];
        o.set("override", ovr.is_number() ? Json(ovr.as_number()) : Json());
        auto cur = current_env(g);
        Json c = Json::obj();
        for (const char* k : kLimitKeys) if (cur.count(k)) c.set(k, cur[k]);
        o.set("current", c);
        out.push(o);
    }
    return out;
}

static bool write_game(const Json& g, const std::map<std::string, std::string>* env, bool dry) {
    std::string kind = g["launcher"].str(), id = g["id"].str();
    auto bak = [](const std::string& p) {
        if (path_exists(p)) { std::error_code ec; fs::copy_file(p, p + ".tuxthrottle-bak-" + std::to_string(std::time(nullptr)), fs::copy_options::overwrite_existing, ec); }
    };
    if (kind == "heroic") {
        std::string p = g["base"].str() + "/GamesConfig/" + id + ".json";
        Json data = read_json_file(p);
        if (data.object().empty()) { data = Json::obj(); data.set("version", "v0"); data.set("explicit", true); }
        Json cfg = data[id].is_object() ? data[id] : Json::obj();
        std::map<std::string, std::string> cur, nw;
        for (const auto& e : cfg["enviromentOptions"].array()) if (e.has("key")) cur[e["key"].str()] = e["value"].str();
        for (const auto& kv : cur) if (!is_limit_key(kv.first)) nw[kv.first] = kv.second;
        if (env) for (const auto& kv : *env) nw[kv.first] = kv.second;
        if (nw == cur) return false;
        Json arr = Json::arr();
        for (const auto& kv : nw) { Json e = Json::obj(); e.set("key", kv.first); e.set("value", kv.second); arr.push(e); }
        cfg.set("enviromentOptions", arr);
        data.set(id, cfg);
        if (!dry) { mkdir_p(dir_name(p)); bak(p); write_file(p, data.dump(2)); }
        return true;
    }
    if (kind == "lutris") {
        std::string p = g["yml"].str();
        Yaml data = Yaml::parse(read_file(p).value_or(""));
        if (data.is_null()) data = Yaml::make_map();
        Yaml* s = data.get("system");
        if (!s || !s->is_map()) { data.set("system", Yaml::make_map()); s = data.get("system"); }
        std::map<std::string, std::string> cur, nw;
        Yaml* e = s->get("env");
        if (e && e->is_map()) for (const auto& kv : e->map) cur[kv.first] = kv.second.str();
        for (const auto& kv : cur) if (!is_limit_key(kv.first)) nw[kv.first] = kv.second;
        if (env) for (const auto& kv : *env) nw[kv.first] = kv.second;
        if (nw == cur) return false;
        if (nw.empty()) s->erase("env");
        else {
            Yaml m = Yaml::make_map();
            for (const auto& kv : nw) m.set(kv.first, Yaml::make_scalar(kv.second, true));
            s->set("env", m);
        }
        if (!dry) { bak(p); write_file(p, data.dump()); }
        return true;
    }
    return launchers::steam_update_launch_options(id, [&](const std::string& old) { return steam_opts_with(old, env); }, dry);
}

static std::optional<std::string> blocked(const std::vector<Json>& games) {
    for (auto [kind, proc] : {std::pair<const char*, const char*>{"heroic", "heroic"}, {"lutris", "lutris"}, {"steam", "steam"}}) {
        bool any = false;
        for (const auto& g : games) any = any || g["launcher"].str() == kind;
        if (any && run_argv({"pgrep", "-x", proc}, 3000).code == 0) return std::string(proc) + " is running - close it first (it would overwrite the change)";
    }
    return std::nullopt;
}

static int do_apply(const Json* sel, std::optional<int> forced, const std::string& method, bool dry, bool remove) {
    Json games = list_games();
    std::vector<std::pair<Json, int>> todo;
    if (!sel) {
        for (const auto& g : games.array()) {
            int ov = g["override"].is_number() ? static_cast<int>(g["override"].as_number()) : 0;
            int rec = g["recommended"].is_number() ? static_cast<int>(g["recommended"].as_number()) : 0;
            if (remove || forced || ov || rec) todo.emplace_back(g, forced ? *forced : (ov ? ov : rec));
        }
    } else {
        for (const auto& s : sel->array())
            for (const auto& g : games.array())
                if (g["launcher"].str() == s["launcher"].str() && g["id"].str() == (s["id"].is_number() ? std::to_string(static_cast<long long>(s["id"].as_number())) : s["id"].str()))
                    todo.emplace_back(g, static_cast<int>(s["fps"].as_number()));
    }
    std::vector<Json> only;
    for (auto& t : todo) only.push_back(t.first);
    if (!dry) if (auto msg = blocked(only)) { std::puts(msg->c_str()); return 1; }
    Json ov = overrides();
    int n = 0;
    for (auto& [g, fps] : todo) {
        if (!remove && fps <= 0) continue;
        auto env = env_for(fps, method);
        if (write_game(g, remove ? nullptr : &env, dry)) {
            std::string what = remove ? "remove" : method + " " + std::to_string(fps);
            std::printf("%s%s: %s %s\n", dry ? "would " : "", what.c_str(), g["launcher"].str().c_str(), g["name"].str().c_str());
            ++n;
        }
        int rec = g["recommended"].is_number() ? static_cast<int>(g["recommended"].as_number()) : -1;
        if (sel && !remove && !dry && fps != rec) ov.set(norm_name(g["name"].str()), fps);
    }
    if (sel && !dry) save_overrides(ov);
    std::printf("%d game(s) %schanged\n", n, dry ? "would be " : "");
    return 0;
}

int main(const std::vector<std::string>& args_in) {
    std::vector<std::string> a = args_in;
    if (a.empty() || (a[0] != "list" && a[0] != "env" && a[0] != "apply" && a[0] != "remove" && a[0] != "set" && a[0] != "unset")) {
        std::puts("usage: fpslimit {list [--json]|env <fps> [--method M]|apply [--fps N] [--method M] [--sel B64] [--dry-run]|remove [--sel B64]|set '<name>' <fps>|unset '<name>'}");
        return 2;
    }
    auto opt = [&](const char* flag) -> std::optional<std::string> {
        auto it = std::find(a.begin(), a.end(), flag);
        if (it == a.end()) return std::nullopt;
        std::string v = it + 1 != a.end() ? *(it + 1) : "";
        a.erase(it, it + 1 != a.end() ? it + 2 : it + 1);
        return v;
    };
    std::string method = opt("--method").value_or("strangle");
    auto sel_b64 = opt("--sel");
    auto fps_opt = opt("--fps");
    bool as_json = std::find(a.begin(), a.end(), "--json") != a.end(), dry = std::find(a.begin(), a.end(), "--dry-run") != a.end();
    a.erase(std::remove_if(a.begin(), a.end(), [](const std::string& x) { return x == "--json" || x == "--dry-run"; }), a.end());
    if (method != "strangle" && method != "dxvk") { std::puts("--method must be strangle or dxvk"); return 2; }
    std::optional<Json> sel;
    if (sel_b64) {
        try { sel = Json::parse(base64_decode(*sel_b64)); } catch (const std::exception&) { std::puts("bad --sel argument"); return 2; }
    }
    const std::string cmd = a[0];
    if (cmd == "list") {
        Json games = list_games();
        if (as_json) { std::puts(games.dump().c_str()); return 0; }
        for (const auto& g : games.array()) {
            int ov = g["override"].is_number() ? static_cast<int>(g["override"].as_number()) : 0;
            int rec = g["recommended"].is_number() ? static_cast<int>(g["recommended"].as_number()) : 0;
            int sug = ov ? ov : rec;
            std::string now = g["current"].object().empty() ? "no limit" : g["current"].dump();
            std::printf("%s\t%s\t%s\tnow %s\n", g["launcher"].str().c_str(), g["name"].str().c_str(), sug ? ("suggest " + std::to_string(sug)).c_str() : "no advice", now.c_str());
        }
        return 0;
    }
    if (cmd == "env" && a.size() > 1) { std::puts(env_string(std::atoi(a[1].c_str()), method).c_str()); return 0; }
    if (cmd == "apply") return do_apply(sel ? &*sel : nullptr, fps_opt ? std::optional<int>(std::atoi(fps_opt->c_str())) : std::nullopt, method, dry, false);
    if (cmd == "remove") return do_apply(sel ? &*sel : nullptr, std::nullopt, method, dry, true);
    if (a.size() < 2) return 2;
    Json ov = overrides();
    if (cmd == "set" && a.size() > 2) {
        ov.set(norm_name(a[1]), std::atoi(a[2].c_str()));
        std::printf("override saved: %s = %s FPS\n", a[1].c_str(), a[2].c_str());
    } else {
        ov.erase(norm_name(a[1]));
        std::printf("override removed: %s\n", a[1].c_str());
    }
    save_overrides(ov);
    return 0;
}

}  // namespace tt::fpslimit

namespace tt::protondb {

static const double kCacheTtl = 7 * 24 * 3600;

static std::string cache_path(const std::string& appid) { return env_or("HOME", "/root") + "/.cache/tuxthrottle/protondb/" + appid + ".json"; }

static void write_cache(const std::string& appid, Json data) {
    data.set("_fetched", static_cast<double>(std::time(nullptr)));
    mkdir_p(dir_name(cache_path(appid)));
    write_file(cache_path(appid), data.dump());
}

std::string label(const Json& data) {
    if (!data.is_object() || data["tier"].str().empty()) return "";
    static const std::map<std::string, std::string> labels = {{"platinum", "Platinum"}, {"gold", "Gold"}, {"silver", "Silver"}, {"bronze", "Bronze"}, {"borked", "Borked"}, {"pending", "Pending"}};
    std::string t = data["tier"].str();
    auto it = labels.find(t);
    std::string shown = it != labels.end() ? it->second : (t.empty() ? t : std::string(1, static_cast<char>(std::toupper(static_cast<unsigned char>(t[0])))) + t.substr(1));
    return "ProtonDB: " + shown;
}

std::optional<Json> lookup(const std::string& appid, bool use_cache) {
    if (appid.empty() || !std::all_of(appid.begin(), appid.end(), [](char c) { return c >= '0' && c <= '9'; })) return std::nullopt;
    if (use_cache) {
        Json c = read_json_file(cache_path(appid));
        if (!c.object().empty() && static_cast<double>(std::time(nullptr)) - c["_fetched"].as_number() <= kCacheTtl) {
            if (c["tier"].is_null() && c.object().size() <= 2) return std::nullopt;   // negative cache entry
            return c;
        }
    }
    std::string url = "https://www.protondb.com/api/v1/reports/summaries/" + appid + ".json";
    std::string exe = which("curl");
    if (exe.empty()) return std::nullopt;
    auto r = run_argv_stdout({exe, "-fsSL", "--max-time", "5", "-A", "TuxThrottle", url}, 7000);
    Json data;
    try { data = Json::parse(r.out); } catch (const std::exception&) { r.code = 1; }
    if (r.code != 0 || !data.is_object()) {
        Json neg = Json::obj();
        neg.set("tier", Json());
        write_cache(appid, neg);
        return std::nullopt;
    }
    write_cache(appid, data);
    return data;
}

int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: protondb <appid> [--json] [--no-cache]"); return 2; }
    bool as_json = std::find(a.begin(), a.end(), "--json") != a.end(), no_cache = std::find(a.begin(), a.end(), "--no-cache") != a.end();
    auto d = lookup(a[0], !no_cache);
    if (as_json) std::puts(d ? d->dump().c_str() : "null");
    else std::puts(d && !label(*d).empty() ? label(*d).c_str() : "no ProtonDB data");
    return d ? 0 : 1;
}

}  // namespace tt::protondb

namespace tt::fixlog {

static std::string log_path(const std::string& user) {
    std::string home = user.empty() ? env_or("HOME", "/root") : home_of(user);
    return home + "/.local/share/tuxthrottle/fixlog.jsonl";
}

void log_event(const std::string& source, const std::string& message, const std::string& level, const std::string& user) {
    std::string p = log_path(user);
    mkdir_p(dir_name(p));
    Json e = Json::obj();
    e.set("ts", static_cast<double>(std::time(nullptr)));
    e.set("source", source);
    e.set("level", level);
    e.set("message", message);
    auto lines = split_lines(read_file(p).value_or(""));
    lines.push_back(e.dump());
    if (lines.size() > 500) lines.erase(lines.begin(), lines.end() - 500);
    write_file(p, join(lines, "\n") + "\n");
    if (!user.empty()) chown_to_user(p, user);
}

Json read_recent(int n, const std::string& user) {
    auto lines = split_lines(read_file(log_path(user)).value_or(""));
    Json out = Json::arr();
    size_t start = lines.size() > static_cast<size_t>(n) ? lines.size() - static_cast<size_t>(n) : 0;
    std::vector<Json> items;
    for (size_t i = start; i < lines.size(); ++i) {
        try { items.push_back(Json::parse(lines[i])); } catch (const std::exception&) {}
    }
    std::reverse(items.begin(), items.end());
    for (auto& j : items) out.push(j);
    return out;
}

int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: fixlog {log <source> <message> [--level L] [--user U]|show [-n N] [--json] [--user U]}"); return 2; }
    auto opt = [&](const char* f, const std::string& def) {
        for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1];
        return def;
    };
    std::string user = opt("--user", "");
    if (a[0] == "log" && a.size() >= 3) { log_event(a[1], a[2], opt("--level", "info"), user); return 0; }
    Json entries = read_recent(std::atoi(opt("-n", "50").c_str()), user);
    if (std::find(a.begin(), a.end(), "--json") != a.end()) std::puts(entries.dump(2).c_str());
    else if (entries.array().empty()) std::puts("no fixes logged yet");
    else
        for (const auto& e : entries.array()) {
            std::time_t t = static_cast<std::time_t>(e["ts"].as_number());
            std::tm tm;
            localtime_r(&t, &tm);
            char buf[32];
            std::strftime(buf, sizeof buf, "%Y-%m-%d %H:%M", &tm);
            std::printf("[%s] %-5s %-20s %s\n", buf, e["level"].str().c_str(), e["source"].str().c_str(), e["message"].str().c_str());
        }
    return 0;
}

}  // namespace tt::fixlog
