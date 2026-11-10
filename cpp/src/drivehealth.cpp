#include "drivehealth.hpp"

#include <pwd.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <regex>
#include <set>

#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::drivehealth {

namespace {
const char* kLevels[] = {"ok", "note", "warn", "bad"};
int level_idx(const std::string& l) {
    for (int i = 0; i < 4; ++i) if (l == kLevels[i]) return i;
    return 0;
}
const std::set<std::string> kRealFs = {"btrfs", "ext4", "ext3", "ext2", "xfs", "f2fs", "ntfs", "ntfs3", "fuseblk", "exfat", "vfat"};
const std::set<std::string> kNtfs = {"ntfs", "ntfs3", "fuseblk"};

struct LogPat { const char* label; const char* pat; };
const LogPat kLogPatterns[] = {
    {"I/O errors", "i/o error|buffer i/o error|blk_update_request|critical medium error"},
    {"NVMe timeouts / resets", "nvme.*(timeout|reset|abort|controller is down)"},
    {"SATA link errors", "ata[0-9]+.*(failed command|hard resetting|exception emask|link is slow)"},
    {"Btrfs errors", "btrfs.*(error|corrupt|csum failed|parent transid)"},
    {"ext4 errors", "ext4-fs error|ext4-fs warning.*corrupt"},
    {"XFS errors", "xfs.*(corruption|metadata i/o error|internal error)"},
    {"NTFS errors", "ntfs3?.*(error|corrupt|dirty|failed)"},
};

std::pair<int, std::string> run(const std::vector<std::string>& cmd, int timeout_s = 60) {
    auto r = run_argv(cmd, timeout_s * 1000);
    if (r.code == -1) return {127, "cannot run " + cmd[0]};
    return {r.code, r.out};
}

long long ll(const Json& v) { return static_cast<long long>(v.as_number()); }
bool has_num(const Json& v) { return v.is_number(); }

std::string gib(long long b) { return std::to_string(b >> 30); }
}  // namespace

Json finding(const std::string& level, const std::string& text) {
    Json o = Json::obj();
    o.set("level", level);
    o.set("text", text);
    return o;
}

std::string worst(const Json& findings) {
    int w = 0;
    for (const auto& f : findings.array()) w = std::max(w, level_idx(f["level"].str()));
    return kLevels[w];
}

std::pair<Json, Json> smart_findings(const Json& data) {
    Json f = Json::arr(), summ = Json::obj();
    const Json& st = data["smart_status"]["passed"];
    if (st.is_bool() && !st.as_bool()) f.push(finding("bad", "SMART overall health: FAILED - back up now and replace the drive"));
    else if (!st.is_bool()) f.push(finding("note", "the drive did not report a SMART health verdict"));
    const Json& temp = data["temperature"]["current"];
    if (has_num(temp)) summ.set("temperature_c", temp);
    if (has_num(data["power_on_time"]["hours"])) summ.set("power_on_hours", data["power_on_time"]["hours"]);
    const Json& nv = data["nvme_smart_health_information_log"];
    if (nv.is_object()) {
        for (const char* k : {"percentage_used", "available_spare", "media_errors", "unsafe_shutdowns"})
            summ.set(std::string(std::strcmp(k, "percentage_used") == 0 ? "wear_percent" : std::strcmp(k, "available_spare") == 0 ? "spare_percent" : k), nv[k]);
        long long cw = ll(nv["critical_warning"]);
        if (cw) {
            std::vector<std::string> bits;
            static const std::pair<int, const char*> names[] = {{1, "spare below threshold"}, {2, "temperature"}, {4, "reliability degraded"},
                                                                {8, "read-only mode"}, {16, "volatile backup failed"}};
            for (auto& [b, n] : names) if (cw & b) bits.push_back(n);
            char hx[16];
            std::snprintf(hx, sizeof hx, "0x%llx", cw);
            f.push(finding("bad", "NVMe critical warning: " + join(bits.empty() ? std::vector<std::string>{hx} : bits, ", ")));
        }
        if (ll(nv["media_errors"]) > 0)
            f.push(finding("bad", std::to_string(ll(nv["media_errors"])) + " media / data-integrity errors - data was read back wrong at least once"));
        if (has_num(nv["available_spare"])) {
            long long spare = ll(nv["available_spare"]), thr = has_num(nv["available_spare_threshold"]) && ll(nv["available_spare_threshold"]) ? ll(nv["available_spare_threshold"]) : 10;
            if (spare <= thr) f.push(finding("bad", "spare blocks at " + std::to_string(spare) + "% (threshold " + std::to_string(thr) + "%)"));
            else if (spare < 30) f.push(finding("warn", "spare blocks down to " + std::to_string(spare) + "%"));
        }
        if (has_num(nv["percentage_used"])) {
            long long used = ll(nv["percentage_used"]);
            if (used >= 100) f.push(finding("bad", "rated endurance used up (" + std::to_string(used) + "%)"));
            else if (used >= 85) f.push(finding("warn", std::to_string(used) + "% of rated endurance used"));
        }
        if (has_num(temp) && ll(temp) >= 75) f.push(finding("warn", "running hot: " + std::to_string(ll(temp)) + " °" "C"));
        if (ll(nv["unsafe_shutdowns"]) >= 100)
            f.push(finding("note", std::to_string(ll(nv["unsafe_shutdowns"])) +
                                       " unsafe shutdowns (power lost without a clean flush) - some drives also count deep power-saving here"));
        if (ll(nv["num_err_log_entries"]) > 0)
            f.push(finding("note", std::to_string(ll(nv["num_err_log_entries"])) + " entries in the drive's error log (often harmless unsupported-command replies)"));
    }
    struct Watch { int id; const char* name; const char* level; };
    static const Watch watch[] = {{5, "reallocated sectors", "bad"}, {187, "reported uncorrectable errors", "bad"}, {197, "sectors pending reallocation", "bad"},
                                  {198, "offline-uncorrectable sectors", "bad"}, {199, "CRC (cable) errors", "warn"}};
    for (const auto& a : data["ata_smart_attributes"]["table"].array()) {
        long long aid = ll(a["id"]), raw = ll(a["raw"]["value"]);
        for (const auto& w : watch)
            if (aid == w.id && raw > 0) {
                f.push(finding(w.level, std::to_string(raw) + " " + w.name));
                std::string key = w.name;
                std::replace(key.begin(), key.end(), ' ', '_');
                summ.set(key, static_cast<double>(raw));
            }
        if ((aid == 177 || aid == 231 || aid == 233) && has_num(a["value"]) && ll(a["value"]) <= 10)
            f.push(finding("warn", "SSD wear indicator low (" + a["name"].str() + ": " + std::to_string(ll(a["value"])) + ")"));
    }
    if (f.array().empty()) f.push(finding("ok", "SMART: no problems reported"));
    return {summ, f};
}

