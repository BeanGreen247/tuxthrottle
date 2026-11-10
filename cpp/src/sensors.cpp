#include "sensors.hpp"

#include <fcntl.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <dlfcn.h>
#include <map>
#include <mutex>
#include <regex>
#include <set>

#include "util.hpp"

namespace tt::sensors {

// Results that cost a process spawn or a sysfs/D-Bus sweep are kept for a while: the UI asks for them every frame.
template <class T>
struct Memo {
    std::mutex m;
    bool has = false;
    double t = 0;
    T v{};
    template <class F>
    T get(double ttl, F f) {
        std::lock_guard<std::mutex> g(m);
        double now = now_seconds();
        if (has && (ttl < 0 || now - t < ttl)) return v;
        v = f();
        t = now;
        has = true;
        return v;
    }
};


namespace {
std::string g_session_user;

std::string dmi(const std::string& name) { return read_trim("/sys/class/dmi/id/" + name); }

std::vector<std::string> quoted_fields(const std::string& line) {
    std::vector<std::string> out;
    size_t i = 0;
    while ((i = line.find('"', i)) != std::string::npos) {
        size_t j = line.find('"', i + 1);
        if (j == std::string::npos) break;
        out.push_back(line.substr(i + 1, j - i - 1));
        i = j + 1;
    }
    return out;
}

bool gpu_class(const std::string& cls) {
    std::string c = lower(cls);
    return c.find("vga compatible controller") != std::string::npos || c.find("3d controller") != std::string::npos ||
           c.find("display controller") != std::string::npos;
}

std::string env_str(const char* n) { return env_or(n); }

std::optional<double> to_double(const std::string& s) {
    std::string t = trim(s);
    if (t.empty()) return std::nullopt;
    char* end = nullptr;
    double v = std::strtod(t.c_str(), &end);
    if (end == t.c_str() || *end != '\0') return std::nullopt;
    return v;
}

std::optional<int> opt_round(const std::optional<double>& v) {
    return v ? std::optional<int>(static_cast<int>(std::lround(*v))) : std::nullopt;
}

std::vector<std::string> split_commas(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s + ",") {
        if (c == ',') { out.push_back(trim(cur)); cur.clear(); }
        else cur += c;
    }
    return out;
}

Result blob_result(const RunResult& r, const char* fallback) {
    std::string t = trim(r.out);
    return {r.code == 0, r.code == 0 ? "" : (t.empty() ? fallback : t)};
}
}  // namespace

std::string app_dir() {
    std::string e = env_or("TUXTHROTTLE_HOME");
    if (!e.empty()) return e;
    if (path_exists("/opt/tuxthrottle/models")) return "/opt/tuxthrottle";
    std::string self = real_path("/proc/self/exe");
    for (std::string d = dir_name(self); d != "/" && d != "."; d = dir_name(d))
        if (path_exists(d + "/models") && path_exists(d + "/config")) return d;
    return "/opt/tuxthrottle";
}

std::string battery_dir();
std::string dmi_value(const std::string& name) { return dmi(name); }
std::string battery_dir_with_threshold() { return battery_dir(); }

// ---------------------------------------------------------------- model
ModelInfo detect_model() {
    ModelInfo m;
    m.vendor = dmi("sys_vendor");
    m.product = dmi("product_name");
    m.board = dmi("board_name");
    m.bios = dmi("bios_version");
    if (m.product.empty()) m.product = "unknown";
    m.is_target = m.product == "Dell G15 5515" || m.board == "0R3CDX";
    m.is_close = !m.is_target && (m.product.find("G15 5515") != std::string::npos || starts_with(m.product, "Dell G15"));
    return m;
}

const Json& model_profile() {
    static Json cached;
    static bool have = false;
    if (have) return cached;
    std::string forced = trim(env_or("TUXTHROTTLE_MODEL"));
    std::string product = dmi("product_name"), board = dmi("board_name");
    Json fallback;
    {
        Json::Object o;
        o.emplace_back("id", "g15-5515");
        o.emplace_back("name", "Dell G15 5515");
        o.emplace_back("match", Json(Json::Object{}));
        fallback = Json(std::move(o));
    }
    std::optional<Json> chosen;
    for (const auto& path : glob_paths(app_dir() + "/models/*.json")) {
        bool underscore = starts_with(base_name(path), "_");
        if (underscore && forced.empty()) continue;
        Json prof;
        try {
            prof = Json::parse(read_file(path).value_or(""));
        } catch (const std::exception&) {
            continue;
        }
        if (!forced.empty() && prof["id"].str() == forced) {
            std::cerr << "sensors: TUXTHROTTLE_MODEL override active - using '" << forced << "' profile\n";
            cached = prof;
            have = true;
            return cached;
        }
        const Json& match = prof["match"];
        auto contains = [](const Json& arr, const std::string& v) {
            for (const auto& x : arr.array()) if (x.str() == v) return true;
            return false;
        };
        if (forced.empty() && ((!product.empty() && contains(match["product_name"], product)) ||
                               (!board.empty() && contains(match["board_name"], board)))) {
            chosen = prof;
            break;
        }
        if (prof["id"].str() == "g15-5515") fallback = prof;
    }
    if (!forced.empty()) std::cerr << "sensors: TUXTHROTTLE_MODEL='" << forced << "' set but no such models/*.json - falling back\n";
    cached = chosen ? *chosen : fallback;
    have = true;
    return cached;
}

std::string model_id() {
    std::string id = model_profile()["id"].str();
    return id.empty() ? "g15-5515" : id;
}

bool model_allows(const std::vector<std::string>& models) {
    if (models.empty()) return true;
    return std::find(models.begin(), models.end(), model_id()) != models.end();
}

bool model_skips_tweak(const std::string& tweak_id) {
    for (const auto& x : model_profile()["tweaks_skip"].array())
        if (x.str() == tweak_id) return true;
    return false;
}

static const Json& prof_section(const char* name) { return model_profile()[name]; }
static std::string cpu_temp_hwmon() { auto s = prof_section("cpu")["hwmon"].str(); return s.empty() ? "k10temp" : s; }
static std::string fan_hwmon() { auto s = prof_section("fans")["hwmon"].str(); return s.empty() ? "alienware_wmi" : s; }
static std::string fan_pwm_hwmon() { auto s = prof_section("fans")["pwm_hwmon"].str(); return s.empty() ? "dell_smm" : s; }
static std::vector<int> fan_indices() {
    const Json& f = prof_section("fans");
    int n = static_cast<int>(f["count"].as_number());
    if (n <= 0) n = static_cast<int>(f["rpm_inputs"].array().size());
    if (n <= 0) n = 2;
    std::vector<int> v;
    for (int i = 1; i <= n; ++i) v.push_back(i);
    return v;
}
static std::string platform_profile_path() {
    auto s = prof_section("fans")["platform_profile_path"].str();
    return s.empty() ? "/sys/firmware/acpi/platform_profile" : s;
}
static int pwm_floor() { int v = static_cast<int>(prof_section("fans")["pwm_floor"].as_number()); return v > 0 ? v : PWM_FLOOR; }
static std::string fan_boost_attr(int i) {
    const auto& names = prof_section("fans")["additive_boost"].array();
    if (i >= 1 && i <= static_cast<int>(names.size()) && !names[static_cast<size_t>(i - 1)].str().empty())
        return names[static_cast<size_t>(i - 1)].str();
    return "fan" + std::to_string(i) + "_boost";
}
static int fan_rpm_max() { int v = static_cast<int>(prof_section("fans")["rpm_max"].as_number()); return v > 0 ? v : 4700; }
static std::string game_mode_value() {
    auto s = prof_section("game_mode")["value"].str();
    return s.empty() ? "performance" : s;
}

