// VRAM budget: tiers (regular/medium/extreme), free-VRAM action, compositor GPU selector (port of tuxthrottle_vram.py).
#pragma once
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::vram {

extern const std::vector<std::string> kTiers;  // regular medium extreme
std::string current_tier();
std::string current_compositor_gpu();
std::vector<std::string> compositor_gpu_modes();
std::pair<bool, std::string> apply_tier(const std::string& tier);
std::pair<bool, std::string> set_compositor_gpu(const std::string& mode);
Json free_vram(bool restart_compositor);
Json status();
std::string format_status(const Json& s);
// CLI entry: args after the "vram" word. Returns an exit code.
int main(const std::vector<std::string>& args);

}  // namespace tt::vram