Json usage_finding(double used_pct, long long free_bytes) {
    char b[96];
    if (used_pct >= 97) { std::snprintf(b, sizeof b, "%.0f%% full (%s GiB free)", used_pct, gib(free_bytes).c_str()); return finding("bad", b); }
    if (used_pct >= 90) { std::snprintf(b, sizeof b, "%.0f%% full (%s GiB free)", used_pct, gib(free_bytes).c_str()); return finding("warn", b); }
    return Json();
}

Json btrfs_stats_findings(const std::string& text) {
    Json f = Json::arr();
    static const std::regex re(R"(\[(\S+)\]\.(\w+)\s+(\d+))");
    for (auto it = std::sregex_iterator(text.begin(), text.end(), re); it != std::sregex_iterator(); ++it)
        if (std::stoll((*it)[3]) > 0) f.push(finding("bad", "Btrfs " + (*it)[2].str() + " = " + (*it)[3].str() + " on " + (*it)[1].str()));
    return f;
}

Json btrfs_scrub_finding(const std::string& text) {
    if (text.find("no stats available") != std::string::npos)
        return finding("note", "never scrubbed - a scrub reads every block and verifies its checksum (“Start scrub”)");
    std::smatch m, started, status;
    static const std::regex es(R"(Error summary:\s*(.+))"), ss(R"(Scrub started:\s*(.+))"), st(R"(Status:\s*(\w+))");
    bool hm = std::regex_search(text, m, es), hs = std::regex_search(text, started, ss), ht = std::regex_search(text, status, st);
    std::string when = hs ? " (started " + trim(started[1]) + ")" : "";
    if (ht && status[1] == "running") return finding("note", "scrub running" + when);
    if (hm && m[1].str().find("no errors") == std::string::npos) return finding("bad", "last scrub found errors: " + trim(m[1]) + when);
    return finding("ok", "last scrub clean" + when);
}

Json scan_log(const std::string& text) {
    Json rows = Json::arr();
    auto lines = split_lines(text);
    for (const auto& lp : kLogPatterns) {
        std::regex rx(lp.pat, std::regex::icase | std::regex::extended);
        std::vector<std::string> hits;
        for (const auto& ln : lines) if (std::regex_search(ln, rx)) hits.push_back(trim(ln));
        if (hits.empty()) continue;
        Json r = Json::obj();
        r.set("label", lp.label);
        r.set("count", static_cast<double>(hits.size()));
        Json last = Json::arr();
        for (size_t i = hits.size() > 3 ? hits.size() - 3 : 0; i < hits.size(); ++i) last.push(hits[i]);
        r.set("last", last);
        r.set("level", std::string(lp.label) == "NTFS errors" ? "warn" : "bad");
        rows.push(r);
    }
    return rows;
}