// ---------------------------------------------------------------- CPU
std::optional<double> cpu_freq_ghz() {
    long long best = -1;
    for (const auto& p : glob_paths("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
        if (auto v = read_int(p)) best = std::max(best, *v);
    if (best < 0) return std::nullopt;
    return static_cast<double>(best) / 1e6;
}

std::string hwmon_by_name(const std::string& name) {
    for (const auto& np : glob_paths("/sys/class/hwmon/hwmon*/name"))
        if (read_trim(np) == name) return dir_name(np);
    return "";
}

std::optional<double> cpu_temp_c() {
    std::string want = cpu_temp_hwmon();
    for (const auto& np : glob_paths("/sys/class/hwmon/hwmon*/name")) {
        if (read_trim(np) != want) continue;
        if (auto v = read_int(dir_name(np) + "/temp1_input")) return static_cast<double>(*v) / 1000.0;
    }
    return std::nullopt;
}

std::string cpu_model_name() {
    for (const auto& line : split_lines(read_file("/proc/cpuinfo").value_or("")))
        if (starts_with(line, "model name")) {
            size_t c = line.find(':');
            if (c != std::string::npos) return trim(line.substr(c + 1));
        }
    return "";
}

static bool cpu_is_amd_raw() { return read_file("/proc/cpuinfo").value_or("").substr(0, 4096).find("AuthenticAMD") != std::string::npos; }

static std::vector<std::string> powercap_energy_paths_raw() {
    std::vector<std::string> out;
    for (const auto& np : glob_paths("/sys/class/powercap/*/name")) {
        std::string n = lower(read_trim(np));
        if (n.find("package") != std::string::npos || n.find("core") != std::string::npos) {
            std::string e = dir_name(np) + "/energy_uj";
            if (path_exists(e)) out.push_back(e);
        }
    }
    return out;
}

bool cpu_is_amd() { static const bool v = cpu_is_amd_raw(); return v; }

static const std::vector<std::string>& powercap_energy_paths() { static const std::vector<std::string> v = powercap_energy_paths_raw(); return v; }

std::optional<double> cpu_power_watts() {
    // Callers that sample repeatedly (the dashboard / tray) reuse the previous reading as the baseline instead of
    // sleeping 100 ms every time; the first call, or one after a long gap, still measures over 100 ms.
    static std::mutex mu;
    static long long last_e = -1;
    static double last_t = 0;
    auto paths0 = powercap_energy_paths();
    if (paths0.empty()) return std::nullopt;
    {
        long long t = 0;
        bool ok = true;
        for (const auto& p : paths0) { auto v = read_int(p); if (!v) { ok = false; break; } t += *v; }
        double now = now_seconds();
        std::lock_guard<std::mutex> g(mu);
        if (ok) {
            if (last_e >= 0 && now - last_t >= 0.5 && now - last_t <= 15.0 && t >= last_e) {
                double w = (static_cast<double>(t - last_e) / 1e6) / (now - last_t);
                last_e = t; last_t = now;
                return w;
            }
            last_e = t; last_t = now;
        }
    }
    auto paths = powercap_energy_paths();
    if (paths.empty()) return std::nullopt;
    auto total = [&]() -> std::optional<long long> {
        long long t = 0;
        for (const auto& p : paths) {
            auto v = read_int(p);
            if (!v) return std::nullopt;
            t += *v;
        }
        return t;
    };
    auto e1 = total();
    double t1 = now_seconds();
    sleep_ms(100);
    auto e2 = total();
    double t2 = now_seconds();
    if (!e1 || !e2 || *e2 < *e1) return std::nullopt;
    return (static_cast<double>(*e2 - *e1) / 1e6) / (t2 - t1);
}

bool rapl_permissions_ok() {
    auto paths = powercap_energy_paths();
    if (paths.empty()) return true;
    struct stat st;
    if (::stat(paths[0].c_str(), &st) != 0) return false;
    return (st.st_mode & S_IROTH) != 0;
}

std::optional<std::string> amd_pstate_mode() {
    std::string s = read_trim("/sys/devices/system/cpu/amd_pstate/status");
    if (s.empty()) return std::nullopt;
    return s;
}

// ---------------------------------------------------------------- GPUs
std::string amdgpu_card_dir() {
    for (const auto& c : glob_paths("/sys/class/drm/card[0-9]*/device"))
        if (read_trim(c + "/vendor") == "0x1002") return c;
    return "";
}

bool has_amd_gpu() { static const bool v = !amdgpu_card_dir().empty(); return v; }   // hardware does not change while we run

bool has_nvidia_gpu() {
    // PCI scan first (no process, no GPU wake-up); nvidia-smi only as a fallback. Cached: this is asked on every UI frame.
    static const bool v = [] {
        for (const auto& p : glob_paths("/sys/bus/pci/devices/*/vendor"))
            if (read_trim(p) == "0x10de") return true;
        if (!which("nvidia-smi").empty()) {
            auto r = run_argv_stdout({"nvidia-smi", "-L"}, 5000);
            if (r.code == 0 && !trim(r.out).empty()) return true;
        }
        return false;
    }();
    return v;
}

static std::string trim_gpu_name(std::string s) {
    static const std::regex re(R"(\s*\((?:rev|prog-if)[^)]*\)|\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]|\s*\bCorporation\b|\s*\bTechnology\b|\s*\bInc\.?)",
                               std::regex::icase);
    s = std::regex_replace(s, re, "");
    size_t a = s.find_first_not_of(" ,-"), b = s.find_last_not_of(" ,-");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

static std::string pretty_gpu_name(const std::string& raw) {
    std::string dev = trim_gpu_name(raw);
    size_t o = dev.find('[');
    if (o != std::string::npos) {
        size_t c = dev.find(']', o);
        if (c != std::string::npos) dev = trim(dev.substr(o + 1, c - o - 1));
    }
    size_t s = dev.find(" / ");
    if (s != std::string::npos) dev = trim(dev.substr(0, s));
    return dev;
}

static std::vector<std::string> gpu_names_raw() {
    std::vector<std::string> names;
    bool nvidia_seen = false;
    if (!which("nvidia-smi").empty()) {
        auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=name", "--format=csv,noheader"}, 5000);
        for (const auto& ln : split_lines(r.out)) {
            std::string n = trim(ln);
            if (!n.empty() && std::find(names.begin(), names.end(), n) == names.end()) {
                names.push_back(n);
                nvidia_seen = true;
            }
        }
    }
    if (!which("lspci").empty()) {
        auto r = run_argv_stdout({"lspci", "-mm"}, 6000);
        for (const auto& ln : split_lines(r.out)) {
            auto parts = quoted_fields(ln);
            if (parts.size() < 3 || !gpu_class(parts[0])) continue;
            if (nvidia_seen && starts_with(lower(parts[1]), "nvidia")) continue;
            std::string dev = pretty_gpu_name(parts[2]);
            if (!dev.empty() && std::find(names.begin(), names.end(), dev) == names.end()) names.push_back(dev);
        }
    }
    return names;
}

std::vector<std::string> gpu_names() { static Memo<std::vector<std::string>> memo; return memo.get(-1, [] { return gpu_names_raw(); }); }

std::string norm_pci(const std::string& addr) {
    std::string a = lower(trim(addr));
    if (a.empty()) return "";
    std::vector<std::string> parts;
    std::string cur;
    for (char c : a + ":") {
        if (c == ':') { parts.push_back(cur); cur.clear(); }
        else cur += c;
    }
    if (parts.size() == 2) return "0000:" + a;
    char* end = nullptr;
    unsigned long dom = std::strtoul(parts[0].c_str(), &end, 16);
    if (end == parts[0].c_str() || *end) return a;
    char buf[16];
    std::snprintf(buf, sizeof buf, "%04lx", dom);
    std::string out = buf;
    for (size_t i = 1; i < parts.size(); ++i) out += ":" + parts[i];
    return out;
}

std::string nvidia_pci_dir() {
    for (const auto& dev : glob_paths("/sys/bus/pci/devices/*"))
        if (read_trim(dev + "/vendor") == "0x10de" && starts_with(read_trim(dev + "/class"), "0x03")) return dev;
    return "";
}

bool dgpu_is_awake() {
    std::string dev = nvidia_pci_dir();
    if (dev.empty()) return false;
    auto t = read_file(dev + "/power/runtime_status");
    if (!t) return true;
    return trim(*t) == "active";
}

static std::vector<GpuDevice> gpu_devices_raw() {
    std::vector<GpuDevice> devs;
    std::set<std::string> seen;
    bool nvidia_seen = false;
    if (!which("nvidia-smi").empty() && dgpu_is_awake()) {
        auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=name,pci.bus_id", "--format=csv,noheader"}, 5000);
        for (const auto& ln : split_lines(r.out)) {
            size_t c = ln.find(',');
            if (c == std::string::npos) continue;
            std::string name = trim(ln.substr(0, c));
            if (!name.empty() && !seen.count(name)) {
                devs.push_back({name, norm_pci(ln.substr(c + 1))});
                seen.insert(name);
                nvidia_seen = true;
            }
        }
    }
    if (!which("lspci").empty()) {
        auto r = run_argv_stdout({"lspci", "-Dmm"}, 6000);
        for (const auto& ln : split_lines(r.out)) {
            if (trim(ln).empty()) continue;
            std::string slot = ln.substr(0, ln.find_first_of(" \t"));
            auto parts = quoted_fields(ln);
            if (parts.size() < 3 || !gpu_class(parts[0])) continue;
            if (nvidia_seen && starts_with(lower(parts[1]), "nvidia")) {
                for (auto& d : devs)
                    if (d.pci.empty() && lower(d.name).find("nvidia") != std::string::npos) d.pci = norm_pci(slot);
                continue;
            }
            std::string dev = pretty_gpu_name(parts[2]);
            if (!dev.empty() && !seen.count(dev)) {
                devs.push_back({dev, norm_pci(slot)});
                seen.insert(dev);
            }
        }
    }
    return devs;
}

std::vector<GpuDevice> gpu_devices() { static Memo<std::vector<GpuDevice>> memo; return memo.get(-1, [] { return gpu_devices_raw(); }); }

IgpuValues igpu_values() {
    IgpuValues v;
    std::string card = amdgpu_card_dir();
    if (card.empty()) return v;
    static const std::regex mhz(R"((\d+)Mhz)");
    for (const auto& line : split_lines(read_file(card + "/pp_dpm_sclk").value_or(""))) {
        if (line.find('*') == std::string::npos) continue;
        std::smatch m;
        if (std::regex_search(line, m, mhz)) v.clock_mhz = std::stoi(m[1]);
    }
    for (const auto& p : glob_paths(card + "/hwmon/hwmon*/temp1_input"))
        if (auto t = read_int(p)) v.temp_c = static_cast<double>(*t) / 1000.0;
    return v;
}

// NVML, loaded at run time: reads the four dashboard values in-process instead of spawning nvidia-smi every sample.
// Init + query + shutdown on each call so no device handle stays open while the GPU is meant to be allowed to sleep.
namespace {
struct Nvml {
    void* lib = nullptr;
    int (*init)() = nullptr;
    int (*shutdown)() = nullptr;
    int (*handle)(unsigned, void**) = nullptr;
    int (*temp)(void*, int, unsigned*) = nullptr;
    int (*clock)(void*, int, unsigned*) = nullptr;
    int (*util)(void*, void*) = nullptr;
    int (*power)(void*, unsigned*) = nullptr;
    bool ok = false;
    Nvml() {
        for (const char* n : {"libnvidia-ml.so.1", "libnvidia-ml.so"}) { lib = dlopen(n, RTLD_LAZY | RTLD_LOCAL); if (lib) break; }
        if (!lib) return;
        init = reinterpret_cast<decltype(init)>(dlsym(lib, "nvmlInit_v2"));
        shutdown = reinterpret_cast<decltype(shutdown)>(dlsym(lib, "nvmlShutdown"));
        handle = reinterpret_cast<decltype(handle)>(dlsym(lib, "nvmlDeviceGetHandleByIndex_v2"));
        temp = reinterpret_cast<decltype(temp)>(dlsym(lib, "nvmlDeviceGetTemperature"));
        clock = reinterpret_cast<decltype(clock)>(dlsym(lib, "nvmlDeviceGetClockInfo"));
        util = reinterpret_cast<decltype(util)>(dlsym(lib, "nvmlDeviceGetUtilizationRates"));
        power = reinterpret_cast<decltype(power)>(dlsym(lib, "nvmlDeviceGetPowerUsage"));
        ok = init && shutdown && handle && temp && clock && util && power;
    }
};

// the actual NVML calls; only ever runs in a short-lived child, so the ~15 MB NVML / CUDA setup never stays resident in this process
bool nvml_query(DgpuValues* v) {
    static Nvml n;
    if (!n.ok) return false;
    if (n.init() != 0) return false;
    bool ok = false;
    void* h = nullptr;
    if (n.handle(0, &h) == 0) {
        unsigned t = 0, c = 0, pw = 0;
        struct { unsigned gpu, memory; } u{0, 0};
        if (n.temp(h, 0, &t) == 0 && n.clock(h, 0, &c) == 0 && n.util(h, &u) == 0 && n.power(h, &pw) == 0) {
            v->clock_mhz = static_cast<int>(c);
            v->temp_c = static_cast<int>(t);
            v->util_pct = static_cast<int>(u.gpu);
            v->power_w = pw / 1000.0;
            ok = true;
        }
    }
    n.shutdown();
    return ok;
}

bool nvml_dgpu(DgpuValues* v) {
    static std::mutex mu;
    std::lock_guard<std::mutex> g(mu);
    struct Wire { int ok, clock, temp, util; double power; } w{0, 0, 0, 0, 0};
    int fd[2];
    if (pipe2(fd, O_CLOEXEC) != 0) return false;
    pid_t pid = fork();
    if (pid < 0) { close(fd[0]); close(fd[1]); return false; }
    if (pid == 0) {
        close(fd[0]);
        DgpuValues r;
        Wire o{0, 0, 0, 0, 0};
        if (nvml_query(&r)) o = {1, r.clock_mhz.value_or(0), r.temp_c.value_or(0), r.util_pct.value_or(0), r.power_w.value_or(0)};
        (void)!write(fd[1], &o, sizeof o);
        _exit(0);
    }
    close(fd[1]);
    pollfd pf{fd[0], POLLIN, 0};
    bool got = poll(&pf, 1, 4000) > 0 && read(fd[0], &w, sizeof w) == static_cast<ssize_t>(sizeof w);
    close(fd[0]);
    if (!got) kill(pid, SIGKILL);
    int status = 0;
    waitpid(pid, &status, 0);
    if (!got || !w.ok) return false;
    v->clock_mhz = w.clock; v->temp_c = w.temp; v->util_pct = w.util; v->power_w = w.power;
    return true;
}
}  // namespace

DgpuValues dgpu_values() {
    DgpuValues v;
    if (!dgpu_is_awake()) return v;
    if (nvml_dgpu(&v)) return v;
    if (which("nvidia-smi").empty()) return v;
    auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=clocks.gr,temperature.gpu,utilization.gpu,power.draw",
                              "--format=csv,noheader,nounits"}, 5000);
    if (r.code != 0) return v;
    auto f = split_commas(trim(r.out));
    if (f.size() != 4) return v;
    auto a = to_double(f[0]), b = to_double(f[1]), c = to_double(f[2]), d = to_double(f[3]);
    if (!a || !b || !c || !d) return v;
    v.clock_mhz = static_cast<int>(*a);
    v.temp_c = static_cast<int>(*b);
    v.util_pct = static_cast<int>(*c);
    v.power_w = *d;
    return v;
}

std::vector<std::string> mangohud_gpu_order() {
    static const std::regex pci_re(R"(^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$)");
    std::vector<std::pair<int, std::string>> nodes;
    for (const auto& p : glob_paths("/sys/class/drm/renderD[0-9]*")) {
        std::string n = base_name(p).substr(7);
        std::string pci = base_name(real_path(p + "/device"));
        if (std::regex_match(pci, pci_re)) nodes.emplace_back(std::atoi(n.c_str()), norm_pci(pci));
    }
    std::sort(nodes.begin(), nodes.end());
    std::vector<std::string> out;
    for (const auto& n : nodes)
        if (std::find(out.begin(), out.end(), n.second) == out.end()) out.push_back(n.second);
    return out;
}

static std::set<std::string> gpu_tokens(const std::string& text) {
    std::set<std::string> out;
    std::string cur;
    for (char c : lower(text) + " ") {
        if (std::isalnum(static_cast<unsigned char>(c))) cur += c;
        else { if (cur.size() > 1) out.insert(cur); cur.clear(); }
    }
    return out;
}

std::vector<std::string> gpu_label_pci_map(const std::vector<std::string>& labels, const std::vector<GpuDevice>* devices) {
    std::vector<std::pair<std::string, std::string>> blobs;   // pci -> lspci text (ordered)
    if (!which("lspci").empty()) {
        auto r = run_argv_stdout({"lspci", "-Dmm"}, 6000);
        for (const auto& ln : split_lines(r.out)) {
            if (trim(ln).empty()) continue;
            std::string slot = ln.substr(0, ln.find_first_of(" \t"));
            auto parts = quoted_fields(ln);
            if (parts.size() < 3 || !gpu_class(parts[0])) continue;
            blobs.push_back({norm_pci(slot), lower(parts[1] + " " + parts[2] + (parts.size() > 3 ? " " + parts[3] : ""))});
        }
    }
    std::vector<std::string> order;
    for (const auto& b : blobs) order.push_back(b.first);
    for (const auto& g : drm_gpus()) { std::string p = norm_pci(g.pci); if (!p.empty() && std::find(order.begin(), order.end(), p) == order.end()) order.push_back(p); }
    std::map<std::string, std::string> named;
    if (devices) for (const auto& d : *devices) {
        std::string p = norm_pci(d.pci);
        if (p.empty()) continue;
        named[p] = trim(named[p] + " " + d.name);
        if (std::find(order.begin(), order.end(), p) == order.end()) order.push_back(p);
    }
    struct Cand { std::string pci; std::set<std::string> tokens; };
    std::vector<Cand> cand;
    for (const auto& p : order) {
        std::string blob;
        for (const auto& b : blobs) if (b.first == p) blob = b.second;
        cand.push_back({p, gpu_tokens(blob + " " + named[p])});
    }
    std::vector<std::string> out;
    size_t n = cand.size();
    if (!n) return std::vector<std::string>(labels.size());
    std::map<std::string, int> df;
    for (const auto& c : cand) for (const auto& t : c.tokens) ++df[t];
    std::set<std::string> used;
    for (const auto& label : labels) {
        auto lt = gpu_tokens(label);
        std::string best;
        double best_score = 0;
        for (const auto& c : cand) {
            if (c.pci.empty() || used.count(c.pci)) continue;
            double score = 0;
            for (const auto& t : lt) if (c.tokens.count(t)) score += 1.0 + static_cast<double>(n - static_cast<size_t>(df[t]));
            if (score > best_score) { best = c.pci; best_score = score; }
        }
        if (!best.empty() && best_score > 0) { used.insert(best); out.push_back(best); } else out.push_back("");
    }
    return out;
}

static std::vector<DrmGpu> drm_gpus_raw() {
    std::vector<DrmGpu> out;
    for (const auto& card : glob_paths("/sys/class/drm/card[0-9]")) {
        std::string dev = card + "/device";
        std::string vendor = read_trim(dev + "/vendor");
        if (vendor.empty()) continue;
        DrmGpu g;
        g.vendor = vendor;
        g.driver = path_exists(dev + "/driver") ? base_name(real_path(dev + "/driver")) : "";
        g.pci = base_name(real_path(dev));
        g.boot_vga = read_trim(dev + "/boot_vga") == "1";
        g.card = "/dev/dri/" + base_name(card);
        for (const auto& rd : glob_paths("/sys/class/drm/renderD*"))
            if (real_path(rd + "/device") == real_path(dev)) { g.render = "/dev/dri/" + base_name(rd); break; }
        bool nvidia = vendor == "0x10de" || g.driver == "nvidia" || g.driver == "nouveau";
        g.kind = (nvidia || !g.boot_vga) ? "discrete" : "integrated";
        out.push_back(std::move(g));
    }
    std::stable_sort(out.begin(), out.end(),
                     [](const DrmGpu& a, const DrmGpu& b) { return (a.kind == "discrete") > (b.kind == "discrete"); });
    return out;
}

std::vector<DrmGpu> drm_gpus() { static Memo<std::vector<DrmGpu>> memo; return memo.get(-1, [] { return drm_gpus_raw(); }); }

std::vector<VramRow> vram_info() {
    std::map<std::string, std::string> names;
    for (const auto& d : gpu_devices())
        if (!d.pci.empty()) names[lower(d.pci)] = d.name;
    std::vector<VramRow> rows;
    for (const auto& g : drm_gpus()) {
        std::string dev = "/sys/bus/pci/devices/" + g.pci;
        VramRow row;
        auto it = names.find(lower(g.pci));
        row.name = it != names.end() ? it->second : (!g.driver.empty() ? g.driver : g.pci);
        row.pci = g.pci;
        row.driver = g.driver;
        row.kind = g.kind;
        auto mb = [&](const char* n) -> std::optional<long> {
            auto v = read_int(dev + "/" + n);
            return v ? std::optional<long>(static_cast<long>(*v / (1024 * 1024))) : std::nullopt;
        };
        if (g.driver == "amdgpu") {
            row.used_mb = mb("mem_info_vram_used");
            row.total_mb = mb("mem_info_vram_total");
            row.gtt_used_mb = mb("mem_info_gtt_used");
        } else if (g.driver == "nvidia" || g.driver == "nouveau") {
            if (which("nvidia-smi").empty() || !dgpu_is_awake()) row.asleep = true;
            else {
                auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"}, 5000);
                auto f = split_commas(trim(r.out));
                if (f.size() == 2) {
                    auto u = to_double(f[0]), t = to_double(f[1]);
                    if (u && t) { row.used_mb = static_cast<long>(*u); row.total_mb = static_cast<long>(*t); }
                }
            }
        }
        if (row.used_mb && row.total_mb && *row.total_mb > 0)
            row.pct = static_cast<int>(std::lround(100.0 * static_cast<double>(*row.used_mb) / static_cast<double>(*row.total_mb)));
        rows.push_back(std::move(row));
    }
    return rows;
}

std::vector<VramConsumer> vram_consumers(size_t limit) {
    static const std::regex vram_re(R"(drm-(?:total|resident|memory)-vram:\s*(\d+)\s*KiB)");
    static const std::regex drv_re(R"(drm-driver:\s*(\S+))");
    std::map<std::string, VramConsumer> by_pid;
    for (const auto& fd : glob_paths("/proc/[0-9]*/fdinfo/*")) {
        size_t a = fd.find('/', 1), b = fd.find('/', a + 1);
        std::string pid = fd.substr(a + 1, b - a - 1);
        if (by_pid.count(pid)) continue;
        std::string txt = read_file(fd).value_or("");
        if (txt.find("drm-driver") == std::string::npos) continue;
        std::smatch mv;
        if (!std::regex_search(txt, mv, vram_re)) continue;
        long kb = std::stol(mv[1]);
        if (kb <= 0) continue;
        std::smatch dm;
        VramConsumer c;
        c.pid = std::stol(pid);
        std::string comm = read_trim("/proc/" + pid + "/comm");
        c.comm = comm.empty() ? "?" : comm;
        c.driver = std::regex_search(txt, dm, drv_re) ? dm[1].str() : "drm";
        c.vram_mb = std::round(static_cast<double>(kb) / 1024.0 * 10) / 10;
        by_pid[pid] = c;
    }
    if (!which("nvidia-smi").empty() && dgpu_is_awake()) {
        auto r = run_argv_stdout({"nvidia-smi", "--query-compute-apps=pid,used_memory,process_name", "--format=csv,noheader,nounits"}, 5000);
        for (const auto& ln : split_lines(r.out)) {
            auto f = split_commas(ln);
            if (f.size() < 3) continue;
            VramConsumer c;
            c.pid = std::atol(f[0].c_str());
            c.comm = base_name(f[2]);
            c.driver = "nvidia";
            c.vram_mb = to_double(f[1]).value_or(0);
            by_pid["nv" + f[0]] = c;
        }
    }
    std::vector<VramConsumer> rows;
    for (auto& kv : by_pid) rows.push_back(kv.second);
    std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) { return a.vram_mb > b.vram_mb; });
    if (rows.size() > limit) rows.resize(limit);
    return rows;
}

