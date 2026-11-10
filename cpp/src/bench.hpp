// Benchmark A/B: parse MangoHud frame logs and compare two runs.
#pragma once
#include <optional>
#include <string>
#include <vector>

namespace tt::bench {

struct Frame {
    double frametime_ms = 0, cpu_load = 0, gpu_load = 0, cpu_temp = 0, gpu_temp = 0, gpu_power = 0;
};

struct Stats {
    size_t frames = 0;
    double seconds = 0, avg_fps = 0, low1_fps = 0, low01_fps = 0, p99_ms = 0, max_ms = 0;
    size_t stutters = 0;
    std::optional<double> cpu_load, gpu_load, cpu_temp, gpu_temp, gpu_power;
};

struct Delta {
    std::string metric;
    double a = 0, b = 0, delta_pct = 0;
    std::string verdict;  // better | worse | same
};

// Throws std::runtime_error when the text has no MangoHud 'fps,frametime,...' header or no frames.
std::vector<Frame> parse_log(const std::string& text);
Stats stats(const std::vector<Frame>& frames);
std::vector<Delta> compare(const Stats& a, const Stats& b);
std::string mangohud_env(const std::string& out_dir, int delay, int duration);

}  // namespace tt::bench
