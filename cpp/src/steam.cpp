#include "steam.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <regex>
#include <set>
#include <system_error>

#include "userenv.hpp"
#include "util.hpp"

namespace tt::steam {

static const char* kRoots[] = {"~/.local/share/Steam", "~/.steam/steam", "~/.steam/root"};

std::string expand_home(const std::string& p) {
    if (!p.empty() && p[0] == '~') return env_or("HOME", "/root") + p.substr(1);
    return p;
}

static fs::path canon(const fs::path& p) {
    std::error_code ec;
    auto r = fs::weakly_canonical(p, ec);
    return ec ? p : r;
}

std::optional<fs::path> steam_root_opt() {
    for (const char* c : kRoots) {
        fs::path p = expand_home(c);
        std::error_code ec;
        if (fs::is_regular_file(p / "steamapps" / "libraryfolders.vdf", ec)) return canon(p);
    }
    return std::nullopt;
}

static fs::path steam_root_or_die() {
    auto r = steam_root_opt();
    if (!r) {
        std::fprintf(stderr, "no Steam install found (looked in %s)\n", "~/.local/share/Steam, ~/.steam/steam, ~/.steam/root");
        std::exit(1);
    }
    return *r;
}

std::vector<std::string> parse_library_paths(const std::string& vdf) {
    std::vector<std::string> out;
    static const std::regex re(R"re("path"\s*"([^"]+)")re");
    for (auto it = std::sregex_iterator(vdf.begin(), vdf.end(), re); it != std::sregex_iterator(); ++it) out.push_back((*it)[1]);
    return out;
}

std::string parse_appmanifest_name(const std::string& acf) {
    static const std::regex re(R"re("name"\s*"([^"]+)")re");
    std::smatch m;
    return std::regex_search(acf, m, re) ? m[1].str() : "";
}

std::vector<fs::path> all_libraries(const fs::path& root) {
    std::string vdf = read_file((root / "steamapps" / "libraryfolders.vdf").string()).value_or("");
    std::vector<fs::path> libs;
    for (const auto& p : parse_library_paths(vdf)) libs.emplace_back(p);
    if (std::find(libs.begin(), libs.end(), root) == libs.end()) libs.insert(libs.begin(), root);
    std::vector<fs::path> out;
    std::error_code ec;
    for (const auto& p : libs) if (fs::is_directory(p, ec)) out.push_back(p);
    return out;
}

std::optional<fs::path> library_for_appid(const fs::path& root, const std::string& appid) {
    std::string vdf = read_file((root / "steamapps" / "libraryfolders.vdf").string()).value_or("");
    // find the last "path" block whose body lists this appid
    std::optional<std::string> best;
    static const std::regex path_re(R"re("path"\s*"([^"]+)")re");
    std::vector<std::pair<size_t, std::string>> starts;
    for (auto it = std::sregex_iterator(vdf.begin(), vdf.end(), path_re); it != std::sregex_iterator(); ++it)
        starts.emplace_back(static_cast<size_t>(it->position()), (*it)[1].str());
    for (size_t i = 0; i < starts.size(); ++i) {
        size_t end = i + 1 < starts.size() ? starts[i + 1].first : vdf.size();
        std::string body = vdf.substr(starts[i].first, end - starts[i].first);
        std::regex has("\"" + appid + "\"\\s");
        if (std::regex_search(body, has)) best = starts[i].second;
    }
    if (best) return fs::path(*best);
    struct Cand { long long size; bool has_compat; fs::path lib; };
    std::vector<Cand> cands;
    std::error_code ec;
    for (const auto& lib : all_libraries(root)) {
        fs::path acf = lib / "steamapps" / ("appmanifest_" + appid + ".acf");
        bool has_compat = fs::exists(lib / "steamapps" / "compatdata" / appid, ec);
        if (fs::is_regular_file(acf, ec)) {
            long long size = 0;
            static const std::regex sz(R"re("SizeOnDisk"\s*"(\d+)")re");
            std::string t = read_file(acf.string()).value_or("");
            std::smatch m;
            if (std::regex_search(t, m, sz)) size = std::stoll(m[1]);
            cands.push_back({size, has_compat, lib});
        } else if (has_compat) {
            cands.push_back({0, true, lib});
        }
    }
    if (cands.empty()) return std::nullopt;
    std::stable_sort(cands.begin(), cands.end(), [](const Cand& a, const Cand& b) { return std::tie(a.size, a.has_compat) > std::tie(b.size, b.has_compat); });
    return cands[0].lib;
}

std::string appid_name(const fs::path& root, const std::string& appid) {
    for (const auto& lib : all_libraries(root)) {
        std::string n = parse_appmanifest_name(read_file((lib / "steamapps" / ("appmanifest_" + appid + ".acf")).string()).value_or(""));
        if (!n.empty()) return n;
    }
    return "";
}

bool colon_ok(const fs::path& dir) {
    fs::path probe = dir / "tuxthrottle_colon_probe:tmp";
    std::FILE* f = std::fopen(probe.c_str(), "w");
    if (!f) return false;
    std::fclose(f);
    ::unlink(probe.c_str());
    return true;
}

std::pair<std::string, fs::path> classify(const fs::path& lib, const std::string& appid) {
    fs::path src = lib / "steamapps" / "compatdata" / appid;
    std::error_code ec;
    if (fs::is_symlink(src, ec)) return {"symlink", src};
    fs::path probe = fs::is_directory(src, ec) ? src : lib / "steamapps" / "compatdata";
    if (!fs::exists(probe, ec)) return {"absent", src};
    return {colon_ok(probe) ? "ok" : "needs-fix", src};
}

bool steam_running() { return run_argv({"pgrep", "-x", "steam"}, 3000).code == 0; }

std::vector<Prefix> all_prefixes(const fs::path& root) {
    std::set<std::string> seen;
    std::vector<Prefix> rows;
    std::error_code ec;
    for (const auto& lib : all_libraries(root)) {
        fs::path cd = lib / "steamapps" / "compatdata";
        if (!fs::is_directory(cd, ec)) continue;
        std::vector<fs::path> entries;
        for (const auto& e : fs::directory_iterator(cd, ec)) entries.push_back(e.path());
        std::sort(entries.begin(), entries.end(), [](const fs::path& a, const fs::path& b) { return a.filename() < b.filename(); });
        for (const auto& e : entries) {
            std::string n = e.filename().string();
            if (n.empty() || !std::all_of(n.begin(), n.end(), [](char c) { return c >= '0' && c <= '9'; }) || seen.count(n) || n == "0") continue;
            seen.insert(n);
            std::string name = appid_name(root, n);
            rows.push_back({n, name.empty() ? "?" : name, classify(lib, n).first, lib});
        }
    }
    return rows;
}

// ---------------------------------------------------------------- helpers
static bool is_digits(const std::string& s) { return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; }); }

static void rm_rf(const fs::path& p) {
    std::error_code ec;
    fs::remove_all(p, ec);
}

static bool copy_tree(const fs::path& src, const fs::path& dst) {
    std::error_code ec;
    fs::create_directories(dst, ec);
    fs::copy(src, dst, fs::copy_options::recursive | fs::copy_options::overwrite_existing | fs::copy_options::copy_symlinks, ec);
    return !ec;
}

static bool has_files(const fs::path& d) {
    std::error_code ec;
    for (auto it = fs::recursive_directory_iterator(d, fs::directory_options::skip_permission_denied, ec); it != fs::recursive_directory_iterator(); it.increment(ec)) {
        if (ec) break;
        std::error_code e2;
        if (it->is_regular_file(e2)) return true;
    }
    return false;
}

static dev_t dev_of(const fs::path& p) {
    struct stat st;
    return ::stat(p.c_str(), &st) == 0 ? st.st_dev : static_cast<dev_t>(-1);
}

static fs::path mount_point(const fs::path& path) {
    fs::path p = canon(path);
    dev_t d = dev_of(p);
    while (p != p.parent_path() && dev_of(p.parent_path()) == d) p = p.parent_path();
    return p;
}

static void must_not_be_root() {
    if (is_root() && env_or("TUXTHROTTLE_ALLOW_ROOT").empty()) {
        std::fputs("run this as your normal user, not root (it edits ~/.local/share/Steam)\n", stderr);
        std::exit(1);
    }
}

[[noreturn]] static void die(const std::string& msg) {
    std::fprintf(stderr, "%s\n", msg.c_str());
    std::exit(1);
}

// ---------------------------------------------------------------- prefix relocate
static const char* kKnownFolders[] = {"Documents", "My Documents", "Saved Games", "Desktop", "AppData/Local", "AppData/LocalLow", "AppData/Roaming"};
static const char* kLooseNames[] = {"Documents", "My Documents", "My Games", "Saved Games"};

static int do_scan(const fs::path& root) {
    auto rows = all_prefixes(root);
    if (rows.empty()) { std::puts("no Proton prefixes found."); return 0; }
    size_t w = 0;
    for (const auto& r : rows) w = std::max(w, r.appid.size());
    int need = 0;
    for (const auto& r : rows) {
        std::string flag = r.status == "needs-fix" ? "NEEDS FIX" : r.status == "symlink" ? "relocated" : r.status == "ok" ? "ok" : "not built";
        if (r.status == "needs-fix") ++need;
        std::printf("  %-*s  %-10s  %s  %s\n", static_cast<int>(w), r.appid.c_str(), flag.c_str(), utf8_pad(utf8_trunc(r.name, 44), 44).c_str(), r.lib.c_str());
    }
    std::puts("");
    if (need) std::printf("%d prefix(es) NEED FIX - run:  tuxthrottlectl prefix <appid>   (or --all)   (Steam closed)\n", need);
    else std::puts("all prefixes are fine.");
    return 0;
}

static bool relocate_one(const fs::path& root, const std::string& appid, std::optional<fs::path> lib_in, std::string& err) {
    auto lib = lib_in ? lib_in : library_for_appid(root, appid);
    if (!lib) { err = "appid " + appid + " not found in any Steam library"; return false; }
    auto [status, src] = classify(*lib, appid);
    fs::path dst = root / "steamapps" / "compatdata" / appid;
    if (status != "needs-fix") { err = appid + ": status is '" + status + "', not moving"; return false; }
    if (canon(src) == canon(dst)) {
        err = appid + ": its library (" + lib->string() + ") is the bad filesystem AND the main Steam library - move the game to a Linux drive instead";
        return false;
    }
    std::error_code ec;
    if (!fs::exists(src, ec)) { err = appid + ": " + src.string() + " vanished"; return false; }
    if (fs::exists(dst, ec) || fs::is_symlink(dst, ec)) { err = appid + ": " + dst.string() + " already exists - clear it first"; return false; }
    std::string name = appid_name(root, appid);
    if (name.empty()) name = appid;
    std::printf("moving prefix for %s:\n  %s\n    ->  %s\n", name.c_str(), src.c_str(), dst.c_str());
    fs::create_directories(dst.parent_path(), ec);
    if (!copy_tree(src, dst)) { err = "copy failed for " + appid; rm_rf(dst); return false; }
    rm_rf(src);
    fs::create_directory_symlink(dst, src, ec);
    if (ec) { err = "symlink failed: " + ec.message(); return false; }
    std::printf("  done - %s is now a symlink to %s\n", src.c_str(), dst.c_str());
    return true;
}

static int do_relevant(const fs::path& root, const std::string& appid) {
    auto lib = library_for_appid(root, appid);
    if (!lib) return 1;
    auto [status, src] = classify(*lib, appid);
    (void)src;
    if (status == "symlink" || status == "needs-fix") return 0;
    if (status == "absent") return colon_ok(*lib) ? 1 : 0;
    return 1;
}

static int do_one(const fs::path& root, const std::string& appid, bool check) {
    auto lib = library_for_appid(root, appid);
    if (!lib) { std::printf("appid %s not found in any Steam library - nothing to do (is it installed? launched once?)\n", appid.c_str()); return 0; }
    auto [status, src] = classify(*lib, appid);
    std::error_code ec;
    if (status == "symlink") { std::printf("OK - prefix already relocated: %s -> %s\n", src.c_str(), fs::read_symlink(src, ec).c_str()); return 0; }
    if (status == "absent") { std::printf("OK - no prefix on disk yet (%s absent); it'll build in place.\n", src.c_str()); return 0; }
    if (status == "ok") { std::printf("OK - %s is on a filesystem that allows ':' in names; no relocation needed.\n", src.c_str()); return 0; }
    if (check) { std::printf("NEEDS FIX - %s is on a filesystem that rejects ':' in filenames; Proton can't build drive-letter links there.\n", src.c_str()); return 1; }
    if (steam_running()) die("Steam is running - close Steam (and the game) first, then retry.");
    std::string err;
    if (!relocate_one(root, appid, lib, err)) die(err);
    std::puts("re-launch the game from Steam; the prefix will finish building on the Linux drive.");
    return 0;
}

static int do_all(const fs::path& root) {
    std::vector<Prefix> targets;
    for (const auto& p : all_prefixes(root)) if (p.status == "needs-fix") targets.push_back(p);
    if (targets.empty()) { std::puts("no prefixes need relocating - nothing to do."); return 0; }
    std::printf("%zu prefix(es) to relocate:\n", targets.size());
    for (const auto& t : targets) std::printf("  %s  %s\n", t.appid.c_str(), t.name.c_str());
    std::puts("");
    if (steam_running()) die("Steam is running - close Steam (and every game) first, then retry.");
    int ok = 0, fails = 0;
    for (const auto& t : targets) {
        std::string err;
        if (relocate_one(root, t.appid, t.lib, err)) ++ok;
        else { std::printf("  SKIP %s: %s\n", t.appid.c_str(), err.c_str()); ++fails; }
    }
    std::printf("\nrelocated %d/%zu.%s\n", ok, targets.size(), fails ? ("  " + std::to_string(fails) + " skipped.").c_str() : "");
    std::puts("re-launch each game from Steam; prefixes rebuild on the Linux drive.");
    return fails ? 1 : 0;
}

// ---------------------------------------------------------------- Lutris Wine prefixes
// A Wine/Proton prefix needs symlinks (pfx -> ., Fonts/*, dosdevices/c:). NTFS/exFAT hosts often refuse them,
// so umu-run / Proton die before the game starts. The fix keeps the big game folders where they are and builds
// the prefix on a Linux drive, linking those folders back in.
bool symlink_ok(const fs::path& dir) {
    fs::path a = dir / "tuxthrottle_symlink_probe_a";
    fs::path b = dir / "tuxthrottle_symlink_probe_b";
    std::error_code ec;
    fs::remove(a, ec);
    fs::remove(b, ec);
    bool ok = ::symlink(".", a.c_str()) == 0 && ::symlink("/", b.c_str()) == 0;
    fs::remove(a, ec);
    fs::remove(b, ec);
    return ok && colon_ok(dir);
}

std::string lutris_yml_prefix(const std::string& yml) {
    bool in_game = false;
    for (const auto& line : split_lines(yml)) {
        if (line.empty() || line[0] == '#') continue;
        if (line[0] != ' ' && line[0] != '\t') { in_game = trim(line) == "game:"; continue; }
        if (!in_game) continue;
        std::string t = trim(line);
        if (!starts_with(t, "prefix:")) continue;
        std::string v = trim(t.substr(7));
        if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front()) v = v.substr(1, v.size() - 2);
        return v;
    }
    return "";
}

std::string lutris_yml_retarget(const std::string& yml, const std::string& from, const std::string& to) {
    return replace_all(yml, from, to);
}

struct LutrisPrefix { std::string slug, status; fs::path prefix, yml; };

static fs::path lutris_games_dir() { return fs::path(env_or("HOME", "/root")) / ".local/share/lutris/games"; }

static std::vector<LutrisPrefix> lutris_prefixes(const fs::path& games = lutris_games_dir()) {
    std::vector<LutrisPrefix> rows;
    std::set<std::string> seen;
    std::error_code ec;
    if (!fs::is_directory(games, ec)) return rows;
    std::vector<fs::path> ymls;
    for (const auto& e : fs::directory_iterator(games, ec)) if (e.path().extension() == ".yml") ymls.push_back(e.path());
    std::sort(ymls.begin(), ymls.end());
    for (const auto& y : ymls) {
        fs::path pfx = lutris_yml_prefix(read_file(y.string()).value_or(""));
        if (pfx.empty() || !seen.insert(pfx.string()).second) continue;
        std::string st = !fs::is_directory(pfx, ec) ? "absent" : !fs::is_directory(pfx / "drive_c", ec) ? "no-prefix" : symlink_ok(pfx) ? "ok" : "needs-fix";
        rows.push_back({y.stem().string(), st, pfx, y});
    }
    return rows;
}

static bool lutris_running() { return run_argv({"pgrep", "-x", "lutris"}, 3000).code == 0; }

static const long long kBigDir = 1LL << 30;       // game folders at least this big stay where they are
static const long long kMaxCopy = 20LL << 30;     // refuse to copy more than this into the new prefix

// Bytes under `d`, stopping once `cap` is passed.
static long long dir_bytes(const fs::path& d, long long cap) {
    long long sz = 0;
    std::error_code ec;
    for (auto it = fs::recursive_directory_iterator(d, fs::directory_options::skip_permission_denied, ec); it != fs::recursive_directory_iterator(); it.increment(ec)) {
        if (ec) break;
        std::error_code e2;
        if (it->is_regular_file(e2)) sz += static_cast<long long>(it->file_size(e2));
        if (sz > cap) break;
    }
    return sz;
}

static long long bytes_filtered(const fs::path& d, const std::set<fs::path>& skip, long long cap) {
    long long sz = 0;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(d, ec)) {
        if (skip.count(e.path()) || e.is_symlink(ec)) continue;
        std::error_code e2;
        if (e.is_directory(e2)) sz += bytes_filtered(e.path(), skip, cap - sz);
        else if (e.is_regular_file(e2)) sz += static_cast<long long>(e.file_size(e2));
        if (sz > cap) break;
    }
    return sz;
}

static bool copy_filtered(const fs::path& src, const fs::path& dst, const std::set<fs::path>& skip) {
    std::error_code ec;
    fs::create_directories(dst, ec);
    for (const auto& e : fs::directory_iterator(src, ec)) {
        if (skip.count(e.path())) continue;
        fs::path to = dst / e.path().filename();
        std::error_code e2;
        if (e.is_symlink(e2)) fs::copy_symlink(e.path(), to, e2);
        else if (e.is_directory(e2)) { if (!copy_filtered(e.path(), to, skip)) return false; }
        else fs::copy_file(e.path(), to, fs::copy_options::overwrite_existing, e2);
        if (e2) return false;
    }
    return !ec;
}

static bool lutris_relocate(const LutrisPrefix& lp, const std::vector<LutrisPrefix>& all, std::string& err) {
    fs::path dst = fs::path(env_or("HOME", "/root")) / "Games" / lp.prefix.filename();
    std::error_code ec;
    if (fs::exists(dst, ec)) { err = lp.slug + ": " + dst.string() + " already exists - move it away first"; return false; }
    std::set<fs::path> skip{lp.prefix / "pfx", lp.prefix / "pfx.lock", lp.prefix / "GLCache"};
    std::vector<fs::path> kept;
    for (const char* pf : {"Program Files", "Program Files (x86)"}) {
        fs::path d = lp.prefix / "drive_c" / pf;
        if (!fs::is_directory(d, ec)) continue;
        for (const auto& e : fs::directory_iterator(d, ec)) {
            if (e.is_directory(ec) && !e.is_symlink(ec) && dir_bytes(e.path(), kBigDir) >= kBigDir) { skip.insert(e.path()); kept.push_back(e.path()); }
        }
    }
    if (bytes_filtered(lp.prefix, skip, kMaxCopy) > kMaxCopy) { err = lp.slug + ": more than 20 GiB outside the game folders - not copying"; return false; }
    std::printf("moving Lutris prefix %s:\n  %s\n    ->  %s\n", lp.slug.c_str(), lp.prefix.c_str(), dst.c_str());
    if (!copy_filtered(lp.prefix, dst, skip)) { err = "copy failed for " + lp.slug; rm_rf(dst); return false; }
    for (const auto& k : kept) {
        fs::path to = dst / fs::relative(k, lp.prefix);
        fs::create_directories(to.parent_path(), ec);
        fs::create_directory_symlink(k, to, ec);
        if (ec) { err = "link failed for " + k.string() + ": " + ec.message(); rm_rf(dst); return false; }
        std::printf("  kept on the original drive and linked: %s\n", k.filename().c_str());
    }
    for (const auto& o : all) {
        if (o.prefix != lp.prefix) continue;
        std::string text = read_file(o.yml.string()).value_or("");
        fs::copy_file(o.yml, o.yml.string() + ".tuxthrottle-bak-" + std::to_string(static_cast<long long>(::time(nullptr))), ec);
        write_file(o.yml.string(), lutris_yml_retarget(text, lp.prefix.string(), dst.string()));
    }
    std::printf("  done - the old prefix at %s is untouched; delete it once the game works.\n", lp.prefix.c_str());
    return true;
}

static int lutris_main(const std::vector<std::string>& a) {
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    auto rows = lutris_prefixes();
    if (has("--lutris-scan")) {
        if (rows.empty()) { std::puts("no Lutris games with a Wine prefix found."); return 0; }
        int need = 0;
        for (const auto& r : rows) {
            if (r.status == "needs-fix") ++need;
            std::printf("  %-10s  %-28s  %s\n", r.status == "needs-fix" ? "NEEDS FIX" : r.status.c_str(), utf8_pad(utf8_trunc(r.slug, 28), 28).c_str(), r.prefix.c_str());
        }
        if (need) std::printf("\n%d prefix(es) NEED FIX (symlinks refused) - run:  tuxthrottlectl prefix --lutris-fix <slug>   (or --lutris-all)   (Lutris closed)\n", need);
        else std::puts("\nall Lutris prefixes are fine.");
        return 0;
    }
    std::vector<LutrisPrefix> targets;
    if (has("--lutris-all")) { for (const auto& r : rows) if (r.status == "needs-fix") targets.push_back(r); }
    else {
        std::string slug;
        for (const auto& x : a) if (!starts_with(x, "-")) slug = x;
        if (slug.empty()) die("usage: tuxthrottlectl prefix --lutris-scan | --lutris-fix <slug> | --lutris-all");
        for (const auto& r : rows) if (r.slug == slug) targets.push_back(r);
        if (targets.empty()) die("no Lutris game '" + slug + "' with a prefix (see --lutris-scan)");
        if (targets[0].status != "needs-fix") { std::printf("OK - %s is %s; nothing to move.\n", slug.c_str(), targets[0].status.c_str()); return 0; }
    }
    if (targets.empty()) { std::puts("no Lutris prefixes need relocating - nothing to do."); return 0; }
    if (lutris_running()) die("Lutris is running - close Lutris (and the game) first, then retry.");
    int ok = 0;
    for (const auto& t : targets) {
        std::string err;
        if (lutris_relocate(t, rows, err)) ++ok;
        else std::printf("  SKIP %s\n", err.c_str());
    }
    std::printf("\nrelocated %d/%zu. Reopen Lutris and launch the game.\n", ok, targets.size());
    return ok == static_cast<int>(targets.size()) ? 0 : 1;
}

struct Stray { std::string kf; fs::path link, target; };

static std::vector<Stray> stray_saves(const fs::path& root, const std::string& appid) {
    std::vector<Stray> out;
    auto lib = library_for_appid(root, appid);
    if (!lib) return out;
    fs::path pfx = canon(*lib / "steamapps" / "compatdata" / appid / "pfx");
    fs::path su = pfx / "drive_c" / "users" / "steamuser";
    std::error_code ec;
    if (!fs::is_directory(su, ec)) return out;
    dev_t home_dev = dev_of(pfx);
    for (const char* kf : kKnownFolders) {
        fs::path link = su / kf;
        if (!fs::is_symlink(link, ec)) continue;
        fs::path target = canon(link);
        if (!fs::exists(target, ec) || !fs::is_directory(target, ec)) continue;
        bool inside = false;
        for (fs::path p = target; p != p.parent_path(); p = p.parent_path()) if (p.parent_path() == su) { inside = true; break; }
        if (dev_of(target) != home_dev && !inside) out.push_back({kf, link, target});
    }
    return out;
}

static int pull_saves_one(const fs::path& root, const std::string& appid) {
    auto strays = stray_saves(root, appid);
    std::string name = appid_name(root, appid);
    if (name.empty()) name = appid;
    if (strays.empty()) { std::printf("%s (%s): no stray save folders - nothing to do.\n", name.c_str(), appid.c_str()); return 0; }
    int moved = 0;
    std::error_code ec;
    for (const auto& s : strays) {
        fs::path staging = s.link; staging += ".tuxthrottle-pull";
        std::printf("%s: %s  <-  %s\n", name.c_str(), s.kf.c_str(), s.target.c_str());
        if (fs::exists(staging, ec)) { std::printf("  SKIP - %s already exists\n", staging.c_str()); continue; }
        if (!copy_tree(s.target, staging)) { std::puts("  FAILED: copy error"); rm_rf(staging); continue; }
        fs::remove(s.link, ec);
        fs::rename(staging, s.link, ec);
        if (ec) { std::printf("  FAILED: %s\n", ec.message().c_str()); rm_rf(staging); continue; }
        std::printf("  pulled into the prefix; old copy left at %s\n", s.target.c_str());
        ++moved;
    }
    if (moved) std::printf("%s: %d folder(s) pulled in. Verify saves in-game, then the old off-drive copies can be deleted.\n", name.c_str(), moved);
    return 0;
}

static std::vector<fs::path> loose_save_dirs(const fs::path& root) {
    fs::path home = canon(env_or("HOME", "/root"));
    std::set<fs::path> seen;
    std::vector<fs::path> out;
    std::error_code ec;
    for (const auto& lib : all_libraries(root)) {
        fs::path mp = mount_point(lib);
        bool home_rel = mp == home;
        for (fs::path p = mp; p != p.parent_path(); p = p.parent_path()) if (p == home) home_rel = true;
        for (fs::path p = home; p != p.parent_path(); p = p.parent_path()) if (p == mp) home_rel = true;
        if (seen.count(mp) || mp == "/" || home_rel) { seen.insert(mp); continue; }
        seen.insert(mp);
        for (const char* nm : kLooseNames) {
            fs::path d = mp / nm;
            if (fs::is_directory(d, ec) && !fs::is_symlink(d, ec) && has_files(d)) out.push_back(d);
        }
    }
    return out;
}

static int do_saves_scan(const fs::path& root) {
    int hits = 0;
    for (const auto& p : all_prefixes(root)) {
        auto strays = stray_saves(root, p.appid);
        if (strays.empty()) continue;
        ++hits;
        std::printf("  %s  %s\n", p.appid.c_str(), p.name.c_str());
        for (const auto& s : strays) std::printf("      %-18s -> %s\n", s.kf.c_str(), s.target.c_str());
    }
    if (hits) std::printf("\n%d game(s) have a save folder symlinked onto another drive - run:  tuxthrottlectl prefix --saves <appid>  (or --saves-all)\n", hits);
    auto loose = loose_save_dirs(root);
    if (!loose.empty()) {
        std::puts("\nLoose save folders sitting on a Steam drive (a prefix used to redirect Documents here; the redirect is gone but the files remain):");
        std::error_code ec;
        for (const auto& d : loose) {
            std::printf("  %s\n", d.c_str());
            std::vector<std::string> subs;
            for (const auto& e : fs::directory_iterator(d, ec)) if (e.is_directory(ec)) subs.push_back(e.path().filename().string());
            std::sort(subs.begin(), subs.end());
            for (const auto& s : subs) std::printf("      %s\n", s.c_str());
        }
        std::puts("  These can't be auto-matched to a game. Launch the game, note which subfolder is its saves, then run:\n"
                  "      tuxthrottlectl prefix --saves-import <appid>\n  which copies that drive's loose Documents into <appid>'s prefix.");
    }
    if (!hits && loose.empty()) std::puts("no save folders found on another drive.");
    return 0;
}

static int import_loose_one(const fs::path& root, const std::string& appid) {
    auto lib = library_for_appid(root, appid);
    if (!lib) die("appid " + appid + " not found in any Steam library");
    std::string name = appid_name(root, appid);
    if (name.empty()) name = appid;
    fs::path docs = *lib / "steamapps" / "compatdata" / appid / "pfx" / "drive_c" / "users" / "steamuser" / "Documents";
    std::error_code ec;
    if (!fs::is_directory(docs, ec)) die(name + ": no prefix Documents folder at " + docs.string() + " - launch the game once first");
    fs::path mp = mount_point(*lib);
    std::vector<fs::path> srcs;
    for (const char* nm : kLooseNames) {
        fs::path d = mp / nm;
        if (fs::is_directory(d, ec) && !fs::is_symlink(d, ec) && has_files(d)) srcs.push_back(d);
    }
    if (srcs.empty()) { std::printf("%s: no loose save folders on %s - nothing to import.\n", name.c_str(), mp.c_str()); return 0; }
    int total = 0;
    for (const auto& src : srcs) {
        std::printf("%s: importing %s  ->  %s\n", name.c_str(), src.c_str(), docs.c_str());
        for (const auto& item : fs::directory_iterator(src, ec)) {
            fs::path dst = docs / item.path().filename();
            bool ok;
            if (item.is_directory(ec)) ok = copy_tree(item.path(), dst);
            else if (!fs::exists(dst, ec)) { fs::copy_file(item.path(), dst, ec); ok = !ec; }
            else ok = true;
            if (ok) { ++total; std::printf("    + %s\n", item.path().filename().c_str()); }
            else std::printf("    ! %s: copy failed\n", item.path().filename().c_str());
        }
    }
    std::printf("%s: imported %d item(s); the originals on %s are untouched. Verify saves in-game before deleting them.\n", name.c_str(), total, mp.c_str());
    return 0;
}

static int do_saves_all(const fs::path& root) {
    std::vector<std::string> targets;
    for (const auto& p : all_prefixes(root)) if (!stray_saves(root, p.appid).empty()) targets.push_back(p.appid);
    if (targets.empty()) { std::puts("no save folders on another drive - nothing to do."); return 0; }
    if (steam_running()) die("Steam is running - close Steam (and every game) first, then retry.");
    for (const auto& a : targets) pull_saves_one(root, a);
    std::printf("\ndone - %zu game(s) processed.\n", targets.size());
    return 0;
}

int prefix_main(const std::vector<std::string>& a) {
    must_not_be_root();
    for (const auto& x : a) if (x == "--lutris-scan" || x == "--lutris-fix" || x == "--lutris-all") return lutris_main(a);
    fs::path root = steam_root_or_die();
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    if (has("--scan")) return do_scan(root);
    if (has("--all")) return do_all(root);
    if (has("--saves-scan")) return do_saves_scan(root);
    if (has("--saves-all")) return do_saves_all(root);
    std::vector<std::string> args;
    for (const auto& x : a) if (!starts_with(x, "-")) args.push_back(x);
    if (has("--saves-import")) {
        if (args.empty() || !is_digits(args[0])) die("usage: tuxthrottlectl prefix --saves-import <appid>");
        if (steam_running()) die("Steam is running - close Steam and the game first, then retry.");
        return import_loose_one(root, args[0]);
    }
    if (has("--saves")) {
        if (args.empty() || !is_digits(args[0])) die("usage: tuxthrottlectl prefix --saves <appid>");
        if (steam_running()) die("Steam is running - close Steam and the game first, then retry.");
        return pull_saves_one(root, args[0]);
    }
    if (args.empty() || !is_digits(args[0]))
        die("usage: tuxthrottlectl prefix <appid> [--check|--relevant] | --scan | --all | --lutris-scan | --lutris-fix <slug> | --lutris-all | --saves-scan | --saves <appid> | --saves-all | --saves-import <appid>");
    if (has("--relevant")) return do_relevant(root, args[0]);
    return do_one(root, args[0], has("--check"));
}

// ---------------------------------------------------------------- save vault
static const char* kSaveFolders[] = {"Documents", "Saved Games", "AppData/Roaming", "AppData/LocalLow"};

static std::pair<long, long long> tree_stats(const fs::path& d) {
    long n = 0;
    long long sz = 0;
    std::error_code ec;
    for (auto it = fs::recursive_directory_iterator(d, fs::directory_options::skip_permission_denied, ec); it != fs::recursive_directory_iterator(); it.increment(ec)) {
        if (ec) break;
        std::error_code e2;
        if (it->is_regular_file(e2)) { sz += static_cast<long long>(it->file_size(e2)); ++n; }
    }
    return {n, sz};
}

static std::string human_short(long long b) {
    double x = static_cast<double>(b);
    const char* u[] = {"B", "K", "M", "G"};
    for (int i = 0; i < 4; ++i) {
        if (x < 1024 || i == 3) return i == 0 ? format_double(x, 0) + u[i] : format_double(x, 1) + u[i];
        x /= 1024;
    }
    return format_double(x, 1) + "G";
}

static fs::path check_vault(const std::string& raw, bool create) {
    fs::path p = expand_home(raw);
    fs::path probe = p;
    std::error_code ec;
    while (!fs::exists(probe, ec) && probe != probe.parent_path()) probe = probe.parent_path();
    if (!fs::exists(probe, ec)) die("path does not exist: " + p.string());
    struct stat st, hs;
    ::stat(probe.c_str(), &st);
    ::stat(env_or("HOME", "/root").c_str(), &hs);
    if (st.st_dev == hs.st_dev)
        die("the save vault must be on a SEPARATE drive - not the OS / Steam drive. " + p.string() + " is on the same filesystem as your home directory.");
    if (create) {
        fs::create_directories(p, ec);
        if (ec) die("can't create vault folder " + p.string() + ": " + ec.message());
    }
    if (!fs::is_directory(p, ec)) die("vault folder does not exist: " + p.string());
    return canon(p);
}

static std::optional<fs::path> steamuser_dir(const fs::path& root, const std::string& appid) {
    auto lib = library_for_appid(root, appid);
    if (!lib) return std::nullopt;
    fs::path su = *lib / "steamapps" / "compatdata" / appid / "pfx" / "drive_c" / "users" / "steamuser";
    std::error_code ec;
    return fs::is_directory(su, ec) ? std::optional<fs::path>(su) : std::nullopt;
}

static void export_one(const fs::path& root, const std::string& appid, const fs::path& vault) {
    std::string name = appid_name(root, appid);
    if (name.empty()) name = appid;
    auto su = steamuser_dir(root, appid);
    if (!su) { std::printf("  %s (%s): no prefix on disk - launch it once first\n", name.c_str(), appid.c_str()); return; }
    fs::path box = vault / appid;
    int copied = 0;
    std::error_code ec;
    for (const char* rel : kSaveFolders) {
        fs::path src = *su / rel;
        if (!fs::is_directory(src, ec)) continue;
        auto [n, sz] = tree_stats(src);
        if (n == 0) continue;
        copy_tree(src, box / rel);
        std::printf("  %s: %s  (%ld files, %s)  ->  vault\n", name.c_str(), rel, n, human_short(sz).c_str());
        ++copied;
    }
    if (copied) {
        write_file((box / ".tuxthrottle-name").string(), name + "\n");
        std::printf("  %s (%s): %d folder(s) exported\n", name.c_str(), appid.c_str(), copied);
    } else std::printf("  %s (%s): nothing to export\n", name.c_str(), appid.c_str());
}

static void import_one(const fs::path& root, const std::string& appid, const fs::path& vault) {
    std::string name = appid_name(root, appid);
    if (name.empty()) name = appid;
    fs::path box = vault / appid;
    std::error_code ec;
    if (!fs::is_directory(box, ec)) { std::printf("  %s (%s): nothing in the vault\n", name.c_str(), appid.c_str()); return; }
    auto su = steamuser_dir(root, appid);
    if (!su) { std::printf("  %s (%s): no prefix on disk - launch it once first\n", name.c_str(), appid.c_str()); return; }
    int got = 0;
    for (const char* rel : kSaveFolders) {
        fs::path src = box / rel;
        if (!fs::is_directory(src, ec)) continue;
        copy_tree(src, *su / rel);
        auto [n, sz] = tree_stats(src);
        std::printf("  %s: vault  ->  %s  (%ld files, %s)\n", name.c_str(), rel, n, human_short(sz).c_str());
        ++got;
    }
    if (got) std::printf("  %s (%s): %d folder(s) imported\n", name.c_str(), appid.c_str(), got);
    else std::printf("  %s (%s): vault entry holds no known save folders\n", name.c_str(), appid.c_str());
}

static std::vector<std::string> vault_boxes(const fs::path& vault) {
    std::vector<std::string> out;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(vault, ec))
        if (e.is_directory(ec) && is_digits(e.path().filename().string())) out.push_back(e.path().filename().string());
    std::sort(out.begin(), out.end());
    return out;
}

int savevault_main(const std::vector<std::string>& a) {
    must_not_be_root();
    if (a.size() < 2 || (a[0] != "list" && a[0] != "export" && a[0] != "import"))
        die("usage: savevault list <vault> | export <vault> <appid>|all | import <vault> <appid>|all");
    std::string mode = a[0], raw = a[1], who = a.size() > 2 ? a[2] : "all";
    fs::path root = steam_root_or_die();
    if (mode == "list") {
        fs::path vault = check_vault(raw, false);
        auto boxes = vault_boxes(vault);
        if (boxes.empty()) { std::printf("vault %s is empty.\n", vault.c_str()); return 0; }
        std::printf("vault: %s\n\n", vault.c_str());
        std::error_code ec;
        for (const auto& b : boxes) {
            std::string nm = trim(read_file((vault / b / ".tuxthrottle-name").string()).value_or(""));
            std::vector<std::string> folders;
            for (const char* r : kSaveFolders) if (fs::is_directory(vault / b / r, ec)) folders.push_back(r);
            auto [n, sz] = tree_stats(vault / b);
            std::printf("  %-10s %s %s   %ld files, %s\n", b.c_str(), utf8_pad(utf8_trunc(nm, 38), 38).c_str(), folders.empty() ? "(empty)" : join(folders, ", ").c_str(), n, human_short(sz).c_str());
        }
        return 0;
    }
    fs::path vault = check_vault(raw, mode == "export");
    if (mode == "import" && steam_running()) die("Steam is running - close Steam (and every game) first, then retry.");
    auto run_one = [&](const std::string& ap) { mode == "export" ? export_one(root, ap, vault) : import_one(root, ap, vault); };
    if (who == "all") {
        std::vector<std::string> targets;
        if (mode == "export") for (const auto& p : all_prefixes(root)) targets.push_back(p.appid);
        else targets = vault_boxes(vault);
        if (targets.empty()) { std::puts("nothing to do."); return 0; }
        std::printf("%s: %zu game(s)\n\n", mode.c_str(), targets.size());
        for (const auto& ap : targets) run_one(ap);
        std::printf("\ndone - vault: %s\n", vault.c_str());
        return 0;
    }
    if (!is_digits(who)) die("expected a numeric AppID or 'all', got: " + who);
    run_one(who);
    std::printf("vault: %s\n", vault.c_str());
    return 0;
}

// ---------------------------------------------------------------- shader cache
static const char* kSubdirs[] = {"mesa-shader-cache", "dxvk-state-cache", "nv-shader-cache", "steam-shadercache"};
static const char* kDefaultDir = "~/.cache/tuxthrottle-shaders";
static const int kDefaultSizeGb = 80;
static const char* kBgKey = "EnableShaderBackgroundProcessing";

static std::string sc_config_path() { return expand_home("~/.config/tuxthrottle/shadercache.json"); }
static Json sc_load() { return read_json_file(sc_config_path()); }
static void sc_save(const Json& c) {
    mkdir_p(dir_name(sc_config_path()));
    write_file(sc_config_path(), c.dump(2, true));
}

std::string shadercache_dir() {
    std::string d = sc_load()["dir"].str();
    return expand_home(d.empty() ? kDefaultDir : d);
}

static int max_size_gb() {
    double v = sc_load()["max_size_gb"].as_number();
    return v > 0 ? static_cast<int>(v) : kDefaultSizeGb;
}

static fs::path ensure_dirs() {
    fs::path base = shadercache_dir();
    std::error_code ec;
    for (const char* s : kSubdirs) fs::create_directories(base / s, ec);
    return base;
}

static void relink_steam_shadercache(const fs::path& base) {
    fs::path dest = base / "steam-shadercache";
    auto root = steam_root_opt();
    if (!root) return;
    std::error_code ec;
    fs::create_directories(dest, ec);
    for (const auto& lib : all_libraries(*root)) {
        fs::path sc = lib / "steamapps" / "shadercache";
        if (!fs::is_symlink(sc, ec)) continue;
        auto cur = fs::canonical(sc, ec);
        if (!ec && cur == canon(dest)) continue;
        fs::remove(sc, ec);
        fs::create_directory_symlink(dest, sc, ec);
    }
}

static long long du_bytes(const fs::path& p) {
    std::error_code ec;
    if (!fs::exists(p, ec)) return 0;
    auto r = run_argv_stdout({"du", "-sb", p.string()}, 30000);
    auto f = split_ws(r.out);
    return f.empty() ? 0 : std::atoll(f[0].c_str());
}

static std::string human(double n) {
    const char* units[] = {"B", "KiB", "MiB", "GiB"};
    for (const char* u : units) {
        if (n < 1024) return std::string(u) == "B" ? format_double(n, 0) + " " + u : format_double(n, 1) + " " + u;
        n /= 1024.0;
    }
    return format_double(n, 1) + " TiB";
}

std::pair<bool, std::string> shadercache_set(const std::string& dir, std::optional<int> size_gb) {
    Json cfg = sc_load();
    cfg.set("dir", expand_home(dir));
    if (size_gb) cfg.set("max_size_gb", *size_gb);
    if (!cfg.has("max_size_gb")) cfg.set("max_size_gb", kDefaultSizeGb);
    sc_save(cfg);
    fs::path base = ensure_dirs();
    relink_steam_shadercache(base);
    return {true, "cache directory -> " + base.string() + "  (max " + std::to_string(max_size_gb()) + " GB for Mesa/NVIDIA)"};
}

Json shadercache_status() {
    fs::path base = ensure_dirs();
    long long steam_b = du_bytes(base / "steam-shadercache"), rest_b = 0;
    for (const char* s : kSubdirs) if (std::string(s) != "steam-shadercache") rest_b += du_bytes(base / s);
    Json o = Json::obj();
    o.set("dir", base.string());
    o.set("max_size_gb", max_size_gb());
    o.set("total", human(static_cast<double>(steam_b + rest_b)));
    o.set("steam_shader_cache", human(static_cast<double>(steam_b)));
    o.set("other_shader_caches", human(static_cast<double>(rest_b)));
    Json b = Json::obj();
    b.set("total", static_cast<double>(steam_b + rest_b));
    b.set("steam", static_cast<double>(steam_b));
    b.set("other", static_cast<double>(rest_b));
    o.set("bytes", b);
    return o;
}

static std::pair<bool, std::string> link_steam_shadercache() {
    if (steam_running()) return {false, "close Steam first"};
    auto root = steam_root_opt();
    if (!root) return {false, "no Steam install found"};
    fs::path base = ensure_dirs(), dest = base / "steam-shadercache";
    std::error_code ec;
    fs::create_directories(dest, ec);
    std::vector<std::string> linked;
    for (const auto& lib : all_libraries(*root)) {
        fs::path sc = lib / "steamapps" / "shadercache";
        if (fs::is_symlink(sc, ec)) {
            auto cur = fs::canonical(sc, ec);
            if (!ec && cur == canon(dest)) continue;
            fs::remove(sc, ec);
            fs::create_directory_symlink(dest, sc, ec);
            if (ec) return {false, "could not repair " + sc.string() + ": " + ec.message()};
            linked.push_back(sc.string() + " (repaired)");
            continue;
        }
        if (fs::is_directory(sc, ec)) {
            for (const auto& item : fs::directory_iterator(sc, ec)) {
                fs::path target = dest / item.path().filename();
                if (!fs::exists(target, ec)) fs::rename(item.path(), target, ec);
                if (ec) { ec.clear(); copy_tree(item.path(), target); rm_rf(item.path()); }
            }
            rm_rf(sc);
        }
        fs::create_directory_symlink(dest, sc, ec);
        linked.push_back(sc.string());
    }
    if (linked.empty()) return {true, "nothing to do - already linked, or no library has a shadercache folder yet"};
    return {true, "linked: " + join(linked, ", ")};
}

static std::pair<bool, std::string> unlink_steam_shadercache() {
    if (steam_running()) return {false, "close Steam first"};
    auto root = steam_root_opt();
    if (!root) return {false, "no Steam install found"};
    fs::path dest = ensure_dirs() / "steam-shadercache";
    std::vector<std::string> restored;
    std::error_code ec;
    for (const auto& lib : all_libraries(*root)) {
        fs::path sc = lib / "steamapps" / "shadercache";
        if (!fs::is_symlink(sc, ec)) continue;
        fs::remove(sc, ec);
        fs::create_directories(sc, ec);
        if (fs::is_directory(dest, ec))
            for (const auto& item : fs::directory_iterator(dest, ec)) {
                fs::path target = sc / item.path().filename();
                if (fs::exists(target, ec)) continue;
                if (item.is_directory(ec)) copy_tree(item.path(), target);
                else fs::copy_file(item.path(), target, ec);
            }
        restored.push_back(sc.string());
    }
    if (restored.empty()) return {true, "nothing was linked"};
    return {true, "restored: " + join(restored, ", ")};
}

Json steam_link_status() {
    Json out = Json::obj();
    auto root = steam_root_opt();
    if (!root) {
        out.set("ok", true);
        out.set("libs", Json::arr());
        out.set("summary", "no Steam install found");
        return out;
    }
    fs::path dest = canon(fs::path(shadercache_dir()) / "steam-shadercache");
    Json libs = Json::arr();
    int broken = 0, linked = 0;
    std::error_code ec;
    for (const auto& lib : all_libraries(*root)) {
        fs::path sc = lib / "steamapps" / "shadercache";
        std::string state, detail;
        if (fs::is_symlink(sc, ec)) {
            auto tgt = fs::canonical(sc, ec);
            if (!ec) { state = tgt == dest ? "ok" : "broken"; detail = tgt.string(); }
            else { ec.clear(); state = "broken"; detail = "dangling → " + fs::read_symlink(sc, ec).string(); }
        } else if (fs::exists(sc, ec)) { state = "unlinked"; detail = "plain directory"; }
        else { state = "unlinked"; detail = "not created yet"; }
        broken += state == "broken";
        linked += state == "ok";
        Json l = Json::obj();
        l.set("lib", lib.string());
        l.set("path", sc.string());
        l.set("state", state);
        l.set("detail", detail);
        libs.push(l);
    }
    size_t n = libs.array().size();
    std::string summary;
    if (broken) summary = std::to_string(broken) + " broken shadercache link" + (broken != 1 ? "s" : "") + " - Steam writes will fail; press “Link Steam cache” to repair";
    else if (linked) summary = std::to_string(linked) + "/" + std::to_string(n) + " librar" + (n != 1 ? "ies" : "y") + " linked, all healthy";
    else summary = "not linked (each library keeps its own shadercache)";
    out.set("ok", broken == 0);
    out.set("libs", libs);
    out.set("summary", summary);
    out.set("broken", broken);
    out.set("linked", linked);
    return out;
}

std::pair<bool, std::string> heal_steam_links() {
    Json before = steam_link_status();
    if (before["ok"].as_bool()) return {true, "healthy - nothing to repair"};
    relink_steam_shadercache(ensure_dirs());
    Json after = steam_link_status();
    if (after["ok"].as_bool()) return {true, "repaired " + std::to_string(static_cast<int>(before["broken"].as_number())) + " broken shadercache link(s)"};
    return {false, after["summary"].str()};
}

static std::vector<fs::path> all_steam_shadercache_dirs() {
    std::vector<fs::path> dirs{fs::path(shadercache_dir()) / "steam-shadercache"};
    auto root = steam_root_opt();
    std::error_code ec;
    if (root)
        for (const auto& lib : all_libraries(*root)) {
            fs::path sc = lib / "steamapps" / "shadercache";
            if (fs::exists(sc, ec) || fs::is_symlink(sc, ec)) dirs.push_back(sc);
        }
    std::set<fs::path> seen;
    std::vector<fs::path> out;
    for (const auto& d : dirs) {
        fs::path key = canon(d);
        if (seen.insert(key).second) out.push_back(d);
    }
    return out;
}

static bool empty_dir(const fs::path& d) {
    std::error_code ec;
    if (!fs::is_directory(d, ec)) return false;
    for (const auto& c : fs::directory_iterator(d, ec)) {
        std::error_code e2;
        if (c.is_directory(e2) && !c.is_symlink(e2)) fs::remove_all(c.path(), e2);
        else fs::remove(c.path(), e2);
    }
    return true;
}

static std::pair<bool, std::string> rebuild(const std::string& target) {
    if (steam_running()) return {false, "close Steam first (it keeps its shader cache open)"};
    ensure_dirs();
    std::error_code ec;
    if (!target.empty() && target != "all") {
        if (!is_digits(target)) return {false, "target must be 'all' or a numeric AppID (got '" + target + "')"};
        int hit = 0;
        for (const auto& scdir : all_steam_shadercache_dirs()) {
            fs::path app = scdir / target;
            if (fs::exists(app, ec) || fs::is_symlink(app, ec)) {
                if (fs::is_directory(app, ec) && !fs::is_symlink(app, ec)) fs::remove_all(app, ec);
                else fs::remove(app, ec);
                if (!ec) ++hit;
            }
        }
        if (hit) return {true, "cleared Steam's shader cache for AppID " + target + " (" + std::to_string(hit) + " location" + (hit != 1 ? "s" : "") + "); Steam rebuilds it on the next launch"};
        return {true, "no Steam shader cache found for AppID " + target + " - nothing to do"};
    }
    std::vector<std::string> cleared;
    for (const auto& d : all_steam_shadercache_dirs()) if (empty_dir(d)) cleared.push_back(d.string());
    if (cleared.empty()) return {true, "Steam's shader cache was already empty"};
    return {true, "cleared " + join(cleared, ", ") + " - Steam rebuilds its shader cache on the next launch"};
}

static std::optional<fs::path> steam_config_vdf() {
    auto root = steam_root_opt();
    if (!root) return std::nullopt;
    fs::path p = *root / "config" / "config.vdf";
    std::error_code ec;
    return fs::is_regular_file(p, ec) ? std::optional<fs::path>(p) : std::nullopt;
}

std::optional<std::string> steam_bg_shaders_status() {
    auto p = steam_config_vdf();
    if (!p) return std::nullopt;
    auto t = read_file(p->string());
    if (!t) return std::nullopt;
    std::regex re(std::string("\"") + kBgKey + "\"\\s+\"([01])\"");
    std::smatch m;
    if (!std::regex_search(*t, m, re)) return std::string("on");
    return m[1] == "1" ? "on" : "off";
}

std::optional<std::string> set_bg_shader_key(const std::string& txt, bool enabled) {
    std::string want = enabled ? "1" : "0";
    std::regex pat(std::string("(\"") + kBgKey + "\"\\s+\")[01](\")");
    if (std::regex_search(txt, pat)) return regex_replace_fn(txt, pat, [&](const std::smatch& m) { return m[1].str() + want + m[2].str(); }, true);
    std::regex block(R"("ShaderCacheManager"\s*\n\s*\{)");
    std::smatch m;
    if (!std::regex_search(txt, m, block)) return std::nullopt;
    return txt.substr(0, static_cast<size_t>(m.position()) + m.length()) + "\n\t\t\t\t\t\"" + kBgKey + "\"\t\t\"" + want + "\"" +
           txt.substr(static_cast<size_t>(m.position()) + m.length());
}

std::pair<bool, std::string> set_steam_bg_shaders(bool enabled) {
    if (steam_running()) return {false, "close Steam first (it rewrites config.vdf on exit)"};
    auto p = steam_config_vdf();
    if (!p) return {false, "no Steam config.vdf found"};
    auto txt = read_file(p->string());
    if (!txt) return {false, "cannot read " + p->string()};
    auto nw = set_bg_shader_key(*txt, enabled);
    if (!nw) return {false, "no \"ShaderCacheManager\" block in config.vdf to add the key to"};
    if (*nw == *txt) return {true, std::string("already ") + (enabled ? "on" : "off")};
    write_file(p->string() + ".tuxthrottle-bak-" + std::to_string(std::time(nullptr)), *txt);
    std::string tmp = p->string() + ".tuxthrottle-tmp";
    if (!write_file(tmp, *nw) || ::rename(tmp.c_str(), p->c_str()) != 0) return {false, "write failed"};
    return {true, std::string("background processing of Vulkan shaders → ") + (enabled ? "ON" : "OFF") + " (restart Steam for it to take effect)"};
}

static std::pair<bool, std::string> clean(const std::string& what) {
    if (steam_running() && (what == "steam-shadercache" || what == "all")) return {false, "close Steam first (it may be writing to its shader cache)"};
    fs::path base = ensure_dirs();
    std::vector<std::string> targets;
    if (what == "all" || what.empty()) for (const char* s : kSubdirs) targets.push_back(s);
    else targets.push_back(what);
    std::vector<std::string> cleared;
    std::error_code ec;
    for (const auto& name : targets) {
        fs::path d = base / name;
        if (!fs::is_directory(d, ec)) continue;
        for (const auto& c : fs::directory_iterator(d, ec)) fs::remove_all(c.path(), ec);
        cleared.push_back(name);
    }
    if (cleared.empty()) return {true, "nothing to clean"};
    return {true, "cleared: " + join(cleared, ", ")};
}

int shadercache_main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: shadercache {dir|show|set|link-steam|link-check|heal|rebuild|steam-bg-shaders|clean}"); return 2; }
    const std::string& cmd = a[0];
    bool as_json = std::find(a.begin(), a.end(), "--json") != a.end();
    auto report = [](const std::pair<bool, std::string>& r) { std::puts(r.second.c_str()); return r.first ? 0 : 1; };
    if (cmd == "dir") { std::puts(shadercache_status()["dir"].str().c_str()); return 0; }
    if (cmd == "show") {
        Json s = shadercache_status();
        if (as_json) std::puts(s.dump(2, true).c_str());
        else {
            std::printf("dir:                 %s\n", s["dir"].str().c_str());
            std::printf("max size (Mesa/NV):  %d GB\n", static_cast<int>(s["max_size_gb"].as_number()));
            std::printf("total:               %s\n", s["total"].str().c_str());
            std::printf("  Steam shader cache: %s\n", s["steam_shader_cache"].str().c_str());
            std::printf("  other caches:       %s   (Mesa + DXVK + NVIDIA)\n", s["other_shader_caches"].str().c_str());
        }
        return 0;
    }
    if (cmd == "set") {
        if (a.size() < 2) return 2;
        std::optional<int> size;
        for (size_t i = 2; i + 1 < a.size(); ++i) if (a[i] == "--size") size = std::atoi(a[i + 1].c_str());
        return report(shadercache_set(a[1], size));
    }
    if (cmd == "link-steam") return report(std::find(a.begin(), a.end(), "--undo") != a.end() ? unlink_steam_shadercache() : link_steam_shadercache());
    if (cmd == "link-check") {
        Json st = steam_link_status();
        if (as_json) std::puts(st.dump(2, true).c_str());
        else {
            std::puts(st["summary"].str().c_str());
            for (const auto& x : st["libs"].array()) std::printf("  [%-8s] %s  (%s)\n", x["state"].str().c_str(), x["path"].str().c_str(), x["detail"].str().c_str());
        }
        return st["ok"].as_bool() ? 0 : 1;
    }
    if (cmd == "heal") return report(heal_steam_links());
    if (cmd == "rebuild") return report(rebuild(a.size() > 1 ? a[1] : "all"));
    if (cmd == "steam-bg-shaders") {
        std::string state = a.size() > 1 ? a[1] : "status";
        if (state == "status") {
            auto s = steam_bg_shaders_status();
            std::puts(s ? s->c_str() : "unknown (no Steam config found)");
            return s ? 0 : 1;
        }
        return report(set_steam_bg_shaders(state == "on"));
    }
    if (cmd == "clean") return report(clean(a.size() > 1 ? a[1] : "all"));
    return 2;
}

}  // namespace tt::steam