Json trend_finding(long long boots, long long unsafe, long long, double days) {
    if (boots <= 0) return Json();
    char span[96];
    std::snprintf(span, sizeof span, "over %lld boot(s), %.0f day(s)", boots, days);
    if (unsafe <= 0) return finding("ok", std::string("unsafe shutdowns have not gone up ") + span);
    if (unsafe >= boots)
        return finding("warn", "unsafe shutdowns went up by " + std::to_string(unsafe) + " " + span +
                                   " - about one per boot, so the drive is losing power before it is told to shut down (at power-off or in suspend). "
                                   "No data is lost while filesystems are synced first, but it is worth a BIOS / drive firmware update");
    return finding("note", "unsafe shutdowns went up by " + std::to_string(unsafe) + " " + span);
}

// per-drive unsafe-shutdown history (one sample per boot)
static const char* kHistory = "/var/lib/tuxthrottle/drive-counters.json";

static Json track(const std::string& key, long long unsafe, long long cycles) {
    Json hist = read_json_file(kHistory);
    Json rows = hist[key].is_array() ? hist[key] : Json::arr();
    std::string boot = read_trim("/proc/sys/kernel/random/boot_id");
    double now = static_cast<double>(std::time(nullptr));
    Json::Array arr = rows.array();
    if (!arr.empty() && arr.back()["boot"].str() == boot) {
        Json last = arr.back();
        last.set("unsafe", static_cast<double>(unsafe));
        last.set("cycles", static_cast<double>(cycles));
        last.set("ts", now);
        arr.back() = last;
    } else {
        Json r = Json::obj();
        r.set("boot", boot);
        r.set("unsafe", static_cast<double>(unsafe));
        r.set("cycles", static_cast<double>(cycles));
        r.set("ts", now);
        arr.push_back(r);
    }
    while (arr.size() > 60) arr.erase(arr.begin());
    Json out = Json::arr();
    for (auto& r : arr) out.push(r);
    hist.set(key, out);
    mkdir_p(dir_name(kHistory));
    write_file(kHistory, hist.dump());
    Json res = Json::obj();
    if (arr.size() < 2) return res;
    const Json& first = arr.front();
    res.set("boots", static_cast<double>(arr.size() - 1));
    res.set("unsafe", static_cast<double>(unsafe) - first["unsafe"].as_number());
    res.set("cycles", static_cast<double>(cycles) - first["cycles"].as_number());
    res.set("days", std::max(0.0, (now - first["ts"].as_number()) / 86400));
    return res;
}

// ------------------------------------------------------------ drives + filesystems
static Json drives() {
    auto [rc, out] = run({"lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,TRAN,ROTA,RM,FSTYPE,MOUNTPOINT,LABEL"});
    Json devs;
    try { if (rc == 0) devs = Json::parse(out)["blockdevices"]; } catch (const std::exception&) {}
    Json res = Json::arr();
    auto num = [](const Json& v) { return v.is_number() ? v.as_number() : (v.is_string() ? std::atof(v.as_string().c_str()) : 0.0); };
    auto truthy = [&](const Json& v) { return v.is_bool() ? v.as_bool() : num(v) != 0; };
    for (const auto& d : devs.array()) {
        std::string name = d["name"].str();
        if (d["type"].str() != "disk" || starts_with(name, "zram") || starts_with(name, "loop") || starts_with(name, "ram")) continue;
        Json parts = Json::arr();
        for (const auto& c : d["children"].array()) {
            Json p = Json::obj();
            p.set("dev", "/dev/" + c["name"].str());
            p.set("fstype", c["fstype"].str());
            p.set("mount", c["mountpoint"].str());
            p.set("label", c["label"].str());
            p.set("size", num(c["size"]));
            parts.push(p);
        }
        Json o = Json::obj();
        o.set("dev", "/dev/" + name);
        o.set("model", trim(d["model"].str()));
        o.set("size", num(d["size"]));
        o.set("tran", d["tran"].str());
        o.set("rotational", truthy(d["rota"]));
        o.set("removable", truthy(d["rm"]));
        o.set("parts", parts);
        res.push(o);
    }
    return res;
}

static std::pair<Json, Json> smart(const std::string& dev) {
    if (which("smartctl").empty()) return {Json::obj(), Json::arr()};
    auto [rc, out] = run({"smartctl", "-j", "-H", "-A", "-i", dev}, 40);
    Json data;
    try { data = Json::parse(out); } catch (const std::exception&) {
        Json f = Json::arr();
        f.push(finding("note", "could not read SMART data (needs root?)"));
        return {Json::obj(), f};
    }
    std::vector<std::string> msgs;
    for (const auto& m : data["smartctl"]["messages"].array()) msgs.push_back(m["string"].str());
    if (!data.has("smart_status") && !data.has("nvme_smart_health_information_log") && !data.has("ata_smart_attributes")) {
        Json f = Json::arr();
        f.push(finding("note", "no SMART data: " + (msgs.empty() ? "exit " + std::to_string(rc) : join(msgs, "; "))));
        return {Json::obj(), f};
    }
    auto [summ, f] = smart_findings(data);
    if (is_root() && summ["unsafe_shutdowns"].is_number()) {
        std::string key = data["serial_number"].str().empty() ? dev : data["serial_number"].str();
        long long cycles = has_num(data["power_cycle_count"]) ? ll(data["power_cycle_count"]) : ll(data["nvme_smart_health_information_log"]["power_cycles"]);
        Json t = track(key, ll(summ["unsafe_shutdowns"]), cycles);
        if (t.has("boots")) {
            Json tf = trend_finding(ll(t["boots"]), ll(t["unsafe"]), ll(t["cycles"]), t["days"].as_number());
            if (!tf.is_null()) f.push(tf);
        }
    }
    return {summ, f};
}

