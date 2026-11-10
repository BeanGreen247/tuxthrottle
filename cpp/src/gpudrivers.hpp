// One-click GPU driver installer: detect, plan (dry run), apply, roll back (port of tuxthrottle_gpudrivers.py).
#pragma once
#include <string>
#include <vector>

#include "distro.hpp"
#include "json.hpp"

namespace tt::gpudrivers {

struct Gpu {
    std::string slot, vendor, device_id, driver, kind = "discrete", generation;   // generation: nvidia only
    bool boot_vga = false;
};
struct Step {
    std::string title;
    std::vector<std::string> cmds, undo;
    bool manual = false;
    std::string note;
};
struct Plan {
    std::string distro, layout;
    std::vector<Gpu> gpus;
    std::vector<Step> steps;
    std::vector<std::string> warnings;
    bool reboot = false;
};

std::string nvidia_generation(const std::string& device_id);   // open | proprietary | legacy | unsupported
std::vector<Gpu> scan_gpus(const std::string& pci_root = "/sys/bus/pci/devices");
std::string layout(const std::vector<Gpu>& gpus);
// sb: -1 = detect, 0 = off, 1 = on
Plan build_plan(const std::vector<Gpu>& gpus, const Distro& d, bool want32 = true, bool want_power = true, int sb = -1, bool force = false);
std::string render(const Plan& plan);
Json plan_json(const Plan& plan);
int apply(const Plan& plan);
int rollback();
int main(const std::vector<std::string>& args);

}  // namespace tt::gpudrivers