std::optional<RuntimePm> nvidia_runtime_pm() {
    std::string dev = nvidia_pci_dir();
    if (dev.empty()) return std::nullopt;
    std::string c = read_trim(dev + "/power/control"), s = read_trim(dev + "/power/runtime_status");
    return RuntimePm{c.empty() ? "?" : c, s.empty() ? "?" : s};
}

Result set_nvidia_runtime_pm(bool automatic) {
    std::string dev = nvidia_pci_dir();
    if (dev.empty()) return {false, "no NVIDIA GPU"};
    std::string want = automatic ? "auto" : "on";
    if (write_text(dev + "/power/control", want)) return {true, "dGPU runtime PM -> " + want};
    return {false, "cannot write power/control (need root)"};
}

// ---------------------------------------------------------------- NVIDIA limits / clocks
std::optional<NvPowerLimit> nvidia_power_limit_info() {
    if (which("nvidia-smi").empty() || !dgpu_is_awake()) return std::nullopt;
    auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=power.limit,power.min_limit,power.max_limit,power.default_limit",
                              "--format=csv,noheader,nounits"}, 5000);
    if (r.code != 0) return std::nullopt;
    auto f = split_commas(trim(r.out));
    if (f.size() != 4) return std::nullopt;
    auto cur = to_double(f[0]), lo = to_double(f[1]), hi = to_double(f[2]), dft = to_double(f[3]);
    NvPowerLimit n;
    n.supported = cur.has_value();
    n.current = opt_round(cur);
    n.min = lo ? static_cast<int>(std::lround(*lo)) : 1;
    n.max = hi ? static_cast<int>(std::lround(*hi)) : 100;
    n.def = opt_round(dft);
    return n;
}