struct Mount { std::string dev, mount, fstype; std::vector<std::string> opts; };

static std::vector<Mount> mounts() {
    std::vector<Mount> out;
    std::set<std::string> seen;
    for (const auto& ln : split_lines(read_file("/proc/mounts").value_or(""))) {
        auto p = split_ws(ln);
        if (p.size() < 4 || !kRealFs.count(p[2]) || !starts_with(p[0], "/dev/") || seen.count(p[0])) continue;
        seen.insert(p[0]);
        Mount m{p[0], replace_all(p[1], "\\040", " "), p[2], {}};
        std::string cur;
        for (char c : p[3] + ",") { if (c == ',') { m.opts.push_back(cur); cur.clear(); } else cur += c; }
        out.push_back(m);
    }
    return out;
}

static bool fstab_wants_rw(const std::string& mount) {
    for (const auto& ln : split_lines(read_file("/etc/fstab").value_or(""))) {
        auto p = split_ws(ln);
        if (p.size() >= 4 && !starts_with(trim(ln), "#") && p[1] == mount) {
            std::string cur;
            for (char c : p[3] + ",") {
                if (c == ',') { if (cur == "ro") return false; cur.clear(); } else cur += c;
            }
            return true;
        }
    }
    return true;
}

static std::optional<bool> ntfs_dirty_flag(const std::string& dev) {
    FILE* fh = std::fopen(dev.c_str(), "rb");
    if (!fh) return std::nullopt;
    unsigned char boot[512];
    if (std::fread(boot, 1, 512, fh) != 512 || std::memcmp(boot + 3, "NTFS    ", 8) != 0) { std::fclose(fh); return std::nullopt; }
    auto u16 = [](const unsigned char* p) { return static_cast<unsigned>(p[0] | (p[1] << 8)); };
    unsigned bps = u16(boot + 11), spc = boot[13];
    unsigned long long cluster = static_cast<unsigned long long>(bps) * (spc < 0x80 ? spc : (1u << (256 - spc)));
    long long mft_lcn;
    std::memcpy(&mft_lcn, boot + 0x30, 8);
    int c = static_cast<signed char>(boot[0x40]);
    unsigned long long rec_size = c > 0 ? static_cast<unsigned long long>(c) * cluster : (1ull << -c);
    if (rec_size == 0 || rec_size > (1 << 16)) { std::fclose(fh); return std::nullopt; }
    std::vector<unsigned char> rec(rec_size);
    if (fseeko(fh, static_cast<off_t>(static_cast<unsigned long long>(mft_lcn) * cluster + 3 * rec_size), SEEK_SET) != 0 ||
        std::fread(rec.data(), 1, rec_size, fh) != rec_size) { std::fclose(fh); return std::nullopt; }
    std::fclose(fh);
    if (std::memcmp(rec.data(), "FILE", 4) != 0) return std::nullopt;
    unsigned uso = u16(&rec[4]), usn = u16(&rec[6]);
    for (unsigned i = 1; i < usn; ++i) {
        unsigned end = i * bps;
        if (end <= rec.size() && uso + 2 * i + 2 <= rec.size()) { rec[end - 2] = rec[uso + 2 * i]; rec[end - 1] = rec[uso + 2 * i + 1]; }
    }
    unsigned off = u16(&rec[0x14]);
    while (off + 16 <= rec.size()) {
        unsigned atype, alen;
        std::memcpy(&atype, &rec[off], 4);
        std::memcpy(&alen, &rec[off + 4], 4);
        if (atype == 0xFFFFFFFFu || alen == 0) break;
        if (atype == 0x70 && rec[off + 8] == 0) {
            unsigned voff = u16(&rec[off + 0x14]);
            unsigned flags = u16(&rec[off + voff + 10]);
            return (flags & 1) != 0;
        }
        off += alen;
    }
    return std::nullopt;
}

