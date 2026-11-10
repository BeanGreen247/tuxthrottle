#include "bench.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <stdexcept>

#include "util.hpp"

namespace tt::bench {

static constexpr double kStutterFactor = 2.0;

std::vector<Frame> parse_log(const std::string& text) {
    auto lines = split_lines(text);
    size_t hdr = lines.size();
    for (size_t i = 0; i < lines.size(); ++i)
        if (starts_with(lower(lines[i]), "fps,")) { hdr = i; break; }
    if (hdr == lines.size()) throw std::runtime_error("no MangoHud 'fps,frametime,...' header found");

    std::vector<std::string> cols;
    {
        std::string cur;
        for (char c : lines[hdr] + ",") {
            if (c == ',') { cols.push_back(trim(cur)); cur.clear(); }
            else cur += c;
        }
    }
    auto idx = [&](const char* name) {
        for (size_t i = 0; i < cols.size(); ++i)
            if (cols[i] == name) return static_cast<int>(i);
        return -1;
    };
    int i_ft = idx("frametime"), i_cl = idx("cpu_load"), i_gl = idx("gpu_load"), i_ct = idx("cpu_temp"),
        i_gt = idx("gpu_temp"), i_gp = idx("gpu_power");
    if (i_ft < 0) throw std::runtime_error("no frame data");

    std::vector<Frame> out;
    for (size_t li = hdr + 1; li < lines.size(); ++li) {
        std::vector<double> v;
        std::string cur;
        bool ok = true;
        auto flush = [&] {
            char* end = nullptr;
            double d = std::strtod(cur.c_str(), &end);
            if (cur.empty() || end == cur.c_str()) ok = false;
            v.push_back(d);
            cur.clear();
        };
        for (char c : lines[li] + ",") {
            if (c == ',') flush();
            else cur += c;
        }
        if (!ok || static_cast<int>(v.size()) <= i_ft) continue;
        auto at = [&](int i) { return i >= 0 && i < static_cast<int>(v.size()) ? v[static_cast<size_t>(i)] : 0.0; };
        out.push_back({at(i_ft), at(i_cl), at(i_gl), at(i_ct), at(i_gt), at(i_gp)});
    }
    if (out.empty()) throw std::runtime_error("no frame data");
    return out;
}

static double percentile(const std::vector<double>& sorted, double p) {
    // nearest-rank percentile: the smallest value with at least p% of samples at or below it
    long i = static_cast<long>(std::ceil(p / 100.0 * static_cast<double>(sorted.size()) - 1e-9)) - 1;
    i = std::clamp<long>(i, 0, static_cast<long>(sorted.size()) - 1);
    return sorted[static_cast<size_t>(i)];
}

static std::optional<double> avg_pos(const std::vector<Frame>& f, double Frame::*m) {
    double sum = 0;
    size_t n = 0;
    for (const auto& x : f)
        if (x.*m > 0) { sum += x.*m; ++n; }
    if (!n) return std::nullopt;
    return sum / static_cast<double>(n);
}

static double r1(double v) { return std::round(v * 10) / 10; }
static double r2(double v) { return std::round(v * 100) / 100; }

Stats stats(const std::vector<Frame>& frames) {
    std::vector<double> ft;
    for (const auto& f : frames)
        if (f.frametime_ms > 0) ft.push_back(f.frametime_ms);
    if (ft.empty()) throw std::runtime_error("no frame data");
    std::sort(ft.begin(), ft.end());
    double total = 0;
    for (double v : ft) total += v;
    double median = ft[ft.size() / 2];
    Stats s;
    s.frames = ft.size();
    s.seconds = r1(total / 1000);
    s.avg_fps = r1(static_cast<double>(ft.size()) / total * 1000);
    double p99 = percentile(ft, 99), p999 = percentile(ft, 99.9);
    s.low1_fps = r1(1000 / p99);
    s.low01_fps = r1(1000 / p999);
    s.p99_ms = r2(p99);
    s.max_ms = r2(ft.back());
    for (double v : ft)
        if (v > kStutterFactor * median) ++s.stutters;
    s.cpu_load = avg_pos(frames, &Frame::cpu_load);
    s.gpu_load = avg_pos(frames, &Frame::gpu_load);
    s.cpu_temp = avg_pos(frames, &Frame::cpu_temp);
    s.gpu_temp = avg_pos(frames, &Frame::gpu_temp);
    s.gpu_power = avg_pos(frames, &Frame::gpu_power);
    return s;
}

std::vector<Delta> compare(const Stats& a, const Stats& b) {
    std::vector<Delta> out;
    auto add = [&](const char* label, std::optional<double> va, std::optional<double> vb, bool higher_better) {
        if (!va || !vb) return;
        Delta d;
        d.metric = label;
        d.a = r2(*va);
        d.b = r2(*vb);
        d.delta_pct = *va != 0 ? r1((*vb - *va) / *va * 100) : 0;
        if (std::fabs(d.delta_pct) < 1) d.verdict = "same";
        else d.verdict = ((d.delta_pct > 0) == higher_better) ? "better" : "worse";
        out.push_back(std::move(d));
    };
    add("Average FPS", a.avg_fps, b.avg_fps, true);
    add("1% low FPS", a.low1_fps, b.low1_fps, true);
    add("0.1% low FPS", a.low01_fps, b.low01_fps, true);
    add("p99 frametime ms", a.p99_ms, b.p99_ms, false);
    add("Worst frame ms", a.max_ms, b.max_ms, false);
    add("Stutters (>2x median)", static_cast<double>(a.stutters), static_cast<double>(b.stutters), false);
    add("CPU load %", a.cpu_load, b.cpu_load, false);
    add("GPU load %", a.gpu_load, b.gpu_load, false);
    add("CPU temp C", a.cpu_temp, b.cpu_temp, false);
    add("GPU temp C", a.gpu_temp, b.gpu_temp, false);
    add("GPU power W", a.gpu_power, b.gpu_power, false);
    return out;
}

std::string mangohud_env(const std::string& out_dir, int delay, int duration) {
    return "MANGOHUD_CONFIG=output_folder=" + out_dir + ",autostart_log=" + std::to_string(delay) +
           ",log_duration=" + std::to_string(duration) + ",log_interval=0,log_versioning=1 mangohud";
}

}  // namespace tt::bench