Result set_nvidia_power_limit(int watts) {
    if (which("nvidia-smi").empty()) return {false, "nvidia-smi not installed"};
    static const char* locked = "this GPU's power limit is firmware-locked (laptop Dynamic Boost) - nvidia-smi -pl is not supported on it";
    auto info = nvidia_power_limit_info();
    if (info && !info->supported) return {false, locked};
    int w = watts;
    if (info) w = std::max(info->min, std::min(info->max, w));
    run_argv({"nvidia-smi", "-pm", "1"}, 8000);
    auto r = run_argv({"nvidia-smi", "-pl", std::to_string(w)}, 10000);
    if (lower(r.out).find("not supported") != std::string::npos) return {false, locked};
    return blob_result(r, "nvidia-smi -pl failed");
}

std::optional<NvClockInfo> nvidia_clock_info() {
    if (which("nvidia-smi").empty() || !dgpu_is_awake()) return std::nullopt;
    auto r = run_argv_stdout({"nvidia-smi", "--query-gpu=clocks.max.graphics,clocks.max.memory,clocks.graphics,clocks.memory",
                              "--format=csv,noheader,nounits"}, 6000);
    if (r.code != 0) return std::nullopt;
    auto f = split_commas(trim(r.out));
    if (f.size() != 4) return std::nullopt;
    auto gmax = to_double(f[0]), mmax = to_double(f[1]), gcur = to_double(f[2]), mcur = to_double(f[3]);
    NvClockInfo n;
    auto q = run_argv_stdout({"nvidia-smi", "-q", "-d", "SUPPORTED_CLOCKS"}, 8000);
    static const std::regex gr(R"(Graphics\s*:\s*(\d+)\s*MHz)");
    int lo = 0;
    for (auto it = std::sregex_iterator(q.out.begin(), q.out.end(), gr); it != std::sregex_iterator(); ++it) {
        int v = std::stoi((*it)[1]);
        lo = lo == 0 ? v : std::min(lo, v);
    }
    n.supported = gmax.has_value();
    n.gr_min = lo ? lo : 210;
    if (gmax && *gmax) n.gr_max = static_cast<int>(std::lround(*gmax));
    if (gcur && *gcur) n.gr_cur = static_cast<int>(std::lround(*gcur));
    if (mmax && *mmax) n.mem_max = static_cast<int>(std::lround(*mmax));
    if (mcur && *mcur) n.mem_cur = static_cast<int>(std::lround(*mcur));
    return n;
}