static Json filesystems() {
    Json rows = Json::arr();
    std::optional<std::string> boot_log;
    for (const auto& m : mounts()) {
        Json f = Json::arr();
        struct statvfs sv;
        bool have = statvfs(m.mount.c_str(), &sv) == 0;
        double total = have ? static_cast<double>(sv.f_blocks) * static_cast<double>(sv.f_frsize) : 0;
        double freeb = have ? static_cast<double>(sv.f_bavail) * static_cast<double>(sv.f_frsize) : 0;
        double used = have ? (static_cast<double>(sv.f_blocks) - static_cast<double>(sv.f_bfree)) * static_cast<double>(sv.f_frsize) : 0;
        double pct = total > 0 ? used * 100.0 / total : 0;
        if (have) {
            Json uf = usage_finding(pct, static_cast<long long>(freeb));
            if (!uf.is_null()) f.push(uf);
        }
        if (std::find(m.opts.begin(), m.opts.end(), "ro") != m.opts.end() && fstab_wants_rw(m.mount))
            f.push(finding("bad", "mounted READ-ONLY although it should be writable - the kernel usually does that after a filesystem error"));
        if (m.fstype == "btrfs") {
            for (const auto& x : btrfs_stats_findings(run({"btrfs", "device", "stats", m.mount}).second).array()) f.push(x);
            f.push(btrfs_scrub_finding(run({"btrfs", "scrub", "status", m.mount}).second));
        } else if (starts_with(m.fstype, "ext")) {
            std::string out = run({"tune2fs", "-l", m.dev}).second;
            std::smatch st;
            static const std::regex re(R"(Filesystem state:\s*(.+))");
            if (std::regex_search(out, st, re)) {
                std::string s = st[1];
                if (s.find("clean") == std::string::npos) f.push(finding("bad", "ext filesystem state: " + trim(s)));
                if (s.find("error") != std::string::npos) f.push(finding("bad", "errors recorded - run fsck from a live system"));
            }
        } else if (kNtfs.count(m.fstype)) {
            if (!boot_log) boot_log = run({"journalctl", "-k", "-b", "-o", "cat", "--no-pager"}, 30).second;
            std::string name = base_name(m.dev);
            std::regex re("ntfs3?.*" + name + ".*dirty", std::regex::icase);
            if (std::regex_search(*boot_log, re))
                f.push(finding("warn", "was marked dirty when it was mounted (Windows Fast Startup / unclean shutdown) - “Repair NTFS” clears it; a full check is chkdsk /f from Windows"));
            if (std::find(m.opts.begin(), m.opts.end(), "force") != m.opts.end())
                f.push(finding("note", "mounted with `force`: it mounts even when dirty, so a dirty volume would not stop it - hard power-offs are the real risk here"));
        }
        bool bad = false;
        for (const auto& x : f.array()) bad = bad || x["level"].str() == "warn" || x["level"].str() == "bad";
        if (!bad) {
            Json nf = Json::arr();
            nf.push(finding("ok", "no filesystem problems found"));
            for (const auto& x : f.array()) nf.push(x);
            f = nf;
        }
        Json r = Json::obj();
        r.set("dev", m.dev);
        r.set("mount", m.mount);
        r.set("fstype", m.fstype);
        Json opts = Json::arr();
        for (const auto& o : m.opts) opts.push(o);
        r.set("opts", opts);
        r.set("used_pct", std::round(pct * 10) / 10);
        r.set("free", freeb);
        r.set("total", total);
        r.set("findings", f);
        rows.push(r);
    }
    return rows;
}

static Json unmounted_ntfs() {
    std::set<std::string> mounted;
    for (const auto& m : mounts()) mounted.insert(m.dev);
    Json rows = Json::arr();
    for (const auto& d : drives().array())
        for (const auto& p : d["parts"].array()) {
            if (p["fstype"].str() != "ntfs" || mounted.count(p["dev"].str())) continue;
            auto dirty = ntfs_dirty_flag(p["dev"].str());
            Json f = Json::arr();
            if (dirty && *dirty) f.push(finding("warn", "dirty flag set - “Repair NTFS” clears it"));
            else if (dirty) f.push(finding("ok", "clean"));
            else f.push(finding("note", "could not read the volume flags"));
            Json r = Json::obj();
            r.set("dev", p["dev"].str());
            r.set("label", p["label"].str());
            r.set("findings", f);
            rows.push(r);
        }
    return rows;
}

