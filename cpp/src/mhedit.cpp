#include "mhedit.hpp"

#include <algorithm>
#include <cmath>
#include <regex>

#include "util.hpp"

namespace tt::mhedit {

static const std::regex kKey(R"(([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*))");
static const std::regex kBare(R"([A-Za-z_][A-Za-z0-9_]*)");

Conf parse_conf(const std::string& text) {
    Conf out;
    for (const auto& ln : split_lines(text)) {
        std::string s = trim(ln);
        if (s.empty() || s[0] == '#') continue;
        static const std::regex special(R"((cpu_text|gpu_text|position|offset_x|offset_y|font_size)\s*=\s*(.*))");
        std::smatch m;
        if (std::regex_match(s, m, special)) {
            std::string key = m[1], val = trim(m[2].str());
            if (key == "cpu_text") out.cpu = val;
            else if (key == "gpu_text") {
                { std::string cur; for (char c : val + ",") { if (c == ',') { std::string t = trim(cur); if (!t.empty()) out.gpus.push_back(t); cur.clear(); } else cur += c; } }
                out.gpu = join(out.gpus, ", ");
            } else if (key == "position") out.position = val;
            else {
                char* end = nullptr;
                double d = std::strtod(val.c_str(), &end);
                if (end != val.c_str() && *end == 0) {
                    int n = static_cast<int>(d);
                    if (key == "offset_x") out.offset_x = n; else if (key == "offset_y") out.offset_y = n; else out.font_size = n;
                }
            }
        } else if (std::regex_match(s, kBare)) out.elements.insert(s);
        else {
            static const std::regex kv(R"(([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+))");
            if (std::regex_match(s, m, kv)) {
                std::string v = lower(trim(m[2].str()));
                if (v == "1" || v == "true" || v == "on" || v == "yes") out.elements.insert(m[1]);
                else if (v == "0" || v == "false" || v == "off" || v == "no") out.off.insert(m[1]);
            }
        }
    }
    return out;
}

void set(Managed& m, const std::string& key, const Val& v) {
    for (auto& kv : m) if (kv.first == key) { kv.second = v; return; }
    m.push_back({key, v});
}

std::string merge_conf(const std::string& existing, const Managed& managed) {
    std::vector<std::string> header, order;
    std::vector<std::pair<std::string, std::optional<std::string>>> vals;
    auto find = [&](const std::string& k) { for (size_t i = 0; i < vals.size(); ++i) if (vals[i].first == k) return static_cast<int>(i); return -1; };
    for (const auto& ln : split_lines(existing)) {
        std::string s = trim(ln);
        if (s.empty()) continue;
        if (s[0] == '#') { if (order.empty()) header.push_back(s); continue; }
        std::string key;
        std::optional<std::string> val;
        std::smatch m;
        if (std::regex_match(s, m, kKey)) { key = m[1]; val = trim(m[2].str()); }
        else if (std::regex_match(s, kBare)) key = s;
        else continue;
        int i = find(key);
        if (i < 0) { order.push_back(key); vals.push_back({key, val}); } else vals[static_cast<size_t>(i)].second = val;
    }
    for (const auto& [k, v] : managed) {
        int i = find(k);
        if (v.kind == Val::Remove) {
            if (i >= 0) { vals.erase(vals.begin() + i); order.erase(std::find(order.begin(), order.end(), k)); }
        } else {
            if (i < 0) { order.push_back(k); vals.push_back({k, std::nullopt}); i = static_cast<int>(vals.size()) - 1; }
            vals[static_cast<size_t>(i)].second = v.kind == Val::Bare ? std::nullopt : std::optional<std::string>(v.v);
        }
    }
    std::vector<std::string> out = header;
    if (out.empty()) out.push_back("# MangoHud config (managed by TuxThrottle)");
    for (const auto& k : order) {
        int i = find(k);
        out.push_back(vals[static_cast<size_t>(i)].second ? k + "=" + *vals[static_cast<size_t>(i)].second : k);
    }
    std::string text = join(out, "\n");
    while (!text.empty() && (text.back() == '\n' || text.back() == ' ')) text.pop_back();
    return text + "\n";
}

const std::vector<Group>& groups() {
    static const std::vector<Group> g = {
        {"cpu", {"cpu_stats"}, {"cpu_stats", "cpu_temp", "cpu_power"}},
        {"gpu", {"gpu_stats"}, {"gpu_stats", "gpu_temp", "gpu_power"}},
        {"mem", {"ram"}, {"ram", "vram"}},
    };
    return g;
}
const std::vector<std::string>& always() { static const std::vector<std::string> v = {"fps", "engine_version"}; return v; }
const std::vector<std::string>& frame_graph() { static const std::vector<std::string> v = {"frametime", "frame_timing"}; return v; }
const std::set<std::string>& stat_keys() {
    static const std::set<std::string> s = {
        "fps", "fps_only", "frametime", "frame_timing", "frame_count", "cpu_stats", "cpu_temp", "cpu_power", "cpu_mhz", "cpu_load_change",
        "core_load", "core_load_change", "gpu_stats", "gpu_temp", "gpu_power", "gpu_core_clock", "gpu_mem_clock", "gpu_load_change", "gpu_name",
        "gpu_voltage", "gpu_fan", "gpu_throttling", "vram", "ram", "swap", "procmem", "procmem_shared", "procmem_virt", "io_read", "io_write",
        "io_stats", "arch", "engine_version", "vulkan_driver", "wine", "exec_name", "gamemode", "vkbasalt", "battery", "media_player",
        "resolution", "show_fps_limit", "throttling_status", "throttling_status_graph", "fan", "present_mode", "refresh_rate", "winesync", "version"};
    return s;
}

std::string level_of(const std::set<std::string>& els, const std::string& group) {
    for (const auto& g : groups()) {
        if (group != g.name) continue;
        for (const auto& e : g.full) {
            if (std::find(g.minimal.begin(), g.minimal.end(), e) != g.minimal.end()) continue;
            if (els.count(e)) return "full";
        }
    }
    return "minimal";
}

std::optional<std::string> hud_width(const std::vector<std::string>& labels, bool full, int font_size) {
    size_t longest = 0;
    for (const auto& l : labels) if (!l.empty()) longest = std::max(longest, utf8_len(l));
    if (longest <= 8) return std::nullopt;
    int fs = font_size ? font_size : 24;
    int pad = full ? 12 : 8;
    int w = static_cast<int>(longest * fs * 0.85) + static_cast<int>(fs * pad);
    return std::to_string(std::max(320, std::min(1700, w)));
}

std::optional<std::string> gpu_list(int n_gpu, const std::vector<std::string>& pcis_in, const std::vector<std::string>& order) {
    if (n_gpu <= 1) return std::nullopt;
    std::vector<std::string> fb;
    for (int i = 0; i < n_gpu; ++i) fb.push_back(std::to_string(i));
    std::string fallback = join(fb, ",");
    std::vector<std::string> pcis;
    for (const auto& p : pcis_in) if (!p.empty() && static_cast<int>(pcis.size()) < n_gpu) pcis.push_back(p);
    if (static_cast<int>(pcis.size()) != n_gpu) return fallback;
    if (order.empty()) return fallback;
    std::vector<int> idx;
    for (const auto& p : pcis) {
        auto it = std::find(order.begin(), order.end(), p);
        if (it == order.end()) return fallback;
        idx.push_back(static_cast<int>(it - order.begin()));
    }
    std::set<int> uniq(idx.begin(), idx.end());
    if (static_cast<int>(uniq.size()) != n_gpu) return fallback;
    std::vector<std::string> s;
    for (int i : idx) s.push_back(std::to_string(i));
    return join(s, ",");
}

Managed build_managed(const Editor& e) {
    Managed m;
    std::vector<std::string> gp;
    for (const auto& g : e.gpus) { std::string t = trim(g); if (!t.empty() && t != "detecting...") gp.push_back(t); }
    std::string cpu = trim(e.cpu);
    bool full = e.full_cpu || e.full_gpu || e.full_mem;
    std::vector<std::string> labels{cpu};
    labels.insert(labels.end(), gp.begin(), gp.end());
    auto width = hud_width(labels, full, e.font_size);
    set(m, "cpu_text", cpu.empty() ? Val::remove() : Val::value(cpu));
    set(m, "gpu_text", gp.empty() ? Val::remove() : Val::value(join(gp, ",")));
    auto gl = gpu_list(static_cast<int>(e.gpus.size()), e.pcis, e.mangohud_gpu_order);
    set(m, "gpu_list", gl ? Val::value(*gl) : Val::remove());
    set(m, "width", width ? Val::value(*width) : Val::remove());
    for (const auto& k : stat_keys()) set(m, k, Val::remove());
    std::vector<std::string> els = always();
    if (e.graph) els.insert(els.end(), frame_graph().begin(), frame_graph().end());
    if (e.gpuname) els.push_back("gpu_name");
    bool fl[3] = {e.full_cpu, e.full_gpu, e.full_mem};
    int gi = 0;
    for (const auto& g : groups()) { const auto& src = fl[gi++] ? g.full : g.minimal; els.insert(els.end(), src.begin(), src.end()); }
    if (e.core_clock) els.push_back("gpu_core_clock");
    if (e.mem_clock) els.push_back("gpu_mem_clock");
    if (e.gamemode) els.push_back("gamemode");
    for (const auto& el : els) set(m, el, Val::bare());
    for (const char* k : {"frametime", "frame_timing", "gpu_name"}) set(m, k, Val::value(std::find(els.begin(), els.end(), k) != els.end() ? "1" : "0"));
    if (e.pos_set) {
        set(m, "position", Val::value(e.pos));
        set(m, "offset_x", e.ox ? Val::value(std::to_string(e.ox)) : Val::remove());
        set(m, "offset_y", e.oy ? Val::value(std::to_string(e.oy)) : Val::remove());
    }
    return m;
}

std::string clean_baseline() {
    return "# MangoHud config (managed by TuxThrottle)\nposition=top-left\nfont_size=20\nbackground_alpha=0.4\nround_corners=6\ntoggle_hud=Shift_R+F12\n";
}

static void anchors(const std::string& pos, int sw, int sh, double* ax, double* ay) {
    *ax = pos.find("left") != std::string::npos ? 0 : pos.find("right") != std::string::npos ? sw - kBoxW : (sw - kBoxW) / 2.0;
    *ay = pos.find("top") != std::string::npos ? 0 : pos.find("bottom") != std::string::npos ? sh - kBoxH : (sh - kBoxH) / 2.0;
}

Anchor anchor_offset(double bx, double by, int sw, int sh) {
    double cx = bx + kBoxW / 2.0, cy = by + kBoxH / 2.0;
    std::string h = cx < sw / 3.0 ? "left" : cx >= 2 * sw / 3.0 ? "right" : "center";
    std::string v = cy < sh / 3.0 ? "top" : cy >= 2 * sh / 3.0 ? "bottom" : "middle";
    if (h == "center" && v == "middle") v = cy < sh / 2.0 ? "top" : "bottom";
    std::string pos;
    if (v == "middle") { h = cx < sw / 2.0 ? "left" : "right"; pos = "middle-" + h; }
    else if (h == "center") pos = v + "-center";
    else pos = v + "-" + h;
    double ax, ay;
    anchors(pos, sw, sh, &ax, &ay);
    return {pos, static_cast<int>(std::nearbyint(bx - ax)), static_cast<int>(std::nearbyint(by - ay))};
}

std::pair<double, double> box_xy(const std::string& pos, int ox, int oy, int sw, int sh) {
    double ax, ay;
    anchors(pos, sw, sh, &ax, &ay);
    return {std::max(0.0, std::min<double>(sw - kBoxW, ax + ox)), std::max(0.0, std::min<double>(sh - kBoxH, ay + oy))};
}

}  // namespace tt::mhedit