Result set_nvidia_clock_lock(int gr_min, int gr_max) {
    if (which("nvidia-smi").empty()) return {false, "nvidia-smi not installed"};
    auto info = nvidia_clock_info();
    if (info && info->gr_max) {
        int floor = info->gr_min ? info->gr_min : 210, ceil = *info->gr_max;
        gr_min = std::max(floor, std::min(ceil, gr_min));
        gr_max = std::max(gr_min, std::min(ceil, gr_max));
    } else {
        gr_max = std::max(gr_min, gr_max);
    }
    run_argv({"nvidia-smi", "-pm", "1"}, 8000);
    auto r = run_argv({"nvidia-smi", "--lock-gpu-clocks=" + std::to_string(gr_min) + "," + std::to_string(gr_max)}, 12000);
    if (r.code == 0 && lower(r.out).find("not supported") == std::string::npos)
        return {true, std::to_string(gr_min) + "-" + std::to_string(gr_max) + " MHz"};
    return {false, trim(r.out).empty() ? "lock-gpu-clocks failed" : trim(r.out)};
}

Result reset_nvidia_clocks() {
    if (which("nvidia-smi").empty()) return {false, "nvidia-smi not installed"};
    return blob_result(run_argv({"nvidia-smi", "--reset-gpu-clocks"}, 12000), "reset failed");
}

NvOffsetInfo nvidia_clock_offset_info() {
    NvOffsetInfo o;
    std::string ns = which("nvidia-settings");
    if (ns.empty()) { o.reason = "nvidia-settings not installed"; return o; }
    std::string disp = env_str("DISPLAY");
    if (disp.empty()) { o.reason = "no X DISPLAY (nvidia-settings can't run under pure Wayland)"; return o; }
    auto r = run_argv({ns, "-c", disp, "-q", "[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels", "-q",
                       "[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels"}, 8000);
    if (r.code != 0 || r.out.find("ERROR") != std::string::npos || r.out.find("Unable") != std::string::npos) {
        o.reason = "GPUGraphicsClockOffset not exposed (Coolbits not set?)";
        return o;
    }
    static const std::regex core_re(R"(GraphicsClockOffsetAllPerformanceLevels'.*?\):\s*(-?\d+))");
    static const std::regex mem_re(R"(MemoryTransferRateOffsetAllPerformanceLevels'.*?\):\s*(-?\d+))");
    static const std::regex range_re(R"(range (-?\d+) - (-?\d+))");
    std::smatch m;
    o.available = true;
    o.core = std::regex_search(r.out, m, core_re) ? std::stoi(m[1]) : 0;
    o.mem = std::regex_search(r.out, m, mem_re) ? std::stoi(m[1]) : 0;
    if (std::regex_search(r.out, m, range_re)) o.core_range = {std::stoi(m[1]), std::stoi(m[2])};
    return o;
}

Result set_nvidia_clock_offset(int core_mhz, int mem_mhz) {
    std::string ns = which("nvidia-settings"), disp = env_str("DISPLAY");
    if (ns.empty() || disp.empty()) return {false, "nvidia-settings / X display unavailable"};
    auto r = run_argv({ns, "-c", disp, "-a", "[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels=" + std::to_string(core_mhz), "-a",
                       "[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels=" + std::to_string(mem_mhz)}, 12000);
    if (r.code == 0 && r.out.find("ERROR") == std::string::npos) {
        char b[96];
        std::snprintf(b, sizeof b, "core %+d / mem %+d MHz", core_mhz, mem_mhz);
        return {true, b};
    }
    return {false, trim(r.out).empty() ? "offset apply failed" : trim(r.out)};
}

bool envycontrol_available() { static Memo<bool> m; return m.get(30.0, [] { return !which("envycontrol").empty(); }); }

std::optional<std::string> gpu_mode_get() {
    std::string exe = which("envycontrol");
    if (exe.empty()) return std::nullopt;
    auto r = run_argv({exe, "--query"}, 8000);
    std::string t = lower(trim(r.out));
    for (const char* m : {"integrated", "hybrid", "nvidia"})
        if (t.find(m) != std::string::npos) return std::string(m);
    return std::nullopt;
}

Result gpu_mode_set(const std::string& mode) {
    std::string exe = which("envycontrol");
    if (exe.empty()) return {false, "envycontrol not installed (install the EnvyControl app first)"};
    if (mode != "integrated" && mode != "hybrid" && mode != "nvidia") return {false, "unknown mode '" + mode + "'"};
    auto r = run_argv({exe, "-s", mode, "--dm", "sddm"}, 60000);
    if (r.code == 0) return {true, ""};
    auto r2 = run_argv({exe, "-s", mode}, 60000);
    if (r2.code == 0) return {true, ""};
    return {false, trim(r2.out).empty() ? "envycontrol failed" : trim(r2.out)};
}

PowerdStatus nvidia_powerd_status() {
    PowerdStatus s;
    s.active = trim(run_argv_stdout({"systemctl", "is-active", "nvidia-powerd.service"}, 5000).out) == "active";
    s.installed = run_argv_stdout({"systemctl", "list-unit-files", "nvidia-powerd.service"}, 5000).out.find("nvidia-powerd.service") != std::string::npos;
    return s;
}