static Json steam_libraries(const std::string& user) {
    struct passwd* pw = getpwnam(user.c_str());
    Json rows = Json::arr();
    if (!pw) return rows;
    for (const std::string& root : {std::string(pw->pw_dir) + "/.local/share/Steam", std::string(pw->pw_dir) + "/.steam/steam"}) {
        std::string vdf = root + "/steamapps/libraryfolders.vdf";
        auto text = read_file(vdf);
        if (!text) continue;
        static const std::regex re(R"re("path"\s*"([^"]+)")re");
        for (auto it = std::sregex_iterator(text->begin(), text->end(), re); it != std::sregex_iterator(); ++it) {
            std::string p = (*it)[1];
            Json f = Json::arr();
            std::string sa = p + "/steamapps";
            struct stat st;
            if (::stat(sa.c_str(), &st) != 0 || !S_ISDIR(st.st_mode)) f.push(finding("bad", "library folder is missing - drive not mounted?"));
            else {
                if (st.st_uid != pw->pw_uid && !(st.st_mode & 0002)) f.push(finding("warn", "not owned by " + user + " - Steam may be unable to write"));
                struct statvfs sv;
                if (statvfs(sa.c_str(), &sv) == 0) {
                    unsigned long long freeb = static_cast<unsigned long long>(sv.f_bavail) * sv.f_frsize;
                    if (freeb < (10ull << 30)) f.push(finding("warn", "only " + std::to_string(freeb >> 30) + " GiB free"));
                }
                std::string sc = sa + "/shadercache";
                struct stat ls, ts;
                if (::lstat(sc.c_str(), &ls) == 0 && S_ISLNK(ls.st_mode) && ::stat(sc.c_str(), &ts) != 0)
                    f.push(finding("bad", "shadercache link is broken - Steam shows “disk write error” (Game Tools → Link Steam cache repairs it)"));
            }
            if (f.array().empty()) f.push(finding("ok", "mounted and writable"));
            Json r = Json::obj();
            r.set("path", p);
            r.set("findings", f);
            rows.push(r);
        }
        break;
    }
    return rows;
}

Json fix_plan(const Json& rep) {
    Json plan = Json::arr();
    auto step = [&](const char* action, const std::string& arg, const std::string& text) {
        Json s = Json::obj();
        s.set("action", action);
        s.set("arg", arg);
        s.set("text", text);
        plan.push(s);
    };
    for (const auto& m : rep["filesystems"].array()) {
        std::string texts;
        for (const auto& f : m["findings"].array()) texts += f["text"].str() + " ";
        auto has = [&](const char* s) { return texts.find(s) != std::string::npos; };
        std::string fs = m["fstype"].str();
        if (fs == "btrfs" && (has("never scrubbed") || has("Btrfs ") || has("last scrub found errors")))
            step("scrub", m["mount"].str(), "start a Btrfs scrub of " + m["mount"].str());
        if (kNtfs.count(fs) && has("was marked dirty")) step("ntfs-repair", m["dev"].str(), "repair NTFS on " + m["dev"].str() + " (" + m["mount"].str() + ")");
        if (has("READ-ONLY"))
            step("manual", m["mount"].str(), m["mount"].str() + " went read-only: reboot, and if it comes back run a filesystem check from a live system");
        for (const auto& f : m["findings"].array())
            if (f["text"].str().find("% full") != std::string::npos) step("manual", m["mount"].str(), m["mount"].str() + " is " + f["text"].str() + ": free up space");
    }
    for (const auto& n : rep["ntfs_unmounted"].array())
        for (const auto& f : n["findings"].array())
            if (f["text"].str().find("dirty flag set") != std::string::npos) { step("ntfs-repair", n["dev"].str(), "repair NTFS on " + n["dev"].str()); break; }
    for (const auto& s : rep["steam"].array()) {
        bool broken = false;
        for (const auto& f : s["findings"].array()) broken = broken || f["text"].str().find("shadercache link is broken") != std::string::npos;
        if (broken) { step("heal-shadercache", "", "repair Steam's broken shadercache link"); break; }
    }
    for (const auto& d : rep["drives"].array())
        for (const auto& f : d["findings"].array())
            if (f["level"].str() == "bad")
                step("manual", d["dev"].str(), d["dev"].str() + ": " + f["text"].str() + " - no software fix; back up and plan a replacement");
    bool any_ssd = false;
    for (const auto& d : rep["drives"].array()) any_ssd = any_ssd || !d["rotational"].as_bool();
    if (any_ssd) step("trim", "", "TRIM every SSD filesystem");
    return plan;
}

Json report(const std::string& user) {
    Json drv = drives();
    Json out_drv = Json::arr();
    for (auto d : drv.array()) {
        auto [s, f] = smart(d["dev"].str());
        d.set("smart", s);
        d.set("findings", f);
        out_drv.push(d);
    }
    Json rep = Json::obj();
    rep.set("drives", out_drv);
    rep.set("filesystems", filesystems());
    rep.set("ntfs_unmounted", unmounted_ntfs());
    rep.set("kernel_log", scan_log(run({"journalctl", "-k", "-b", "-o", "short", "--no-pager"}, 30).second));
    rep.set("steam", user.empty() ? Json::arr() : steam_libraries(user));
    int bad = 0, warn = 0;
    auto tally = [&](const Json& findings) {
        for (const auto& f : findings.array()) { bad += f["level"].str() == "bad"; warn += f["level"].str() == "warn"; }
    };
    for (const auto& d : rep["drives"].array()) tally(d["findings"]);
    for (const char* s : {"filesystems", "ntfs_unmounted", "steam"}) for (const auto& r : rep[s].array()) tally(r["findings"]);
    for (const auto& k : rep["kernel_log"].array()) { bad += k["level"].str() == "bad"; warn += k["level"].str() == "warn"; }
    rep.set("bad", bad);
    rep.set("warn", warn);
    rep.set("fix_plan", fix_plan(rep));
    return rep;
}

std::string format_report(const Json& rep) {
    std::string out;
    auto show = [&](const Json& findings) {
        static const std::map<std::string, std::string> mark = {{"ok", "ok  "}, {"note", "note"}, {"warn", "WARN"}, {"bad", "BAD "}};
        for (const auto& f : findings.array()) out += "      [" + mark.at(f["level"].str()) + "] " + f["text"].str() + "\n";
    };
    char b[512];
    for (const auto& d : rep["drives"].array()) {
        std::vector<std::string> extra;
        for (const auto& kv : d["smart"].object()) {
            if (kv.second.is_null()) continue;
            std::string k = kv.first;
            std::replace(k.begin(), k.end(), '_', ' ');
            extra.push_back(k + " " + (kv.second.is_string() ? kv.second.as_string() : kv.second.dump()));
        }
        std::snprintf(b, sizeof b, "  %s  %s  %lld GiB  %s\n", d["dev"].str().c_str(), d["model"].str().c_str(), ll(d["size"]) >> 30, join(extra, ", ").c_str());
        out += b;
        show(d["findings"]);
    }
    for (const auto& m : rep["filesystems"].array()) {
        std::snprintf(b, sizeof b, "  %s  (%s on %s, %s%% used)\n", m["mount"].str().c_str(), m["fstype"].str().c_str(), m["dev"].str().c_str(),
                      format_double(m["used_pct"].as_number(), 1).c_str());
        out += b;
        show(m["findings"]);
    }
    for (const auto& n : rep["ntfs_unmounted"].array()) { out += "  " + n["dev"].str() + "  (NTFS, not mounted)\n"; show(n["findings"]); }
    for (const auto& k : rep["kernel_log"].array()) {
        out += "  kernel log: " + k["label"].str() + " ×" + std::to_string(ll(k["count"])) + "\n";
        for (const auto& l : k["last"].array()) out += "      " + l.str().substr(0, 160) + "\n";
    }
    for (const auto& s : rep["steam"].array()) { out += "  Steam library " + s["path"].str() + "\n"; show(s["findings"]); }
    out += std::to_string(ll(rep["bad"])) + " problem(s), " + std::to_string(ll(rep["warn"])) + " warning(s)\n";
    return out;
}

// ------------------------------------------------------------ actions
static int scrub(const std::string& mount) {
    bool ok = false;
    for (const auto& m : mounts()) ok = ok || (m.mount == mount && m.fstype == "btrfs");
    if (!ok) { std::printf("%s is not a mounted Btrfs filesystem\n", mount.c_str()); return 1; }
    auto [rc, out] = run({"btrfs", "scrub", "start", mount}, 30);
    std::puts(trim(out).empty() ? "scrub started" : trim(out).c_str());
    if (rc == 0) std::puts("it runs in the background (minutes to hours); press “" "Check drives” again to see the result");
    return rc;
}

static int scrub_all() {
    int rc = 0;
    for (const auto& m : mounts()) if (m.fstype == "btrfs") rc |= scrub(m.mount);
    return rc;
}

static int trim_all() {
    auto [rc, out] = run({"fstrim", "-av"}, 600);
    std::puts(trim(out).c_str());
    return rc;
}

static int selftest(const std::string& dev) {
    auto [rc, out] = run({"smartctl", "-t", "short", dev}, 60);
    std::string t;
    for (const auto& l : split_lines(out)) if (!trim(l).empty()) t += l + "\n";
    if (t.size() > 600) t = t.substr(t.size() - 600);
    std::puts(t.c_str());
    return rc == 0 || rc == 4 ? 0 : rc;
}

static int ntfs_repair(const std::string& dev) {
    bool is_ntfs = false;
    for (const auto& d : drives().array())
        for (const auto& p : d["parts"].array()) if (p["dev"].str() == dev && p["fstype"].str() == "ntfs") is_ntfs = true;
    if (!is_ntfs) { std::printf("%s is not an NTFS partition\n", dev.c_str()); return 1; }
    if (which("ntfsfix").empty()) { std::puts("ntfsfix not installed (package ntfsprogs / ntfs-3g)"); return 1; }
    std::string mount;
    for (const auto& m : mounts()) if (m.dev == dev) mount = m.mount;
    if (!mount.empty()) {
        auto [rc, out] = run({"umount", mount}, 60);
        if (rc != 0) {
            std::printf("cannot unmount %s: %s\nsomething is using it - close Steam, games and file managers open on that drive, then try again\n", mount.c_str(), trim(out).c_str());
            return 2;
        }
        std::printf("unmounted %s\n", mount.c_str());
    }
    auto [rc, out] = run({"ntfsfix", "-d", dev}, 600);
    std::puts(trim(out).c_str());
    if (!mount.empty()) {
        auto [mrc, mout] = run({"mount", mount}, 60);
        if (mrc == 0) std::printf("mounted %s again\n", mount.c_str());
        else std::printf("remount failed: %s\n", trim(mout).c_str());
        rc = rc ? rc : mrc;
    }
    return rc;
}

static int fix_all(const std::string& user) {
    Json rep = report(user);
    int rc = 0;
    bool any_auto = false;
    for (const auto& s : rep["fix_plan"].array()) {
        std::string action = s["action"].str();
        if (action == "manual") continue;
        any_auto = true;
        std::printf("== %s\n", s["text"].str().c_str());
        std::fflush(stdout);
        if (action == "scrub") rc |= scrub(s["arg"].str());
        else if (action == "ntfs-repair") {
            int r = ntfs_repair(s["arg"].str());
            if (r == 2) std::puts("   skipped (drive is in use)");
            rc |= r == 2 ? 0 : r;
        } else if (action == "trim") rc |= trim_all();
        else if (action == "heal-shadercache" && !user.empty()) {
            auto [r, out] = run({"su", "-", user, "-c", shell_quote(ctl_exe()) + " shadercache heal"}, 60);
            std::printf("   %s\n", trim(out).c_str());
            rc |= r;
        }
    }
    bool manual = false;
    for (const auto& s : rep["fix_plan"].array()) if (s["action"].str() == "manual") manual = true;
    if (manual) {
        std::puts("== needs you:");
        for (const auto& s : rep["fix_plan"].array()) if (s["action"].str() == "manual") std::printf("   - %s\n", s["text"].str().c_str());
    }
    if (!any_auto) std::puts("nothing to fix automatically");
    std::puts("lifetime counters (unsafe shutdowns, error-log entries, wear) are history kept by the drive - they cannot be cleared, only watched");
    return rc;
}

static void notify_user(const std::string& user, const Json& rep) {
    if (!(ll(rep["bad"]) || ll(rep["warn"])) || which("notify-send").empty()) return;
    struct passwd* pw = getpwnam(user.c_str());
    if (!pw) return;
    std::vector<std::string> lines;
    for (const auto& d : rep["drives"].array()) for (const auto& f : d["findings"].array()) if (f["level"].str() == "warn" || f["level"].str() == "bad") lines.push_back(f["text"].str());
    for (const auto& m : rep["filesystems"].array()) for (const auto& f : m["findings"].array()) if (f["level"].str() == "warn" || f["level"].str() == "bad") lines.push_back(m["mount"].str() + ": " + f["text"].str());
    for (const auto& k : rep["kernel_log"].array()) lines.push_back("kernel log: " + k["label"].str() + " ×" + std::to_string(ll(k["count"])));
    if (lines.size() > 6) lines.resize(6);
    std::string body = lines.empty() ? "see TuxThrottle → Drives" : join(lines, "\n");
    std::string rd = "/run/user/" + std::to_string(pw->pw_uid);
    std::vector<std::string> cmd{"notify-send", "-a", "TuxThrottle", "-u", ll(rep["bad"]) ? "critical" : "normal", "-i", "drive-harddisk",
                                 "Drive health: " + std::to_string(ll(rep["bad"])) + " problem(s), " + std::to_string(ll(rep["warn"])) + " warning(s)", body};
    if (is_root()) cmd.insert(cmd.begin(), {"sudo", "-u", user, "env", "XDG_RUNTIME_DIR=" + rd, "DBUS_SESSION_BUS_ADDRESS=unix:path=" + rd + "/bus"});
    run(cmd, 20);
}

int main(const std::vector<std::string>& a) {
    if (a.empty()) { std::puts("usage: drives {report [--json] [--user U] | check --notify USER | scrub MOUNT | scrub-all | fix-all [--user U] | trim | selftest DEV | ntfs-repair DEV}"); return 2; }
    auto opt = [&](const char* f, const std::string& def) {
        for (size_t i = 0; i + 1 < a.size(); ++i) if (a[i] == f) return a[i + 1];
        return def;
    };
    std::string user = opt("--user", env_or("SUDO_USER"));
    const std::string& cmd = a[0];
    if (cmd == "report") {
        Json rep = report(user);
        if (std::find(a.begin(), a.end(), "--json") != a.end()) std::puts(rep.dump().c_str());
        else std::fputs(format_report(rep).c_str(), stdout);
        return 0;
    }
    if (cmd == "check") {
        std::string u = opt("--notify", "");
        Json rep = report(u);
        notify_user(u, rep);
        return ll(rep["bad"]) ? 1 : 0;
    }
    if (cmd == "scrub" && a.size() > 1) return scrub(a[1]);
    if (cmd == "scrub-all") return scrub_all();
    if (cmd == "fix-all") return fix_all(user);
    if (cmd == "trim") return trim_all();
    if (cmd == "selftest" && a.size() > 1) return selftest(a[1]);
    if (cmd == "ntfs-repair" && a.size() > 1) return ntfs_repair(a[1]);
    return 2;
}

}  // namespace tt::drivehealth
