#include "compat.hpp"

#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <regex>

#include "formats.hpp"
#include "launchers.hpp"
#include "steam.hpp"
#include "userenv.hpp"
#include "util.hpp"

extern char** environ;

namespace tt::compat {

namespace fs = std::filesystem;

// ---------------------------------------------------------------- SHA-512
namespace {
const unsigned long long K[80] = {
    0x428a2f98d728ae22ULL, 0x7137449123ef65cdULL, 0xb5c0fbcfec4d3b2fULL, 0xe9b5dba58189dbbcULL, 0x3956c25bf348b538ULL, 0x59f111f1b605d019ULL, 0x923f82a4af194f9bULL,
    0xab1c5ed5da6d8118ULL, 0xd807aa98a3030242ULL, 0x12835b0145706fbeULL, 0x243185be4ee4b28cULL, 0x550c7dc3d5ffb4e2ULL, 0x72be5d74f27b896fULL, 0x80deb1fe3b1696b1ULL,
    0x9bdc06a725c71235ULL, 0xc19bf174cf692694ULL, 0xe49b69c19ef14ad2ULL, 0xefbe4786384f25e3ULL, 0x0fc19dc68b8cd5b5ULL, 0x240ca1cc77ac9c65ULL, 0x2de92c6f592b0275ULL,
    0x4a7484aa6ea6e483ULL, 0x5cb0a9dcbd41fbd4ULL, 0x76f988da831153b5ULL, 0x983e5152ee66dfabULL, 0xa831c66d2db43210ULL, 0xb00327c898fb213fULL, 0xbf597fc7beef0ee4ULL,
    0xc6e00bf33da88fc2ULL, 0xd5a79147930aa725ULL, 0x06ca6351e003826fULL, 0x142929670a0e6e70ULL, 0x27b70a8546d22ffcULL, 0x2e1b21385c26c926ULL, 0x4d2c6dfc5ac42aedULL,
    0x53380d139d95b3dfULL, 0x650a73548baf63deULL, 0x766a0abb3c77b2a8ULL, 0x81c2c92e47edaee6ULL, 0x92722c851482353bULL, 0xa2bfe8a14cf10364ULL, 0xa81a664bbc423001ULL,
    0xc24b8b70d0f89791ULL, 0xc76c51a30654be30ULL, 0xd192e819d6ef5218ULL, 0xd69906245565a910ULL, 0xf40e35855771202aULL, 0x106aa07032bbd1b8ULL, 0x19a4c116b8d2d0c8ULL,
    0x1e376c085141ab53ULL, 0x2748774cdf8eeb99ULL, 0x34b0bcb5e19b48a8ULL, 0x391c0cb3c5c95a63ULL, 0x4ed8aa4ae3418acbULL, 0x5b9cca4f7763e373ULL, 0x682e6ff3d6b2b8a3ULL,
    0x748f82ee5defb2fcULL, 0x78a5636f43172f60ULL, 0x84c87814a1f0ab72ULL, 0x8cc702081a6439ecULL, 0x90befffa23631e28ULL, 0xa4506cebde82bde9ULL, 0xbef9a3f7b2c67915ULL,
    0xc67178f2e372532bULL, 0xca273eceea26619cULL, 0xd186b8c721c0c207ULL, 0xeada7dd6cde0eb1eULL, 0xf57d4f7fee6ed178ULL, 0x06f067aa72176fbaULL, 0x0a637dc5a2c898a6ULL,
    0x113f9804bef90daeULL, 0x1b710b35131c471bULL, 0x28db77f523047d84ULL, 0x32caab7b40c72493ULL, 0x3c9ebe0a15c9bebcULL, 0x431d67c49c100d4cULL, 0x4cc5d4becb3e42b6ULL,
    0x597f299cfc657e2aULL, 0x5fcb6fab3ad6faecULL, 0x6c44198c4a475817ULL};
inline unsigned long long rotr(unsigned long long x, int n) { return (x >> n) | (x << (64 - n)); }
}  // namespace

std::string sha512_hex(const std::string& data) {
    unsigned long long h[8] = {0x6a09e667f3bcc908ULL, 0xbb67ae8584caa73bULL, 0x3c6ef372fe94f82bULL, 0xa54ff53a5f1d36f1ULL,
                               0x510e527fade682d1ULL, 0x9b05688c2b3e6c1fULL, 0x1f83d9abfb41bd6bULL, 0x5be0cd19137e2179ULL};
    std::string msg = data;
    unsigned long long bitlen = static_cast<unsigned long long>(data.size()) * 8;
    msg += static_cast<char>(0x80);
    while (msg.size() % 128 != 112) msg += '\0';
    for (int i = 0; i < 8; ++i) msg += '\0';   // high 64 bits of the 128-bit length
    for (int i = 7; i >= 0; --i) msg += static_cast<char>((bitlen >> (i * 8)) & 0xff);
    for (size_t off = 0; off < msg.size(); off += 128) {
        unsigned long long w[80];
        for (int i = 0; i < 16; ++i) {
            unsigned long long v = 0;
            for (int b = 0; b < 8; ++b) v = (v << 8) | static_cast<unsigned char>(msg[off + static_cast<size_t>(i * 8 + b)]);
            w[i] = v;
        }
        for (int i = 16; i < 80; ++i) {
            unsigned long long s0 = rotr(w[i - 15], 1) ^ rotr(w[i - 15], 8) ^ (w[i - 15] >> 7);
            unsigned long long s1 = rotr(w[i - 2], 19) ^ rotr(w[i - 2], 61) ^ (w[i - 2] >> 6);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        unsigned long long a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 80; ++i) {
            unsigned long long S1 = rotr(e, 14) ^ rotr(e, 18) ^ rotr(e, 41);
            unsigned long long ch = (e & f) ^ (~e & g);
            unsigned long long t1 = hh + S1 + ch + K[i] + w[i];
            unsigned long long S0 = rotr(a, 28) ^ rotr(a, 34) ^ rotr(a, 39);
            unsigned long long mj = (a & b) ^ (a & c) ^ (b & c);
            unsigned long long t2 = S0 + mj;
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    char buf[17];
    std::string out;
    for (unsigned long long v : h) { std::snprintf(buf, sizeof buf, "%016llx", v); out += buf; }
    return out;
}

static std::string sha512_file(const std::string& path) {
    // stream through sha512sum-free code: read whole file (archives are <1 GB but typically ~400 MB; chunk to bound memory)
    auto t = read_file(path);
    return t ? sha512_hex(*t) : "";
}

// ---------------------------------------------------------------- natural sort
static std::vector<std::pair<bool, std::string>> natural_tokens(const std::string& s) {
    std::vector<std::pair<bool, std::string>> out;   // {is_number, text}; alternate starting with a (possibly empty) string
    std::string cur;
    bool digits = false;
    out.reserve(s.size());
    for (char c : s) {
        bool d = std::isdigit(static_cast<unsigned char>(c)) != 0;
        if (d != digits) { out.emplace_back(digits, cur); cur.clear(); digits = d; }
        cur += c;
    }
    out.emplace_back(digits, cur);
    return out;
}

bool natural_less(const std::string& a, const std::string& b) {
    auto ta = natural_tokens(a), tb = natural_tokens(b);
    for (size_t i = 0; i < std::min(ta.size(), tb.size()); ++i) {
        if (ta[i].first && tb[i].first) {
            unsigned long long x = std::strtoull(ta[i].second.c_str(), nullptr, 10), y = std::strtoull(tb[i].second.c_str(), nullptr, 10);
            if (x != y) return x < y;
        } else {
            std::string x = lower(ta[i].second), y = lower(tb[i].second);
            if (x != y) return x < y;
        }
    }
    return ta.size() < tb.size();
}

std::string natural_key(const std::string& s) { return s; }

// ---------------------------------------------------------------- tool table
namespace {
struct ToolSpec {
    const char* name; const char* repo; const char* ext; bool arch; std::vector<std::string> launchers; const char* kind; const char* about;
};
const std::vector<ToolSpec>& tools() {
    static const std::vector<ToolSpec> v = {
        {"GE-Proton", "GloriousEggroll/proton-ge-custom", ".tar.gz", false, {"steam", "heroic", "lutris", "bottles"}, "proton",
         "GloriousEggroll's Proton - newest fixes, media codecs; the usual choice for launcher/anti-cheat titles"},
        {"Proton-CachyOS", "CachyOS/proton-cachyos", ".tar.xz", true, {"steam", "heroic", "lutris", "bottles"}, "proton",
         "CachyOS's performance-patched Proton (ntsync, x86-64-v3 build on AVX2 CPUs)"},
        {"Proton-EM", "Etaash-mathamsetty/Proton", ".tar.xz", false, {"steam", "heroic", "lutris", "bottles"}, "proton", "Proton with experimental Wayland / HDR patches"},
        {"Luxtorpeda", "luxtorpeda-dev/luxtorpeda", ".tar.xz", false, {"steam"}, "proton", "runs supported Windows games on native Linux source ports"},
        {"Boxtron", "dreamer/boxtron", ".tar.xz", false, {"steam"}, "proton", "runs DOS games through native DOSBox"},
        {"Wine-GE", "GloriousEggroll/wine-ge-custom", ".tar.xz", false, {"lutris", "heroic", "bottles"}, "wine",
         "GloriousEggroll's Wine for Lutris / Heroic / Bottles - discontinued upstream (GE-Proton via umu replaced it); the last releases are still downloadable"},
        {"Lutris-Wine", "lutris/wine", ".tar.xz", false, {"lutris", "bottles"}, "wine", "Lutris's own Wine builds"},
        {"DXVK", "doitsujin/dxvk", ".tar.gz", false, {"lutris", "heroic", "bottles"}, "dxvk", "Direct3D 8-11 \xE2\x86\x92 Vulkan layer used inside a Wine prefix"},
        {"vkd3d-proton", "HansKristian-Work/vkd3d-proton", ".tar.zst", false, {"lutris", "heroic", "bottles"}, "vkd3d", "Direct3D 12 \xE2\x86\x92 Vulkan layer used inside a Wine prefix"},
    };
    return v;
}
const ToolSpec* find_tool(const std::string& n) { for (const auto& t : tools()) if (n == t.name) return &t; return nullptr; }
const std::vector<std::string> kKinds = {"proton", "wine", "dxvk", "vkd3d"};
const std::vector<std::pair<std::string, std::string>> kValveTools = {
    {"1493710", "Proton Experimental"}, {"2180100", "Proton Hotfix"}, {"4628710", "Proton 11.0"}, {"3658110", "Proton 10.0"}, {"2805730", "Proton 9.0"},
    {"2348590", "Proton 8.0"}, {"1887720", "Proton 7.0"}, {"4183110", "Steam Linux Runtime 4.0"}, {"1628350", "Steam Linux Runtime 3.0 (sniper)"},
    {"1391110", "Steam Linux Runtime 2.0 (soldier)"}, {"1070560", "Steam Linux Runtime 1.0 (scout)"}, {"1161040", "Proton BattlEye Runtime"},
    {"1826330", "Proton EasyAntiCheat Runtime"}};
const std::map<std::string, std::string> kStaticToolNames = {{"proton_experimental", "1493710"}, {"proton_hotfix", "2180100"}, {"proton_11", "4628710"},
                                                            {"proton_10", "3658110"}, {"proton_9", "2805730"}, {"proton_8", "2348590"}, {"proton_7", "1887720"}};

std::string home() { return env_or("HOME", "/root"); }
bool is_file(const std::string& p) { std::error_code ec; return fs::is_regular_file(p, ec); }
bool is_dir(const std::string& p) { std::error_code ec; return fs::is_directory(p, ec); }

std::string steam_root_str() {
    auto r = launchers::steam_roots_list();
    return r.empty() ? "" : r[0];
}

std::optional<std::string> http_get(const std::string& url, int timeout_s = 30) {
    std::string curl = which("curl");
    if (curl.empty()) return std::nullopt;
    auto r = run_argv_stdout({curl, "-fsSL", "--max-time", std::to_string(timeout_s), "-H", "User-Agent: tuxthrottle-compat-tools", "-H", "Accept: application/vnd.github+json", url},
                             (timeout_s + 5) * 1000);
    if (r.code != 0) return std::nullopt;
    return r.out;
}


Json obj_row(std::initializer_list<std::pair<const char*, Json>> kv) {
    Json o = Json::obj();
    for (const auto& p : kv) o.set(p.first, p.second);
    return o;
}
}  // namespace

std::string pick_arch(bool has_avx2) { return has_avx2 ? "x86_64_v3" : "x86_64"; }

static std::string cpu_arch() {
    std::string txt = read_file("/proc/cpuinfo").value_or("");
    static const std::regex avx2(R"(\bavx2\b)");
    return pick_arch(std::regex_search(txt, avx2));
}

Json pick_assets(const std::string& tool, const Json& assets, const std::string& arch_in) {
    const ToolSpec* spec = find_tool(tool);
    if (!spec) return Json();
    std::string ext = spec->ext;
    std::map<std::string, const Json*> names;
    for (const auto& a : assets.array()) if (!a["browser_download_url"].str().empty()) names[a["name"].str()] = &a;
    std::vector<std::string> cands;
    for (const auto& kv : names) {
        const std::string& n = kv.first;
        if (ends_with(n, ext) && n.find("aarch64") == std::string::npos && n.find("arm64") == std::string::npos) cands.push_back(n);
    }
    if (spec->arch) {
        std::string arch = arch_in.empty() ? cpu_arch() : arch_in;
        std::vector<std::string> exact, base;
        for (const auto& n : cands) {
            if (ends_with(n, arch + ext)) exact.push_back(n);
            if (ends_with(n, "x86_64" + ext)) base.push_back(n);
        }
        if (!exact.empty()) cands = exact;
        else if (!base.empty()) cands = base;
    }
    if (cands.empty()) return Json();
    // python: names is a dict in asset order, sorted(cands, key=len)[0] is stable in that order
    std::vector<std::string> ordered;
    for (const auto& a : assets.array()) {
        std::string n = a["name"].str();
        if (std::find(cands.begin(), cands.end(), n) != cands.end() && std::find(ordered.begin(), ordered.end(), n) == ordered.end()) ordered.push_back(n);
    }
    std::stable_sort(ordered.begin(), ordered.end(), [](const std::string& a, const std::string& b) { return a.size() < b.size(); });
    std::string name = ordered[0], stem = name.substr(0, name.size() - ext.size());
    std::vector<std::string> sums_all;
    for (const auto& a : assets.array()) {
        std::string n = a["name"].str();
        if (ends_with(n, ".sha512sum") && names.count(n)) sums_all.push_back(n);
    }
    std::string sum;
    for (const auto& n : sums_all) if (starts_with(n, stem) || sums_all.size() == 1) { sum = n; break; }
    const Json& a = *names[name];
    Json out = Json::obj();
    out.set("asset", name);
    out.set("url", a["browser_download_url"].str());
    out.set("size", a["size"].as_number());
    out.set("checksum_url", sum.empty() ? "" : (*names[sum])["browser_download_url"].str());
    return out;
}

static std::vector<Json> releases(const std::string& tool, int count) {
    std::vector<Json> out;
    const ToolSpec* spec = find_tool(tool);
    if (!spec) return out;
    auto body = http_get("https://api.github.com/repos/" + std::string(spec->repo) + "/releases?per_page=" + std::to_string(count));
    if (!body) throw std::runtime_error("cannot reach api.github.com (is curl installed and the network up?)");
    Json data = Json::parse(*body);
    for (const auto& rel : data.array()) {
        Json pick = pick_assets(tool, rel["assets"], "");
        if (pick.is_null()) continue;
        Json r = Json::obj();
        r.set("tag", rel["tag_name"].str());
        r.set("date", rel["published_at"].str().substr(0, 10));
        for (const auto& kv : pick.object()) r.set(kv.first, kv.second);
        out.push_back(r);
    }
    return out;
}

// ---------------------------------------------------------------- targets
Json targets() {
    struct Cand { std::string name, launcher, marker; std::vector<std::pair<std::string, std::string>> dirs; };
    std::vector<Cand> c;
    std::string h = home(), flat = h + "/.var/app";
    std::string root = steam_root_str();
    if (!root.empty()) c.push_back({"Steam", "steam", root, {{"proton", root + "/compatibilitytools.d"}}});
    std::string fs_ = flat + "/com.valvesoftware.Steam/data/Steam";
    c.push_back({"Steam Flatpak", "steam", fs_, {{"proton", fs_ + "/compatibilitytools.d"}}});
    for (auto [name, base] : {std::pair<std::string, std::string>{"Heroic", h + "/.config/heroic"}, {"Heroic Flatpak", flat + "/com.heroicgameslauncher.hgl/config/heroic"}}) {
        std::string t = base + "/tools";
        c.push_back({name, "heroic", base, {{"proton", t + "/proton"}, {"wine", t + "/wine"}, {"dxvk", t + "/dxvk"}, {"vkd3d", t + "/vkd3d"}}});
    }
    for (auto [name, base] : {std::pair<std::string, std::string>{"Lutris", h + "/.local/share/lutris"}, {"Lutris Flatpak", flat + "/net.lutris.Lutris/data/lutris"}})
        c.push_back({name, "lutris", base, {{"proton", base + "/runners/wine"}, {"wine", base + "/runners/wine"}, {"dxvk", base + "/runtime/dxvk"}, {"vkd3d", base + "/runtime/vkd3d"}}});
    for (auto [name, base] : {std::pair<std::string, std::string>{"Bottles", h + "/.local/share/bottles"}, {"Bottles Flatpak", flat + "/com.usebottles.bottles/data/bottles"}})
        c.push_back({name, "bottles", base, {{"proton", base + "/runners"}, {"wine", base + "/runners"}, {"dxvk", base + "/dxvk"}, {"vkd3d", base + "/vkd3d"}}});
    Json out = Json::arr();
    for (const auto& x : c) {
        Json dirs = Json::obj();
        for (const auto& kv : x.dirs) dirs.set(kv.first, kv.second);
        Json row = Json::obj();
        row.set("name", x.name);
        row.set("launcher", x.launcher);
        row.set("present", is_dir(x.marker));
        row.set("dir", x.dirs[0].second);
        row.set("dirs", dirs);
        out.push(row);
    }
    return out;
}

static std::optional<Json> target_by_name(const std::string& name) {
    Json t = targets();
    for (const auto& x : t.array()) if (lower(x["name"].str()) == lower(name)) return x;
    return std::nullopt;
}

static std::vector<std::string> installed(const std::string& dir) {
    std::vector<std::string> out;
    std::error_code ec;
    if (!is_dir(dir)) return out;
    for (const auto& e : fs::directory_iterator(dir, ec))
        if (e.is_directory(ec) && e.path().filename().string()[0] != '.') out.push_back(e.path().filename().string());
    std::sort(out.begin(), out.end(), [](const std::string& a, const std::string& b) { return natural_less(b, a); });   // reverse
    return out;
}

// ---------------------------------------------------------------- download / extract
static bool download(const std::string& url, const std::string& dest, double size) {
    std::string curl = which("curl");
    if (curl.empty()) return false;
    long pid = spawn_quiet({curl, "-fsSL", "--max-time", "3600", "-A", "tuxthrottle-compat-tools", "-o", dest, url});
    if (pid <= 0) return false;
    int rc = 0;
    long last = -1;
    while (child_running(pid, &rc)) {
        sleep_ms(500);
        struct stat st;
        if (::stat(dest.c_str(), &st) == 0) {
            long mib = static_cast<long>(st.st_size >> 20);
            int pct = size > 0 ? static_cast<int>(static_cast<double>(st.st_size) * 100 / size) : 0;
            if (pct / 10 != last || (size <= 0 && mib != last)) {
                last = size > 0 ? pct / 10 : mib;
                std::printf("  downloaded %ld MiB%s\n", mib, size > 0 ? (" (" + std::to_string(pct) + "%)").c_str() : "");
                std::fflush(stdout);
            }
        }
    }
    return rc == 0;
}

static std::string extract(const std::string& archive, const std::string& into) {
    std::string tar = which("tar");
    if (tar.empty()) throw std::runtime_error("`tar` is not installed");
    std::vector<std::string> list_cmd{tar};
    bool zst = ends_with(archive, ".zst");
    if (zst) {
        if (which("zstd").empty()) throw std::runtime_error("this archive is zstd-compressed - install `zstd`");
        list_cmd.push_back("--zstd");
    }
    list_cmd.push_back("-tf");
    list_cmd.push_back(archive);
    auto lst = run_argv_stdout(list_cmd, 600000);
    if (lst.code != 0) throw std::runtime_error("cannot read the archive");
    std::set<std::string> tops;
    for (auto name : split_lines(lst.out)) {
        if (name.empty() || name == "." || name == "./") continue;
        if (starts_with(name, "./")) name = name.substr(2);
        if (name.empty()) continue;
        if (name[0] == '/' || name.find("../") != std::string::npos || name == "..") throw std::runtime_error("archive contains an unsafe path: " + name);
        tops.insert(name.substr(0, name.find('/')));
    }
    if (tops.size() != 1) throw std::runtime_error("archive has " + std::to_string(tops.size()) + " top-level entries, expected 1");
    std::vector<std::string> x{tar};
    if (zst) x.push_back("--zstd");
    x.insert(x.end(), {"-xf", archive, "-C", into, "--no-same-owner", "--no-same-permissions"});
    auto r = run_argv(x, 1800000);
    if (r.code != 0) throw std::runtime_error("extraction failed: " + trim(r.out));
    return *tops.begin();
}

static int install(const std::string& tool, const std::string& tag, const std::string& target, bool force) {
    const ToolSpec* spec = find_tool(tool);
    if (!spec) {
        std::vector<std::string> n;
        for (const auto& t : tools()) n.push_back(t.name);
        std::printf("unknown tool '%s' - one of: %s\n", tool.c_str(), join(n, ", ").c_str());
        return 1;
    }
    auto tgt = target_by_name(target);
    if (!tgt) { std::printf("unknown target '%s'\n", target.c_str()); return 1; }
    bool ok_launcher = std::find(spec->launchers.begin(), spec->launchers.end(), (*tgt)["launcher"].str()) != spec->launchers.end();
    if (!ok_launcher) { std::printf("%s isn't meant for %s\n", tool.c_str(), (*tgt)["name"].str().c_str()); return 1; }
    auto rels = releases(tool, 30);
    const Json* rel = nullptr;
    for (const auto& r : rels) if (tag.empty() ? (rel == nullptr) : r["tag"].str() == tag) { rel = &r; if (tag.empty() || true) break; }
    if (!rel) { std::printf("no downloadable release found for %s%s\n", tool.c_str(), tag.empty() ? "" : (" " + tag).c_str()); return 1; }
    std::string kind = spec->kind;
    if (!(*tgt)["dirs"].has(kind)) { std::printf("%s has no place for a %s build\n", (*tgt)["name"].str().c_str(), kind.c_str()); return 1; }
    std::string dest = (*tgt)["dirs"][kind].str();
    std::error_code ec;
    fs::create_directories(dest, ec);
    std::string work = dest + "/.tuxthrottle-download";
    fs::remove_all(work, ec);
    fs::create_directories(work, ec);
    auto cleanup = [&] { std::error_code e; fs::remove_all(work, e); };
    try {
        std::string arc = work + "/" + (*rel)["asset"].str();
        std::printf("%s %s \xE2\x86\x92 %s\n", tool.c_str(), (*rel)["tag"].str().c_str(), dest.c_str());
        if (!download((*rel)["url"].str(), arc, (*rel)["size"].as_number())) { std::puts("download failed"); cleanup(); return 1; }
        if (!(*rel)["checksum_url"].str().empty()) {
            auto published = http_get((*rel)["checksum_url"].str());
            if (!published || published->find(sha512_file(arc)) == std::string::npos) {
                std::puts("sha512 MISMATCH - the download is corrupt or was tampered with; nothing installed");
                cleanup();
                return 1;
            }
            std::puts("  sha512 verified");
        } else std::puts("  (this release publishes no checksum - not verified)");
        std::string top = extract(arc, work);
        std::string final_ = dest + "/" + top;
        if (fs::exists(final_, ec)) {
            if (!force) { std::printf("%s is already installed - nothing to do\n", top.c_str()); cleanup(); return 0; }
            fs::remove_all(final_, ec);
        }
        fs::rename(work + "/" + top, final_, ec);
        if (ec) throw std::runtime_error("cannot move into place: " + ec.message());
        std::string launcher = (*tgt)["launcher"].str();
        std::printf("installed %s%s\n", top.c_str(),
                    launcher == "steam" ? " - restart Steam, then pick it under Properties \xE2\x86\x92 Compatibility"
                                        : (" - restart " + split_ws((*tgt)["name"].str())[0] + " if it is open, so it rescans its tools folder").c_str());
        cleanup();
        return 0;
    } catch (const std::exception& e) {
        cleanup();
        std::printf("failed: %s\n", e.what());
        return 1;
    }
}

static int remove_build(const std::string& target, const std::string& name, const std::string& kind) {
    auto tgt = target_by_name(target);
    if (!tgt) { std::printf("unknown target '%s'\n", target.c_str()); return 1; }
    if (name.find('/') != std::string::npos || (!name.empty() && name[0] == '.')) { std::printf("'%s' is not a build name\n", name.c_str()); return 1; }
    std::vector<std::string> ks = kind.empty() ? kKinds : std::vector<std::string>{kind};
    for (const auto& k : ks) {
        if (!(*tgt)["dirs"].has(k)) continue;
        std::string d = (*tgt)["dirs"][k].str();
        auto inst = installed(d);
        if (std::find(inst.begin(), inst.end(), name) != inst.end()) {
            std::error_code ec;
            fs::remove_all(d + "/" + name, ec);
            std::printf("removed %s from %s\n", name.c_str(), (*tgt)["name"].str().c_str());
            return 0;
        }
    }
    std::printf("'%s' is not installed in %s\n", name.c_str(), (*tgt)["name"].str().c_str());
    return 1;
}

// ---------------------------------------------------------------- Steam manifests
struct Manifest { std::string name, lib, installdir, state, acf; long long size = 0; };

static std::map<std::string, Manifest> manifests() {
    std::map<std::string, Manifest> out;
    std::string root = steam_root_str();
    if (root.empty()) return out;
    for (const auto& lib : steam::all_libraries(root))
        for (const auto& acf : glob_paths(lib.string() + "/steamapps/appmanifest_*.acf")) {
            auto t = read_file(acf);
            if (!t) continue;
            std::smatch a, n, d, s, fl;
            static const std::regex ra(R"re("appid"\s+"(\d+)")re"), rn(R"re("name"\s+"([^"]*)")re"), rd(R"re("installdir"\s+"([^"]*)")re"),
                rs(R"re("SizeOnDisk"\s+"(\d+)")re"), rf(R"re("StateFlags"\s+"(\d+)")re");
            if (!std::regex_search(*t, a, ra)) continue;
            Manifest m;
            m.name = std::regex_search(*t, n, rn) ? n[1].str() : a[1].str();
            m.lib = lib.string();
            m.installdir = std::regex_search(*t, d, rd) ? d[1].str() : "";
            m.size = std::regex_search(*t, s, rs) ? std::stoll(s[1]) : 0;
            m.state = std::regex_search(*t, fl, rf) ? fl[1].str() : "4";
            m.acf = acf;
            out.emplace(a[1].str(), m);
        }
    return out;
}

static bool same_device(const std::string& a, const std::string& b) {
    struct stat x, y;
    return ::stat(a.c_str(), &x) == 0 && ::stat(b.c_str(), &y) == 0 && x.st_dev == y.st_dev;
}

Json steam_tools() {
    std::string root = steam_root_str();
    auto man = manifests();
    std::vector<std::pair<std::string, std::string>> known = kValveTools;
    static const std::regex tool_re(R"((Proton( |$)|Steam Linux Runtime))");
    for (const auto& [aid, m] : man) {
        bool have = false;
        for (const auto& k : known) have = have || k.first == aid;
        std::smatch mm;
        if (!have && std::regex_search(m.name, mm, tool_re) && mm.position() == 0) known.emplace_back(aid, m.name);
    }
    Json rows = Json::arr();
    for (const auto& [aid, name] : known) {
        auto it = man.find(aid);
        bool inst = it != man.end();
        Json r = Json::obj();
        r.set("appid", aid);
        r.set("name", inst ? it->second.name : name);
        r.set("installed", inst);
        r.set("ready", inst && it->second.state == "4");
        r.set("library", inst ? it->second.lib : "");
        r.set("size", inst ? static_cast<double>(it->second.size) : 0.0);
        r.set("on_os_drive", inst && !root.empty() && (it->second.lib == root || same_device(it->second.lib, root)));
        rows.push(r);
    }
    return rows;
}

// ---------------------------------------------------------------- session env / Steam control
static std::map<std::string, std::string> session_env() {
    std::map<std::string, std::string> env;
    for (char** e = environ; *e; ++e) { std::string s = *e; size_t eq = s.find('='); if (eq != std::string::npos) env[s.substr(0, eq)] = s.substr(eq + 1); }
    if (!env.count("XDG_RUNTIME_DIR")) env["XDG_RUNTIME_DIR"] = "/run/user/" + std::to_string(getuid());
    auto filled = [&](const char* k) { return env.count(k) && !env[k].empty(); };
    if (filled("DISPLAY") && filled("DBUS_SESSION_BUS_ADDRESS")) return env;
    std::string out = run_argv_stdout({"systemctl", "--user", "show-environment"}, 10000).out;
    for (const auto& ln : split_lines(out)) {
        size_t eq = ln.find('=');
        if (eq == std::string::npos) continue;
        std::string k = ln.substr(0, eq), v = ln.substr(eq + 1);
        static const char* vars[] = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP"};
        bool want = false;
        for (const char* x : vars) want = want || k == x;
        if (want && !filled(k.c_str())) env[k] = starts_with(v, "$'") && ends_with(v, "'") ? v.substr(2, v.size() - 3) : v;
    }
    std::string bus = env["XDG_RUNTIME_DIR"] + "/bus";
    if (!filled("DBUS_SESSION_BUS_ADDRESS") && path_exists(bus)) env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus;
    return env;
}

static std::vector<std::string> env_list(const std::map<std::string, std::string>& env) {
    std::vector<std::string> v;
    for (const auto& kv : env) v.push_back(kv.first + "=" + kv.second);
    return v;
}

static int steam_url(const std::string& url, const std::string& what) {
    if (which("steam").empty()) { std::puts("steam is not on PATH"); return 1; }
    auto env = session_env();
    if (env["DISPLAY"].empty() && env["WAYLAND_DISPLAY"].empty()) {
        std::puts("no graphical session found for this user (no DISPLAY / WAYLAND_DISPLAY) - log into the desktop first");
        return 1;
    }
    std::puts(what.c_str());
    std::vector<std::string> cmd{"steam", url};
    if (!which("systemd-run").empty() && !env["DBUS_SESSION_BUS_ADDRESS"].empty()) cmd = {"systemd-run", "--user", "--collect", "--quiet", "--", "steam", url};
    if (spawn_quiet(cmd, env_list(env), true) <= 0) { std::puts("could not start Steam"); return 1; }
    return 0;
}

static int start_steam() {
    auto env = session_env();
    if (env["DISPLAY"].empty() && env["WAYLAND_DISPLAY"].empty()) { std::puts("no graphical session found for this user - start Steam yourself"); return 1; }
    std::vector<std::string> cmd{"steam"};
    if (!which("systemd-run").empty() && !env["DBUS_SESSION_BUS_ADDRESS"].empty()) cmd = {"systemd-run", "--user", "--collect", "--quiet", "--", "steam"};
    spawn_quiet(cmd, env_list(env), true);
    std::puts("Steam is starting");
    return 0;
}

static int stop_steam(int wait = 90) {
    if (!steam::steam_running()) return 0;
    auto env = session_env();
    std::puts("asking Steam to shut down...");
    std::fflush(stdout);
    long pid = spawn_quiet({"steam", "-shutdown"}, env_list(env));
    double deadline = now_seconds() + 30;
    int rc;
    while (pid > 0 && child_running(pid, &rc) && now_seconds() < deadline) sleep_ms(200);
    deadline = now_seconds() + wait;
    while (steam::steam_running() && now_seconds() < deadline) sleep_ms(1000);
    if (steam::steam_running()) {
        std::printf("Steam did not exit within %ds (a game or a dialog may be holding it) - close it by hand, then try again\n", wait);
        return 1;
    }
    sleep_ms(2000);
    return 0;
}

static int restart_steam() {
    if (which("steam").empty()) { std::puts("steam is not on PATH"); return 1; }
    auto env = session_env();
    if (env["DISPLAY"].empty() && env["WAYLAND_DISPLAY"].empty()) { std::puts("no graphical session found for this user - log into the desktop first"); return 1; }
    if (steam::steam_running() && stop_steam() != 0) return 1;
    std::vector<std::string> cmd{"steam"};
    if (!which("systemd-run").empty() && !env["DBUS_SESSION_BUS_ADDRESS"].empty()) cmd = {"systemd-run", "--user", "--collect", "--quiet", "--", "steam"};
    spawn_quiet(cmd, env_list(env), true);
    std::puts("Steam is starting - queued tools download on their own (Steam \xE2\x86\x92 Downloads)");
    return 0;
}

static int steam_install(const std::string& appid) {
    return steam_url("steam://install/" + appid, "asking Steam to install AppID " + appid + " - Steam opens its install dialog; pick the library on your OS drive (" + steam_root_str() + ")");
}

// ---------------------------------------------------------------- appinfo.vdf
namespace {
unsigned u32(const std::string& b, size_t o) { unsigned v; std::memcpy(&v, b.data() + o, 4); return v; }
long long i64(const std::string& b, size_t o) { long long v; std::memcpy(&v, b.data() + o, 8); return v; }

struct AppBlob { unsigned appid; std::string blob; };

// returns false when the file is not a v29 (keyed) appinfo; names = key string table
bool appinfo_blobs(const std::string& b, std::vector<AppBlob>& out, std::vector<std::string>& names) {
    if (b.size() < 16 || u32(b, 0) != 0x07564429) return false;
    size_t toff = static_cast<size_t>(i64(b, 8));
    if (toff + 4 > b.size()) return false;
    unsigned n = u32(b, toff);
    size_t p = toff + 4;
    for (unsigned i = 0; i < n && p < b.size(); ++i) {
        size_t e = b.find('\0', p);
        if (e == std::string::npos) break;
        names.push_back(b.substr(p, e - p));
        p = e + 1;
    }
    size_t pos = 16;
    while (pos + 8 <= b.size()) {
        unsigned aid = u32(b, pos), size = u32(b, pos + 4);
        if (aid == 0) break;
        if (pos + 8 + size > b.size()) break;
        out.push_back({aid, b.substr(pos + 8, size)});
        pos += 8 + size;
    }
    return true;
}

std::string blob_str(const std::string& blob, const std::vector<std::string>& names, const std::string& key) {
    auto it = std::find(names.begin(), names.end(), key);
    if (it == names.end()) return "";
    unsigned idx = static_cast<unsigned>(it - names.begin());
    std::string k = std::string(1, '\x01') + std::string(reinterpret_cast<const char*>(&idx), 4);
    size_t i = blob.find(k);
    if (i == std::string::npos) return "";
    size_t e = blob.find('\0', i + 5);
    return e == std::string::npos ? "" : blob.substr(i + 5, e - i - 5);
}
}  // namespace

std::string appinfo_installdir_from(const std::string& b, const std::string& appid) {
    if (b.size() < 8) return "";
    unsigned magic = u32(b, 0);
    if (magic != 0x07564429 && magic != 0x07564428) return "";
    bool keyed = magic == 0x07564429;
    size_t pos = keyed ? 16 : 8;
    std::string key = std::string(1, '\x01') + "installdir" + std::string(1, '\0');
    if (keyed) {
        std::vector<AppBlob> blobs;
        std::vector<std::string> names;
        if (!appinfo_blobs(b, blobs, names)) return "";
        unsigned want = static_cast<unsigned>(std::strtoul(appid.c_str(), nullptr, 10));
        for (const auto& ab : blobs) if (ab.appid == want) return blob_str(ab.blob, names, "installdir");
        return "";
    }
    unsigned want = static_cast<unsigned>(std::strtoul(appid.c_str(), nullptr, 10));
    while (pos + 8 <= b.size()) {
        unsigned aid = u32(b, pos), size = u32(b, pos + 4);
        if (aid == 0) break;
        if (aid == want) {
            std::string blob = b.substr(pos + 8, size);
            size_t i = blob.find(key);
            if (i == std::string::npos) return "";
            size_t st = i + key.size(), e = blob.find('\0', st);
            return e == std::string::npos ? "" : blob.substr(st, e - st);
        }
        pos += 8 + size;
    }
    return "";
}

static std::string appinfo_installdir(const std::string& appid) {
    std::string root = steam_root_str();
    if (root.empty()) return "";
    return appinfo_installdir_from(read_file(root + "/appcache/appinfo.vdf").value_or(""), appid);
}

static std::set<std::string> native_linux_apps() {
    std::set<std::string> out;
    std::string root = steam_root_str();
    if (root.empty()) return out;
    std::vector<AppBlob> blobs;
    std::vector<std::string> names;
    if (!appinfo_blobs(read_file(root + "/appcache/appinfo.vdf").value_or(""), blobs, names)) return out;
    for (const auto& ab : blobs) {
        auto parts = std::string(blob_str(ab.blob, names, "oslist"));
        std::vector<std::string> oses;
        std::string cur;
        for (char c : parts + ",") { if (c == ',') { oses.push_back(cur); cur.clear(); } else cur += c; }
        if (std::find(oses.begin(), oses.end(), "linux") != oses.end()) out.insert(std::to_string(ab.appid));
    }
    return out;
}

static std::map<std::string, std::string> valve_tool_names() {
    std::map<std::string, std::string> out = kStaticToolNames;
    std::string root = steam_root_str();
    if (root.empty()) return out;
    std::vector<AppBlob> blobs;
    std::vector<std::string> names;
    if (!appinfo_blobs(read_file(root + "/appcache/appinfo.vdf").value_or(""), blobs, names)) return out;
    for (const auto& ab : blobs) {
        if (ab.appid != 891390) continue;
        auto it = std::find(names.begin(), names.end(), "appid");
        if (it == names.end()) break;
        unsigned ai = static_cast<unsigned>(it - names.begin());
        std::string ia = std::string(1, '\x02') + std::string(reinterpret_cast<const char*>(&ai), 4);
        for (size_t i = 0; i < names.size(); ++i) {
            if (!starts_with(names[i], "proton")) continue;
            unsigned idx = static_cast<unsigned>(i);
            std::string pat = std::string(1, '\0') + std::string(reinterpret_cast<const char*>(&idx), 4);
            size_t j = ab.blob.find(pat);
            if (j == std::string::npos) continue;
            size_t a = ab.blob.find(ia, j);
            if (a != std::string::npos && a < j + 400 && a + 9 <= ab.blob.size()) out[names[i]] = std::to_string(u32(ab.blob, a + 5));
        }
        break;
    }
    return out;
}

static int queue_install(const std::string& appid, bool dry) {
    std::string root = steam_root_str();
    if (root.empty()) { std::puts("no Steam install found"); return 1; }
    auto man = manifests();
    if (man.count(appid)) { std::printf("%s is already installed (or queued) in %s\n", man[appid].name.c_str(), man[appid].lib.c_str()); return 0; }
    std::string installdir = appinfo_installdir(appid);
    if (installdir.empty()) {
        std::printf("Steam's app cache doesn't know AppID %s's install folder yet - falling back to Steam's own install dialog\n", appid.c_str());
        return steam_install(appid);
    }
    std::string name = installdir;
    for (const auto& k : kValveTools) if (k.first == appid) name = k.second;
    std::string acf = root + "/steamapps/appmanifest_" + appid + ".acf";
    std::string text = "\"AppState\"\n{\n\t\"appid\"\t\t\"" + appid + "\"\n\t\"Universe\"\t\t\"1\"\n\t\"name\"\t\t\"" + name + "\"\n\t\"StateFlags\"\t\t\"1026\"\n\t\"installdir\"\t\t\"" + installdir + "\"\n}\n";
    std::printf("%s: queued in %s/steamapps (folder \xE2\x80\x9C%s\xE2\x80\x9D)\n", name.c_str(), root.c_str(), installdir.c_str());
    if (dry) return 0;
    write_file(acf, text);
    if (steam::steam_running())
        std::puts("Steam only reads its library when it starts, and it is running now - the download begins after Steam is restarted. Queue everything you want first, then restart Steam once (the tab has a button for it).");
    else std::puts("Steam downloads it by itself when it starts");
    return 0;
}

// ---------------------------------------------------------------- compat tool mapping
namespace {
struct Span { size_t start, end; std::string indent; };

std::optional<Span> map_span(const std::string& text) {
    static const std::regex head(R"re("CompatToolMapping"\s*\{)re");
    std::smatch m;
    if (!std::regex_search(text, m, head)) return std::nullopt;
    size_t i = static_cast<size_t>(m.position()) + static_cast<size_t>(m.length());
    int depth = 1;
    while (i < text.size() && depth) {
        char c = text[i];
        if (c == '"') {
            size_t q = text.find('"', i + 1);
            if (q == std::string::npos) return std::nullopt;
            i = q;
            while (i > 0 && text[i - 1] == '\\') { q = text.find('"', i + 1); if (q == std::string::npos) return std::nullopt; i = q; }
        } else if (c == '{') ++depth;
        else if (c == '}') --depth;
        ++i;
    }
    if (depth) return std::nullopt;
    size_t close = i - 1;
    size_t line_start = text.rfind('\n', close == 0 ? 0 : close - 1);
    std::string indent = (line_start == std::string::npos ? std::string() : text.substr(line_start + 1, close - line_start - 1)) + "\t";
    return Span{static_cast<size_t>(m.position()) + static_cast<size_t>(m.length()), line_start == std::string::npos ? close : line_start, indent};
}
}  // namespace

std::map<std::string, std::string> read_mapping(const std::string& text) {
    std::map<std::string, std::string> out;
    auto span = map_span(text);
    if (!span) return out;
    std::string body = text.substr(span->start, span->end - span->start);
    static const std::regex entry(R"re("(\d+)"\s*\{([^{}]*)\})re"), nm(R"re("name"\s*"([^"]*)")re");
    for (auto it = std::sregex_iterator(body.begin(), body.end(), entry); it != std::sregex_iterator(); ++it) {
        std::smatch n;
        std::string inner = (*it)[2];
        out[(*it)[1]] = std::regex_search(inner, n, nm) ? n[1].str() : "";
    }
    return out;
}

std::string write_mapping(const std::string& text, const std::map<std::string, std::string>& changes) {
    auto span = map_span(text);
    if (!span) throw std::runtime_error("no CompatToolMapping block in config.vdf - open Steam's Settings -> Compatibility once, then try again");
    std::string body = text.substr(span->start, span->end - span->start);
    std::set<std::string> done;
    auto entry_text = [&](const std::string& aid) {
        std::string prio = aid == "0" ? "75" : "250";
        const std::string& in = span->indent;
        return "\"" + aid + "\"\n" + in + "{\n" + in + "\t\"name\"\t\t\"" + changes.at(aid) + "\"\n" + in + "\t\"config\"\t\t\"\"\n" + in + "\t\"priority\"\t\t\"" + prio + "\"\n" + in + "}";
    };
    static const std::regex entry(R"re("(\d+)"\s*\{([^{}]*)\})re");
    body = regex_replace_fn(body, entry, [&](const std::smatch& m) {
        if (!changes.count(m[1])) return m[0].str();
        done.insert(m[1]);
        return entry_text(m[1]);
    });
    for (const auto& kv : changes) if (!done.count(kv.first)) body += "\n" + span->indent + entry_text(kv.first);
    return text.substr(0, span->start) + body + text.substr(span->end);
}

Json compat_names() {
    std::vector<std::pair<std::string, std::string>> rows;   // {name, label}
    auto man = manifests();
    for (const auto& [name, appid] : valve_tool_names())
        if (man.count(appid) && name != "proton-stable") rows.emplace_back(name, man[appid].name);
    auto tgt = target_by_name("Steam");
    if (tgt) {
        std::string dir = (*tgt)["dir"].str();
        for (const auto& d : installed(dir)) {
            std::string internal = d;
            auto t = read_file(dir + "/" + d + "/compatibilitytool.vdf");
            if (!t) continue;
            static const std::regex re(R"re("compat_tools"\s*\{\s*"([^"]+)")re");
            std::smatch m;
            if (std::regex_search(*t, m, re)) internal = m[1];
            rows.emplace_back(internal, d);
        }
    }
    std::stable_sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) {
        auto rank = [](const std::string& n) { return n == "proton_experimental" ? 0 : n == "proton_hotfix" ? 2 : 1; };
        if (rank(a.first) != rank(b.first)) return rank(a.first) < rank(b.first);
        return natural_less(a.second, b.second);
    });
    Json out = Json::arr();
    for (const auto& [n, l] : rows) out.push(obj_row({{"name", n}, {"label", l}}));
    return out;
}

static std::string config_vdf_path() {
    std::string root = steam_root_str();
    return root.empty() ? "" : root + "/config/config.vdf";
}

static std::map<std::string, std::string> default_plan(const std::string& tool, bool all_games) {
    std::string cfg = config_vdf_path();
    std::map<std::string, std::string> cur = !cfg.empty() && is_file(cfg) ? read_mapping(read_file(cfg).value_or("")) : std::map<std::string, std::string>{};
    std::map<std::string, std::string> plan;
    if (cur["0"] != tool || !cur.count("0")) plan["0"] = tool;
    if (all_games) {
        auto native = native_linux_apps();
        for (const auto& [aid, m] : manifests()) {
            if (native.count(aid) || is_dir("") ) continue;
            std::string l = lower(m.name);
            if ((starts_with(l, "proton") && (l.size() == 6 || l[6] == ' ')) || starts_with(l, "steam linux runtime") || starts_with(l, "steamworks common") || l == "steamvr") continue;
            auto it = cur.find(aid);
            if (it == cur.end() || it->second.empty() || it->second == "proton_hotfix") plan[aid] = tool;
        }
    }
    return plan;
}

static int set_default(const std::string& tool, bool all_games, bool dry, bool restart) {
    Json names = compat_names();
    std::string label;
    std::vector<std::string> known;
    for (const auto& r : names.array()) { known.push_back(r["name"].str()); if (r["name"].str() == tool) label = r["label"].str(); }
    if (label.empty()) { std::printf("'%s' is not an installed Proton build - one of: %s\n", tool.c_str(), join(known, ", ").c_str()); return 1; }
    std::string cfg = config_vdf_path();
    if (cfg.empty() || !is_file(cfg)) { std::puts("Steam's config.vdf not found"); return 1; }
    auto plan = default_plan(tool, all_games);
    std::map<std::string, std::string> nm;
    for (const auto& [a, m] : manifests()) nm[a] = m.name;
    std::printf("%s (%s):\n", label.c_str(), tool.c_str());
    if (plan.count("0")) std::puts("  - Steam's default for other titles (Settings -> Compatibility)");
    std::vector<std::string> ids;
    for (const auto& kv : plan) if (kv.first != "0") ids.push_back(kv.first);
    std::sort(ids.begin(), ids.end(), [&](const std::string& a, const std::string& b) { return lower(nm.count(a) ? nm[a] : a) < lower(nm.count(b) ? nm[b] : b); });
    for (const auto& a : ids) std::printf("  - %s\n", (nm.count(a) ? nm[a] : a).c_str());
    if (plan.empty()) { std::puts("  already set - nothing to change"); return 0; }
    if (dry) return 0;
    bool was_running = steam::steam_running();
    if (was_running) {
        if (!restart) { std::puts("Steam is running - it rewrites config.vdf when it exits, so this needs Steam closed (or use the restart option)."); return 2; }
        int rc = stop_steam();
        if (rc) return rc;
    }
    std::string text = read_file(cfg).value_or("");
    std::string nw;
    try { nw = write_mapping(text, plan); } catch (const std::exception& e) { std::puts(e.what()); return 1; }
    std::error_code ec;
    fs::copy_file(cfg, cfg + ".tuxthrottle-bak-" + std::to_string(std::time(nullptr)), fs::copy_options::overwrite_existing, ec);
    write_file(cfg, nw);
    std::printf("written (%zu entr%s); backup kept next to config.vdf\n", plan.size(), plan.size() == 1 ? "y" : "ies");
    return was_running ? start_steam() : 0;
}

static int move_to_os(const std::string& appid, bool dry) {
    std::string root = steam_root_str();
    auto man = manifests();
    if (root.empty() || !man.count(appid)) { std::printf("AppID %s is not installed\n", appid.c_str()); return 1; }
    const Manifest& m = man[appid];
    if (m.lib == root) { std::printf("%s is already in the OS-drive library\n", m.name.c_str()); return 0; }
    if (steam::steam_running()) { std::puts("Steam is running - quit Steam completely first (it owns the library while it runs)."); return 2; }
    std::string src = m.lib + "/steamapps/common/" + m.installdir, dst = root + "/steamapps/common/" + m.installdir;
    std::string acf_dst = root + "/steamapps/" + base_name(m.acf);
    if (m.installdir.empty() || !is_dir(src)) { std::printf("install folder not found: %s\n", src.c_str()); return 1; }
    std::error_code ec;
    if (fs::exists(dst, ec) || fs::exists(acf_dst, ec)) { std::printf("%s already exists - not overwriting\n", dst.c_str()); return 1; }
    struct statvfs sv;
    unsigned long long freeb = statvfs(root.c_str(), &sv) == 0 ? static_cast<unsigned long long>(sv.f_bavail) * sv.f_frsize : 0;
    if (static_cast<double>(freeb) < static_cast<double>(m.size) * 1.15) {
        std::printf("not enough free space on the OS drive: need ~%lld MiB, have %llu MiB\n", m.size >> 20, freeb >> 20);
        return 1;
    }
    std::printf("%s: %s  \xE2\x86\x92  %s  (%lld MiB)\n", m.name.c_str(), src.c_str(), dst.c_str(), m.size >> 20);
    if (dry) return 0;
    fs::create_directories(fs::path(dst).parent_path(), ec);
    fs::copy(src, dst, fs::copy_options::recursive | fs::copy_options::copy_symlinks, ec);
    if (ec) { std::printf("copy failed: %s\n", ec.message().c_str()); fs::remove_all(dst, ec); return 1; }
    fs::copy_file(m.acf, acf_dst, ec);
    ::unlink(m.acf.c_str());
    fs::remove_all(src, ec);
    std::printf("moved %s to the OS drive - start Steam to pick it up\n", m.name.c_str());
    return 0;
}

// ---------------------------------------------------------------- verify
static std::string fstype_of(const std::string& path) {
    std::string real = real_path(path), best, typ;
    for (const auto& ln : split_lines(read_file("/proc/mounts").value_or(""))) {
        auto parts = split_ws(ln);
        if (parts.size() < 3) continue;
        std::string mnt = replace_all(parts[1], "\\040", " ");
        std::string base = mnt;
        while (base.size() > 1 && base.back() == '/') base.pop_back();
        if ((real == mnt || starts_with(real, base + "/")) && mnt.size() > best.size()) { best = mnt; typ = parts[2]; }
    }
    return typ;
}

static double steam_started() {
    auto r = run_argv_stdout({"pgrep", "-ox", "steam"}, 3000);
    auto f = split_ws(r.out);
    if (f.empty()) return 0;
    struct stat st;
    return ::stat(("/proc/" + f[0]).c_str(), &st) == 0 ? static_cast<double>(st.st_mtime) : 0;
}

std::vector<std::string> check_build(const std::string& path, const std::string& kind) {
    std::vector<std::string> probs;
    std::error_code ec;
    auto any_file = [&](std::initializer_list<std::string> rel) { for (const auto& r : rel) if (is_file(path + "/" + r)) return true; return false; };
    if (kind == "proton") {
        if (is_file(path + "/proton")) {
            if (access((path + "/proton").c_str(), X_OK) != 0) probs.push_back("`proton` launcher is not executable");
            if (!is_file(path + "/toolmanifest.vdf")) probs.push_back("toolmanifest.vdf missing");
            if (!is_dir(path + "/files") && !is_dir(path + "/dist")) probs.push_back("no files/ (or dist/) folder - unpack incomplete");
        } else if (fs::exists(path + "/bin/wine", ec)) {
        } else if (!is_file(path + "/toolmanifest.vdf")) probs.push_back("neither a Proton build (no `proton`) nor a Steam tool (no toolmanifest.vdf)");
        if (is_file(path + "/toolmanifest.vdf") && !is_file(path + "/compatibilitytool.vdf") && base_name(dir_name(path)) == "compatibilitytools.d")
            probs.push_back("compatibilitytool.vdf missing - Steam won't list it");
    } else if (kind == "wine") {
        if (!fs::exists(path + "/bin/wine", ec) && !is_file(path + "/proton")) probs.push_back("bin/wine missing");
    } else if (kind == "dxvk") {
        if (!any_file({"x64/d3d11.dll", "x32/d3d11.dll"})) probs.push_back("x64/d3d11.dll missing");
    } else if (kind == "vkd3d") {
        if (!any_file({"x64/d3d12.dll", "x86/d3d12.dll"})) probs.push_back("x64/d3d12.dll missing");
    }
    return probs;
}

static std::vector<std::string> check_steam_tool(const Manifest& m) {
    std::vector<std::string> probs;
    auto t = read_file(m.acf);
    if (!t) return {"manifest unreadable"};
    static const std::regex fl(R"re("StateFlags"\s+"(\d+)")re");
    std::smatch s;
    if (std::regex_search(*t, s, fl) && s[1] != "4") probs.push_back("Steam reports it as not fully installed (StateFlags " + s[1].str() + ") - let Steam finish / verify it");
    std::string d = m.lib + "/steamapps/common/" + m.installdir;
    if (!is_dir(d)) { probs.push_back("install folder missing: " + d); return probs; }
    std::error_code ec;
    bool empty = fs::directory_iterator(d, ec) == fs::directory_iterator();
    static const std::regex proton_re(R"((Proton \d|Proton (Experimental|Hotfix)))");
    std::smatch pm;
    if (empty) probs.push_back("install folder is empty");
    else if (std::regex_search(m.name, pm, proton_re) && pm.position() == 0) {
        if (!is_file(d + "/proton")) probs.push_back("`proton` launcher missing");
        else if (access((d + "/proton").c_str(), X_OK) != 0) probs.push_back("`proton` launcher is not executable");
        if (!is_file(d + "/toolmanifest.vdf")) probs.push_back("toolmanifest.vdf missing");
    } else if (starts_with(m.name, "Steam Linux Runtime") && m.name.find("scout") == std::string::npos && !is_file(d + "/toolmanifest.vdf"))
        probs.push_back("toolmanifest.vdf missing");
    return probs;
}

Json verify() {
    Json items = Json::arr();
    double started = steam_started();
    int bad = 0;
    for (const auto& t : targets().array()) {
        if (!t["present"].as_bool()) continue;
        std::set<std::string> seen;
        for (const auto& kind : kKinds) {
            if (!t["dirs"].has(kind)) continue;
            std::string d = t["dirs"][kind].str();
            if (!seen.insert(d).second) continue;
            for (const auto& name : installed(d)) {
                std::string path = d + "/" + name;
                Json notes = Json::arr();
                if (t["launcher"].str() == "steam" && started) {
                    struct stat st;
                    if (::stat(path.c_str(), &st) == 0 && static_cast<double>(st.st_mtime) > started)
                        notes.push("installed after Steam started - restart Steam so it shows up under Compatibility");
                }
                auto probs = check_build(path, kind);
                Json pj = Json::arr();
                for (const auto& p : probs) pj.push(p);
                bad += !probs.empty();
                items.push(obj_row({{"what", name}, {"where", t["name"]}, {"ok", probs.empty()}, {"problems", pj}, {"notes", notes}}));
            }
        }
    }
    auto man = manifests();
    for (const auto& row : steam_tools().array()) {
        if (!row["installed"].as_bool()) continue;
        const Manifest& m = man[row["appid"].str()];
        auto probs = check_steam_tool(m);
        Json notes = Json::arr();
        std::string fs_ = fstype_of(m.lib);
        if (!row["on_os_drive"].as_bool()) notes.push("on another drive (" + m.lib + (fs_.empty() ? "" : ", " + fs_) + ") - \xE2\x80\x9CMove to OS drive\xE2\x80\x9D relocates it");
        if (fs_ == "ntfs" || fs_ == "ntfs3" || fs_ == "fuseblk" || fs_ == "exfat" || fs_ == "vfat")
            notes.push(fs_ + " can't keep Unix permissions or symlinks reliably - a Proton/runtime there is fragile");
        Json pj = Json::arr();
        for (const auto& p : probs) pj.push(p);
        bad += !probs.empty();
        items.push(obj_row({{"what", row["name"]}, {"where", "Steam tool"}, {"ok", probs.empty()}, {"problems", pj}, {"notes", notes}, {"appid", row["appid"]}}));
    }
    return obj_row({{"items", items}, {"bad", bad}});
}

Json status() {
    Json tg = Json::arr();
    for (auto t : targets().array()) {
        Json inst = Json::arr();
        std::set<std::string> seen;
        for (const auto& kind : kKinds) {
            if (!t["dirs"].has(kind)) continue;
            std::string d = t["dirs"][kind].str();
            if (!seen.insert(d).second) continue;
            for (const auto& n : installed(d)) inst.push(obj_row({{"name", n}, {"kind", kind}}));
        }
        t.set("installed", inst);
        tg.push(t);
    }
    std::string cfg = config_vdf_path();
    auto mapping = cfg.empty() ? std::map<std::string, std::string>{} : read_mapping(read_file(cfg).value_or(""));
    Json forced = Json::obj();
    for (const auto& kv : mapping) if (kv.first != "0") forced.set(kv.first, kv.second);
    Json tl = Json::obj();
    for (const auto& t : tools()) {
        Json l = Json::arr();
        for (const auto& x : t.launchers) l.push(x);
        tl.set(t.name, obj_row({{"launchers", l}, {"about", t.about}, {"kind", t.kind}}));
    }
    return obj_row({{"targets", tg}, {"steam_tools", steam_tools()}, {"steam_root", steam_root_str()}, {"compat_names", compat_names()},
                    {"compat_default", mapping.count("0") ? mapping["0"] : ""}, {"compat_forced", forced}, {"steam_running", steam::steam_running()}, {"tools", tl}});
}

int main(const std::vector<std::string>& a) {
    if (is_root() && env_or("TUXTHROTTLE_ALLOW_ROOT").empty()) { std::fputs("run this as your normal user, not root\n", stderr); return 1; }
    if (a.empty()) { std::puts("usage: compat {status|releases <tool>|install <tool>|remove <target> <name>|verify|steam-validate|queue-install|restart-steam|set-default|steam-install|move-to-os}"); return 2; }
    const std::string& cmd = a[0];
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    auto opt = [&](const char* f, const std::string& def) { for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1]; return def; };
    std::vector<std::string> pos;
    for (size_t i = 1; i < a.size(); ++i) {
        if (a[i] == "--tag" || a[i] == "--target" || a[i] == "--count" || a[i] == "--kind") { ++i; continue; }
        if (!starts_with(a[i], "--")) pos.push_back(a[i]);
    }
    bool as_json = has("--json"), dry = has("--dry-run");
    try {
        if (cmd == "status") {
            Json s = status();
            if (as_json) { std::puts(s.dump().c_str()); return 0; }
            for (const auto& t : s["targets"].array()) {
                if (!t["present"].as_bool()) continue;
                std::printf("[%s] %s\n", t["name"].str().c_str(), t["dir"].str().c_str());
                for (const auto& n : t["installed"].array()) std::printf("    %s  (%s)\n", n["name"].str().c_str(), n["kind"].str().c_str());
            }
            std::puts("[Steam tools]");
            for (const auto& r : s["steam_tools"].array()) {
                std::string where = !r["installed"].as_bool() ? "not installed" : r["on_os_drive"].as_bool() ? "OS drive" : "OTHER DRIVE: " + r["library"].str();
                std::printf("    %-8s %-36s %s\n", r["appid"].str().c_str(), r["name"].str().c_str(), where.c_str());
            }
            return 0;
        }
        if (cmd == "releases") {
            if (pos.empty() || !find_tool(pos[0])) { std::fputs("unknown tool\n", stderr); return 1; }
            auto rels = releases(pos[0], std::atoi(opt("--count", "15").c_str()));
            if (as_json) { Json arr = Json::arr(); for (const auto& r : rels) arr.push(r); std::puts(arr.dump().c_str()); return 0; }
            for (const auto& r : rels) std::printf("  %-28s %s  %lld MiB  %s\n", r["tag"].str().c_str(), r["date"].str().c_str(), static_cast<long long>(r["size"].as_number()) >> 20, r["asset"].str().c_str());
            return 0;
        }
        if (cmd == "install") { if (pos.empty()) return 2; return install(pos[0], opt("--tag", ""), opt("--target", "Steam"), has("--force")); }
        if (cmd == "remove") { if (pos.size() < 2) return 2; return remove_build(pos[0], pos[1], opt("--kind", "")); }
        if (cmd == "verify") {
            Json res = verify();
            int bad = static_cast<int>(res["bad"].as_number());
            if (as_json) { std::puts(res.dump().c_str()); return bad ? 1 : 0; }
            for (const auto& i : res["items"].array()) {
                std::printf("  [%s] %s  (%s)\n", i["ok"].as_bool() ? "ok " : "BAD", i["what"].str().c_str(), i["where"].str().c_str());
                for (const auto& p : i["problems"].array()) std::printf("          \xE2\x9C\x97 %s\n", p.str().c_str());
                for (const auto& n : i["notes"].array()) std::printf("          \xC2\xB7 %s\n", n.str().c_str());
            }
            size_t n = res["items"].array().size();
            std::printf("%zu/%zu OK%s\n", n - static_cast<size_t>(bad), n, bad ? (", " + std::to_string(bad) + " with problems").c_str() : "");
            return bad ? 1 : 0;
        }
        if (cmd == "restart-steam") return restart_steam();
        if (cmd == "set-default") { if (pos.empty()) return 2; return set_default(pos[0], has("--all-games"), dry, has("--restart-steam")); }
        if (pos.empty() || !std::all_of(pos[0].begin(), pos[0].end(), [](char c) { return c >= '0' && c <= '9'; })) { std::fputs("appid must be numeric\n", stderr); return 1; }
        if (cmd == "steam-install") return steam_install(pos[0]);
        if (cmd == "queue-install") return queue_install(pos[0], dry);
        if (cmd == "steam-validate") return steam_url("steam://validate/" + pos[0], "asking Steam to verify AppID " + pos[0] + " - progress shows in Steam's Downloads page");
        if (cmd == "move-to-os") return move_to_os(pos[0], dry);
    } catch (const std::exception& e) {
        std::printf("failed: %s\n", e.what());
        return 1;
    }
    return 2;
}

}  // namespace tt::compat