// ---------------------------------------------------------------- power profile / game mode
std::string active_power_profile() {
    if (!which("busctl").empty()) {
        static const char* names[][2] = {{"net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"},
                                         {"org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"}};
        static const std::regex re(R"re(^s\s+"([^"]*)")re");
        for (auto& n : names) {
            auto r = run_argv_stdout({"busctl", "--system", "get-property", n[0], n[1], n[0], "ActiveProfile"}, 5000);
            std::smatch m;
            std::string t = trim(r.out);
            if (r.code == 0 && std::regex_search(t, m, re)) return m[1];
        }
    }
    if (!which("powerprofilesctl").empty()) return trim(run_argv_stdout({"powerprofilesctl", "get"}, 5000).out);
    return "";
}

bool game_mode_state() {
    std::string p = active_power_profile();
    return !p.empty() && p == game_mode_value();
}

void notify(const std::string& summary, const std::string& body) {
    std::string exe = which("notify-send");
    if (exe.empty()) return;
    run_argv({exe, "-a", "TuxThrottle", "-i", "input-keyboard", "-t", "10000", summary, body}, 5000);
}

Result set_game_mode(bool enable) {
    std::vector<std::string> names = enable ? std::vector<std::string>{"gaming-performance", "amdgpu-perf-high", "nvidia-max-perf"}
                                            : std::vector<std::string>{"gaming-balanced", "amdgpu-perf-auto", "nvidia-max-perf off"};   // the dGPU script takes on|off
    std::vector<std::string> paths, args;
    for (const auto& n : names) {
        size_t sp = n.find(' ');
        std::string p = which(n.substr(0, sp));
        if (p.empty()) continue;
        paths.push_back(p);
        args.push_back(sp == std::string::npos ? "" : n.substr(sp + 1));
    }
    if (paths.empty()) return {false, "None of the Game Mode helper scripts are installed - run the Toolkit's Presets first."};
    auto done = [&]() -> Result {
        if (enable) notify("Game Mode: ON", "G-Mode / performance profile - fans + power limits up");
        else notify("Game Mode: OFF", "Back to balanced profile");
        return {true, ""};
    };
    std::string last_err;
    bool any_fail = false;
    for (size_t i = 0; i < paths.size(); ++i) {
        const std::string& p = paths[i];
        std::vector<std::string> cmd{"sudo", "-n", p};
        if (!args[i].empty()) cmd.push_back(args[i]);
        auto r = run_argv(cmd, 30000);
        if (r.code != 0) { any_fail = true; last_err = trim(r.out).empty() ? p + " failed" : trim(r.out); }
    }
    if (!any_fail || game_mode_state() == enable) return done();
    std::vector<std::string> joined;
    for (size_t i = 0; i < paths.size(); ++i) joined.push_back(paths[i] + (args[i].empty() ? "" : " " + args[i]));
    auto r = run_argv({"pkexec", "sh", "-c", join(joined, " ; ")}, 60000);
    if (r.code != 0 && game_mode_state() != enable)
        return {false, !trim(r.out).empty() ? trim(r.out) : (!last_err.empty() ? last_err : "pkexec failed")};
    return done();
}

Result toggle_game_mode() { return set_game_mode(!game_mode_state()); }

GameModeStatus gamemode_status() {
    GameModeStatus s;
    std::string exe = which("gamemoded");
    s.installed = !exe.empty();
    if (!s.installed) return s;
    auto r = run_argv({exe, "-s"}, 5000);
    s.active = r.out.find("is active") != std::string::npos;
    static const std::regex re(R"((\d+)\s+client)");
    std::smatch m;
    if (std::regex_search(r.out, m, re)) s.clients = std::stoi(m[1]);
    return s;
}

// ---------------------------------------------------------------- fans
std::vector<Fan> read_fans() {
    std::string aw = hwmon_by_name(fan_hwmon()), smm = hwmon_by_name(fan_pwm_hwmon());
    std::string base = !aw.empty() ? aw : smm;
    std::vector<Fan> fans;
    if (base.empty()) return fans;
    for (int i : fan_indices()) {
        std::string n = std::to_string(i);
        auto rpm = read_int(base + "/fan" + n + "_input");
        if (!rpm && !smm.empty() && smm != base) rpm = read_int(smm + "/fan" + n + "_input");
        if (!rpm) continue;
        Fan f;
        f.index = i;
        f.label = read_trim(base + "/fan" + n + "_label");
        if (f.label.empty()) f.label = "Fan " + n;
        f.rpm = static_cast<int>(*rpm);
        auto mx = read_int(base + "/fan" + n + "_max");
        f.max = mx && *mx ? static_cast<int>(*mx) : fan_rpm_max();
        if (!aw.empty()) {
            auto b = read_int(aw + "/" + fan_boost_attr(i));
            if (b) f.boost = static_cast<int>(*b);
        }
        fans.push_back(std::move(f));
    }
    return fans;
}

std::vector<int> get_fan_boost() {
    std::string aw = hwmon_by_name(fan_hwmon());
    std::vector<int> out;
    if (aw.empty()) return out;
    for (int i : fan_indices()) out.push_back(static_cast<int>(read_int(aw + "/" + fan_boost_attr(i)).value_or(0)));
    return out;
}

Result set_fan_boost(int index, int value) {
    std::string aw = hwmon_by_name(fan_hwmon());
    if (aw.empty()) return {false, fan_hwmon() + " hwmon not present"};
    int v = std::max(0, std::min(255, value));
    if (write_text(aw + "/" + fan_boost_attr(index), std::to_string(v))) return {true, ""};
    return {false, "cannot write " + fan_boost_attr(index)};
}

std::vector<std::string> platform_profile_choices() { return split_ws(read_file(platform_profile_path() + "_choices").value_or("")); }
std::string get_platform_profile() { return read_trim(platform_profile_path()); }
Result set_platform_profile(const std::string& name) {
    if (write_text(platform_profile_path(), name)) return {true, ""};
    return {false, "cannot write " + platform_profile_path()};
}

std::vector<PwmState> get_pwm_state() {
    std::string smm = hwmon_by_name(fan_pwm_hwmon());
    std::vector<PwmState> out;
    if (smm.empty()) return out;
    for (int i : fan_indices()) {
        PwmState s;
        auto e = read_int(smm + "/pwm" + std::to_string(i) + "_enable"), p = read_int(smm + "/pwm" + std::to_string(i));
        if (e) s.enable = static_cast<int>(*e);
        if (p) s.pwm = static_cast<int>(*p);
        out.push_back(s);
    }
    return out;
}

Result set_pwm_manual(int index, int value) {
    std::string smm = hwmon_by_name(fan_pwm_hwmon());
    if (smm.empty()) return {false, fan_pwm_hwmon() + " hwmon not present"};
    int v = std::max(pwm_floor(), std::min(255, value));
    std::string n = std::to_string(index);
    if (write_text(smm + "/pwm" + n + "_enable", "1") && write_text(smm + "/pwm" + n, std::to_string(v))) return {true, ""};
    return {false, "cannot write pwm" + n};
}

Result restore_fan_auto() {
    std::vector<std::string> errs;
    std::string smm = hwmon_by_name(fan_pwm_hwmon());
    if (!smm.empty())
        for (int i : fan_indices())
            if (!write_text(smm + "/pwm" + std::to_string(i) + "_enable", "2")) errs.push_back("cannot write pwm" + std::to_string(i) + "_enable");
    for (int i : fan_indices()) {
        auto r = set_fan_boost(i, 0);
        if (!r.first && r.second.find("not present") == std::string::npos) errs.push_back(r.second);
    }
    return {errs.empty(), join(errs, "; ")};
}

// ---------------------------------------------------------------- ryzenadj
bool ryzenadj_available() { static Memo<bool> m; return m.get(30.0, [] { return !which("ryzenadj").empty(); }); }

std::optional<RyzenInfo> read_ryzenadj_info() {
    std::string exe = which("ryzenadj");
    if (exe.empty()) return std::nullopt;
    auto r = run_argv_stdout({exe, "-i"}, 8000);
    if (r.code != 0 && r.out.empty()) return std::nullopt;
    RyzenInfo info;
    for (const auto& line : split_lines(r.out)) {
        if (std::count(line.begin(), line.end(), '|') < 3) continue;
        std::vector<std::string> cells;
        std::string cur;
        std::string body = trim(line);
        if (!body.empty() && body.front() == '|') body.erase(0, 1);
        if (!body.empty() && body.back() == '|') body.pop_back();
        for (char c : body + "|") {
            if (c == '|') { cells.push_back(trim(cur)); cur.clear(); }
            else cur += c;
        }
        if (cells.size() < 2) continue;
        auto val = to_double(cells[1]);
        if (!val) continue;
        double v = std::round(*val * 10) / 10;
        std::string name = cells[0];
        for (auto& ch : name) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
        if (name == "STAPM LIMIT") info.stapm_limit = v;
        else if (name == "PPT LIMIT FAST") info.fast_limit = v;
        else if (name == "PPT LIMIT SLOW") info.slow_limit = v;
        else if (name == "THM LIMIT CORE") info.tctl_limit = v;
        else if (name == "STAPM VALUE") info.stapm_value = v;
        else if (name == "PPT VALUE FAST") info.fast_value = v;
        else if (name == "PPT VALUE SLOW") info.slow_value = v;
        else if (name == "THM VALUE CORE") info.tctl_value = v;
    }
    if (!info.any()) return std::nullopt;
    return info;
}

Result set_ryzenadj_limits(std::optional<double> fast_w, std::optional<double> slow_w, std::optional<double> stapm_w) {
    std::string exe = which("ryzenadj");
    if (exe.empty()) return {false, "ryzenadj is not installed (Power & Limits tab tweak)"};
    std::vector<std::string> args{exe};
    auto add = [&](const char* flag, std::optional<double> v) {
        if (!v) return;
        int w = std::max(10, std::min(90, static_cast<int>(std::lround(*v))));
        args.push_back(std::string(flag) + "=" + std::to_string(w * 1000));
    };
    add("--stapm-limit", stapm_w);
    add("--fast-limit", fast_w);
    add("--slow-limit", slow_w);
    if (args.size() == 1) return {false, "no limit given"};
    auto r = run_argv(args, 10000);
    if (r.code == 0 || r.out.find("OK") != std::string::npos) return {true, ""};
    return {false, trim(r.out).empty() ? "ryzenadj failed" : trim(r.out)};
}

bool ryzenadj_co_supported() { return ryzenadj_available() && cpu_is_amd(); }

Result set_co_offset(int all_core) {
    std::string exe = which("ryzenadj");
    if (exe.empty()) return {false, "ryzenadj is not installed (RyzenCurveOptimizer tweak)"};
    int v = std::max(-50, std::min(0, all_core));
    auto r = run_argv({exe, "--set-coall=" + std::to_string(v)}, 10000);
    if (r.code == 0 || r.out.find("OK") != std::string::npos) return {true, ""};
    return {false, trim(r.out).empty() ? "ryzenadj failed" : trim(r.out)};
}

// ---------------------------------------------------------------- battery
std::string battery_dir() {
    for (const auto& b : glob_paths("/sys/class/power_supply/BAT*"))
        if (path_exists(b + "/charge_control_end_threshold")) return b;
    return "";
}

static std::optional<int> smbios_battery_end() {
    std::string exe = which("smbios-battery-ctl");
    if (exe.empty()) return std::nullopt;
    auto r = run_argv_stdout({exe, "--get-charging-cfg"}, 8000);
    static const std::regex re(R"([Ee]nd\s*[:=]\s*(\d+))");
    std::smatch m;
    if (std::regex_search(r.out, m, re)) return std::stoi(m[1]);
    return std::nullopt;
}

static std::optional<int> opt_int(const std::optional<long long>& v) {
    return v ? std::optional<int>(static_cast<int>(*v)) : std::nullopt;
}

ChargeLimitInfo battery_charge_limit_info() {
    ChargeLimitInfo info;
    std::string bat = battery_dir();
    if (!bat.empty()) {
        info.supported = true;
        info.method = "sysfs";
        info.current = opt_int(read_int(bat + "/charge_control_end_threshold"));
        info.capacity = opt_int(read_int(bat + "/capacity"));
    } else {
        info.dell_libsmbios_possible = lower(dmi("sys_vendor")).find("dell") != std::string::npos;
        if (!which("smbios-battery-ctl").empty()) {
            info.supported = true;
            info.method = "libsmbios";
            info.current = smbios_battery_end();
        }
        for (const auto& cap : glob_paths("/sys/class/power_supply/BAT*/capacity")) {
            info.capacity = opt_int(read_int(cap));
            break;
        }
    }
    for (const auto& ac : glob_paths("/sys/class/power_supply/A[CD]*/online"))
        if (auto v = read_int(ac)) { info.ac_online = *v != 0; break; }
    return info;
}

Result set_battery_charge_limit(int percent) {
    int p = std::max(50, std::min(100, percent));
    std::string bat = battery_dir();
    if (!bat.empty()) {
        if (write_text(bat + "/charge_control_end_threshold", std::to_string(p))) return {true, ""};
        return {false, "cannot write charge_control_end_threshold"};
    }
    std::string exe = which("smbios-battery-ctl");
    if (!exe.empty()) {
        int start = std::max(50, std::min(p - 5, 95));
        RunResult r = p >= 100 ? run_argv({exe, "--set-charging-mode=standard"}, 15000)
                               : run_argv({exe, "--set-custom-charge-interval=" + std::to_string(start) + " " + std::to_string(p)}, 15000);
        return {r.code == 0, trim(r.out)};
    }
    return {false, "no charge_control_end_threshold in sysfs; on Dell install libsmbios (dnf install libsmbios) for firmware-level control"};
}

std::optional<std::string> battery_charge_mode() {
    std::string exe = which("smbios-battery-ctl");
    if (exe.empty()) return std::nullopt;
    auto r = run_argv_stdout({exe, "--get-charging-cfg"}, 8000);
    static const std::regex re(R"([Cc]harging mode\s*[:=]\s*([A-Za-z_]+))");
    std::smatch m;
    if (std::regex_search(r.out, m, re)) return lower(m[1]);
    return std::nullopt;
}

Result set_battery_charge_mode(const std::string& mode) {
    std::string exe = which("smbios-battery-ctl");
    if (exe.empty()) return {false, "libsmbios not installed (Dell battery threshold tweak)"};
    if (mode != "standard" && mode != "express" && mode != "adaptive" && mode != "primarily_ac")
        return {false, "unknown charging mode '" + mode + "'"};
    auto r = run_argv({exe, "--set-charging-mode=" + mode}, 15000);
    return {r.code == 0, trim(r.out)};
}

BatteryHealth battery_health_info() {
    BatteryHealth h;
    auto bats = glob_paths("/sys/class/power_supply/BAT*");
    if (bats.empty()) return h;
    std::string bat = bats[0];
    auto s = [&](const char* n) { return read_trim(bat + "/" + n); };
    auto full = read_int(bat + "/energy_full"), design = read_int(bat + "/energy_full_design");
    h.unit = "Wh";
    double scale = 1e6;
    if (!full) {
        full = read_int(bat + "/charge_full");
        design = read_int(bat + "/charge_full_design");
        h.unit = "Ah";
    }
    if (full && design && *design > 0 && *full) h.wear_pct = std::round(100.0 * (1.0 - static_cast<double>(*full) / static_cast<double>(*design)) * 10) / 10;
    auto power_uw = read_int(bat + "/power_now"), volt_uv = read_int(bat + "/voltage_now"), cur_ua = read_int(bat + "/current_now");
    if (!power_uw && cur_ua && volt_uv && *volt_uv) power_uw = std::llabs(*cur_ua) * *volt_uv / 1000000;
    h.status = s("status");
    std::optional<long long> now_raw, rate;
    if (h.unit == "Wh") { now_raw = read_int(bat + "/energy_now"); rate = power_uw; }
    else { now_raw = read_int(bat + "/charge_now"); if (cur_ua && *cur_ua) rate = std::llabs(*cur_ua); }
    if (rate && *rate && now_raw && full && *full) {
        std::string st = lower(h.status);
        if (st == "discharging") { h.eta_min = static_cast<int>(std::lround(static_cast<double>(*now_raw) / static_cast<double>(*rate) * 60)); h.eta_kind = "to empty"; }
        else if (st == "charging" && *full > *now_raw) {
            h.eta_min = static_cast<int>(std::lround(static_cast<double>(*full - *now_raw) / static_cast<double>(*rate) * 60));
            h.eta_kind = "to full";
        }
    }
    h.present = true;
    h.manufacturer = s("manufacturer");
    h.model = s("model_name");
    h.technology = s("technology");
    h.capacity_pct = opt_int(read_int(bat + "/capacity"));
    h.cycle_count = opt_int(read_int(bat + "/cycle_count"));
    if (full && *full) h.full = std::round(static_cast<double>(*full) / scale * 10) / 10;
    if (design && *design) h.design = std::round(static_cast<double>(*design) / scale * 10) / 10;
    if (power_uw && *power_uw) h.power_w = std::round(static_cast<double>(*power_uw) / 1e6 * 10) / 10;
    if (volt_uv && *volt_uv) h.voltage_v = std::round(static_cast<double>(*volt_uv) / 1e6 * 100) / 100;
    return h;
}

// ---------------------------------------------------------------- display / input
static VrrStatus vrr_status_raw() {
    VrrStatus v;
    for (const auto& p : glob_paths("/sys/class/drm/card*-*/vrr_capable"))
        if (read_trim(p) == "1") {
            std::string conn = base_name(dir_name(p));
            size_t d = conn.find('-');
            v.capable.push_back(d == std::string::npos ? conn : conn.substr(d + 1));
        }
    v.panels = static_cast<int>(glob_paths("/sys/class/drm/card*-*/status").size());
    return v;
}

VrrStatus vrr_status() { static Memo<VrrStatus> memo; return memo.get(30.0, [] { return vrr_status_raw(); }); }

bool daemon_running() {
    if (!path_exists("/run/tuxthrottle/control.sock")) return false;
    // look at /proc/*/comm directly: spawning pgrep every few seconds cost more CPU than the rest of the tray
    for (const auto& c : glob_paths("/proc/[0-9]*/comm")) {
        auto t = read_file(c);
        if (t && trim(*t) == "tuxthrottled") return true;
    }
    return false;
}

static std::vector<Backend> backends_raw() {
    std::vector<Backend> v;
    bool d = daemon_running();
    v.push_back({"Control", d ? "tuxthrottled daemon (socket)" : "direct (no daemon, sysfs + tools)", d});
    bool ry = ryzenadj_available();
    v.push_back({"CPU power limits", ry ? "ryzenadj" : cpu_is_amd() ? "not available (ryzenadj missing)" : "not available (not an AMD CPU)", ry});
    v.push_back({"CPU power reading", !powercap_energy_paths().empty() ? "RAPL powercap (sysfs)" : "not available", !powercap_energy_paths().empty()});
    {
        std::string igpu = has_amd_gpu() ? "amdgpu sysfs" : "";
        std::string dgpu;
        if (has_nvidia_gpu()) {
            // only look for the library file: dlopen-ing NVML costs ~15 MB of resident memory in every process that asks
            bool nvml = !glob_paths("/usr/lib*/libnvidia-ml.so.1").empty() || !glob_paths("/usr/lib/*/libnvidia-ml.so.1").empty();
            dgpu = nvml ? "NVML" : !which("nvidia-smi").empty() ? "nvidia-smi" : "";
        }
        std::string both = igpu + (igpu.empty() || dgpu.empty() ? "" : " + ") + dgpu;
        v.push_back({"GPU readings", both.empty() ? "not available" : both, !both.empty()});
    }
    {
        std::string fan = !hwmon_by_name("alienware_wmi").empty() ? "alienware_wmi" : !hwmon_by_name("dell_smm").empty() ? "dell_smm" : "";
        v.push_back({"Fans", fan.empty() ? "not available" : fan + " (hwmon)", !fan.empty()});
    }
    {
        auto lim = battery_charge_limit_info();
        v.push_back({"Battery limit", lim.supported ? (lim.method == "libsmbios" ? "Dell libsmbios (firmware)" : "kernel sysfs") : "not available", lim.supported});
    }
    {
        bool orgb = !which("openrgb").empty();
        v.push_back({"Keyboard lighting", orgb ? "OpenRGB" : "not available (OpenRGB missing)", orgb});
    }
    {
        bool ks = !which("kscreen-doctor").empty();
        v.push_back({"Display", ks ? "kscreen-doctor (KDE)" : "not available", ks});
    }
    return v;
}

std::vector<Backend> backends() { static Memo<std::vector<Backend>> memo; return memo.get(120.0, [] { return backends_raw(); }); }   // what answers each job changes with installs, not by the second

std::string backend_summary() {
    std::vector<std::string> parts;
    for (const auto& b : backends()) {
        if (!b.ok) continue;
        if (b.area == "Control") parts.push_back(b.name.rfind("tuxthrottled", 0) == 0 ? "daemon" : "direct");
        else if (b.area == "Keyboard lighting") parts.push_back("OpenRGB");
        else if (b.area == "GPU readings") parts.push_back(b.name);
        else if (b.area == "CPU power limits") parts.push_back("ryzenadj");
    }
    return join(parts, " \xC2\xB7 ");
}

void set_session_user(const std::string& name) { g_session_user = name; }

static std::optional<std::pair<std::string, int>> real_user_uid() {
    auto from_name = [](const std::string& n) -> std::optional<std::pair<std::string, int>> {
        if (n.empty() || n == "root") return std::nullopt;
        if (struct passwd* p = getpwnam(n.c_str())) return std::make_pair(std::string(p->pw_name), static_cast<int>(p->pw_uid));
        return std::nullopt;
    };
    if (auto r = from_name(g_session_user)) return r;
    for (const char* e : {"SUDO_USER", "PKEXEC_USER"})
        if (auto r = from_name(env_str(e))) return r;
    for (const char* e : {"PKEXEC_UID", "SUDO_UID"}) {
        std::string v = env_str(e);
        if (v.empty()) continue;
        if (struct passwd* p = getpwuid(static_cast<uid_t>(std::atoi(v.c_str()))))
            if (p->pw_uid != 0) return std::make_pair(std::string(p->pw_name), static_cast<int>(p->pw_uid));
    }
    if (struct passwd* p = getpwuid(getuid()))
        if (p->pw_uid != 0) return std::make_pair(std::string(p->pw_name), static_cast<int>(p->pw_uid));
    return std::nullopt;
}

bool session_ready() {
    if (!is_root()) return true;
    auto ru = real_user_uid();
    if (!ru) return false;
    std::string uid = std::to_string(ru->second);
    if (!path_exists("/run/user/" + uid + "/wayland-0") && !path_exists("/run/user/" + uid + "/wayland-1") &&
        !path_exists("/tmp/.X11-unix/X0"))
        return false;
    for (const char* proc : {"kwin_wayland", "kwin_x11"})
        if (run_argv({"pgrep", "-u", uid, "-x", proc}, 3000).code == 0) return true;
    return false;
}

std::vector<std::string> session_cmd(const std::vector<std::string>& argv) {
    if (!is_root()) return argv;
    auto ru = real_user_uid();
    if (!ru) return argv;
    std::string rundir = "/run/user/" + std::to_string(ru->second);
    std::vector<std::string> out{"sudo", "-u", ru->first, "-H", "env", "XDG_RUNTIME_DIR=" + rundir,
                                 "DBUS_SESSION_BUS_ADDRESS=unix:path=" + rundir + "/bus", "WAYLAND_DISPLAY=wayland-0", "DISPLAY=:0"};
    out.insert(out.end(), argv.begin(), argv.end());
    return out;
}

static std::optional<PanelModes> panel_modes_raw() {
    if (which("kscreen-doctor").empty() || !session_ready()) return std::nullopt;
    auto r = run_argv_stdout(session_cmd({"kscreen-doctor", "-j"}), 6000);
    if (r.code != 0 || trim(r.out).empty()) return std::nullopt;
    Json data;
    try {
        data = Json::parse(r.out);
    } catch (const std::exception&) {
        return std::nullopt;
    }
    const auto& outs = data["outputs"].array();
    if (outs.empty()) return std::nullopt;
    auto key = [](const Json& o) {
        std::string n = o["name"].str();
        for (auto& c : n) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        return std::make_pair(starts_with(n, "EDP") || starts_with(n, "LVDS") ? 0 : 1, o["enabled"].as_bool() ? 0 : 1);
    };
    const Json* best = &outs[0];
    for (const auto& o : outs)
        if (key(o) < key(*best)) best = &o;
    auto id_str = [](const Json& v) {
        return v.is_string() ? v.as_string() : (v.is_number() ? std::to_string(static_cast<long long>(v.as_number())) : std::string());
    };
    PanelModes pm;
    pm.output = best->operator[]("name").str();
    pm.current_id = id_str((*best)["currentModeId"]);
    std::set<int> rates;
    for (const auto& m : (*best)["modes"].array()) {
        const Json& sz = m["size"];
        double w = sz["width"].as_number(), h = sz["height"].as_number(), hz = m["refreshRate"].as_number();
        if (!w || !h || !hz) continue;
        hz = std::round(hz * 1000) / 1000;
        PanelMode pmod{id_str(m["id"]), static_cast<int>(w), static_cast<int>(h), hz};
        pm.modes.push_back(pmod);
        rates.insert(static_cast<int>(std::lround(hz)));
        if (pmod.id == pm.current_id) pm.current_hz = hz;
    }
    pm.rates.assign(rates.begin(), rates.end());
    return pm;
}

std::optional<PanelModes> panel_modes() { static Memo<std::optional<PanelModes>> memo; return memo.get(4.0, [] { return panel_modes_raw(); }); }

Result set_panel_refresh(int hz) {
    auto info = panel_modes();
    if (!info) return {false, "kscreen-doctor not available (KDE / KScreen only)"};
    if (info->modes.empty()) return {false, "no modes reported for the panel"};
    const PanelMode* cur = nullptr;
    for (const auto& m : info->modes) if (m.id == info->current_id) cur = &m;
    std::vector<PanelMode> pool = info->modes;
    if (cur) {
        std::vector<PanelMode> same;
        for (const auto& m : pool) if (m.w == cur->w && m.h == cur->h) same.push_back(m);
        if (!same.empty()) pool = same;
    }
    const PanelMode* best = &pool[0];
    for (const auto& m : pool) {
        double da = std::fabs(m.hz - hz), db = std::fabs(best->hz - hz);
        if (da < db || (da == db && m.w * m.h > best->w * best->h)) best = &m;
    }
    std::string tag = std::to_string(best->w) + "x" + std::to_string(best->h) + "@" + std::to_string(static_cast<int>(std::lround(best->hz)));
    std::string last;
    for (const std::string& spec : {"output." + info->output + ".mode." + best->id, "output." + info->output + ".mode." + tag}) {
        auto r = run_argv(session_cmd({"kscreen-doctor", spec}), 15000);
        if (r.code == 0) return {true, tag};
        last = trim(r.out);
    }
    return {false, last.empty() ? "kscreen-doctor failed" : last};
}

// ---------------------------------------------------------------- touchpad (KWin D-Bus)
static std::string touchpad_device_path() {
    if (which("busctl").empty()) return "";
    auto r = run_argv_stdout(session_cmd({"busctl", "--user", "tree", "org.kde.KWin"}), 6000);
    if (r.code != 0) return "";
    static const std::regex re(R"((/org/kde/KWin/InputDevice/\S+))");
    for (auto it = std::sregex_iterator(r.out.begin(), r.out.end(), re); it != std::sregex_iterator(); ++it) {
        std::string path = (*it)[1];
        auto rr = run_argv_stdout(session_cmd({"busctl", "--user", "get-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", "touchpad"}), 6000);
        auto f = split_ws(rr.out);
        if (rr.code == 0 && f.size() >= 2 && f[0] == "b" && f[1] == "true") return path;
    }
    return "";
}

static std::optional<bool> touchpad_get_bool(const std::string& path, const char* prop) {
    auto r = run_argv_stdout(session_cmd({"busctl", "--user", "get-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", prop}), 6000);
    auto f = split_ws(r.out);
    if (r.code != 0 || f.size() < 2 || f[0] != "b") return std::nullopt;
    return f[1] == "true";
}

TouchpadInfo touchpad_info() {
    TouchpadInfo t;
    std::string path = touchpad_device_path();
    if (path.empty()) return t;
    t.available = true;
    auto r = run_argv_stdout(session_cmd({"busctl", "--user", "get-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", "name"}), 6000);
    if (r.code == 0) {
        std::string o = trim(r.out);
        size_t sp = o.find(' ');
        std::string n = sp == std::string::npos ? o : trim(o.substr(sp + 1));
        if (n.size() >= 2 && n.front() == '"' && n.back() == '"') n = n.substr(1, n.size() - 2);
        t.name = n;
    }
    t.enabled = touchpad_get_bool(path, "enabled");
    t.tap_to_click = touchpad_get_bool(path, "tapToClick");
    t.natural_scroll = touchpad_get_bool(path, "naturalScroll");
    t.disable_while_typing = touchpad_get_bool(path, "disableWhileTyping");
    return t;
}

static Result touchpad_set_bool(const char* prop, bool value) {
    std::string path = touchpad_device_path();
    if (path.empty()) return {false, "no touchpad reachable via KWin D-Bus (KDE Plasma / Wayland only)"};
    auto r = run_argv(session_cmd({"busctl", "--user", "set-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", prop, "b",
                                   value ? "true" : "false"}), 6000);
    if (r.code != 0) return {false, trim(r.out).empty() ? "busctl set-property failed" : trim(r.out)};
    return {true, std::string(prop) + " -> " + (value ? "True" : "False")};
}
Result set_touchpad_enabled(bool on) { return touchpad_set_bool("enabled", on); }
Result set_touchpad_tap_to_click(bool on) { return touchpad_set_bool("tapToClick", on); }
Result set_touchpad_natural_scroll(bool on) { return touchpad_set_bool("naturalScroll", on); }
Result set_touchpad_disable_while_typing(bool on) { return touchpad_set_bool("disableWhileTyping", on); }

SnapshotLight snapshot_light() {
    SnapshotLight s;
    s.cpu_temp_c = cpu_temp_c();
    s.cpu_freq_ghz = cpu_freq_ghz();
    auto d = dgpu_values();
    s.dgpu_temp_c = d.temp_c ? std::optional<double>(*d.temp_c) : std::nullopt;
    s.dgpu_clock_mhz = d.clock_mhz;
    s.dgpu_power_w = d.power_w;
    auto ra = read_ryzenadj_info();
    if (ra) s.stapm_w = ra->stapm_value ? ra->stapm_value : ra->stapm_limit;
    for (const auto& f : read_fans()) s.fan_rpm.push_back(f.rpm);
    return s;
}

}  // namespace tt::sensors
