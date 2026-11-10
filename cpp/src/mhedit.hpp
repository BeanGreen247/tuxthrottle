// MangoHud overlay editor logic (the MangoHud tab's "overlay editor"): load / clean-merge write of MangoHud.conf,
// width + gpu_list computation, anchor/offset maths for the placement picker. Pure functions, GUI-free.
#pragma once
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace tt::mhedit {

struct Conf {
    std::string cpu, gpu, position;
    std::vector<std::string> gpus;
    int offset_x = 0, offset_y = 0, font_size = 0;
    std::set<std::string> elements, off;   // bare / =1 toggles, and =0 toggles
};
Conf parse_conf(const std::string& text);

struct Val {
    enum Kind { Remove, Bare, Value } kind = Remove;
    std::string v;
    static Val remove() { return {Remove, ""}; }
    static Val bare() { return {Bare, ""}; }
    static Val value(const std::string& s) { return {s.empty() ? Remove : Value, s}; }
};
using Managed = std::vector<std::pair<std::string, Val>>;   // insertion-ordered; setting an existing key keeps its slot
void set(Managed& m, const std::string& key, const Val& v);

// Clean rewrite: each key once (last value wins), leading comments kept, blanks / inline comments / junk dropped.
std::string merge_conf(const std::string& existing, const Managed& managed);

// per-group detail level
struct Group { const char* name; std::vector<std::string> minimal, full; };
const std::vector<Group>& groups();
const std::vector<std::string>& always();
const std::vector<std::string>& frame_graph();
const std::set<std::string>& stat_keys();
std::string level_of(const std::set<std::string>& elements, const std::string& group);   // "full" | "minimal"

struct Editor {
    std::string cpu;
    std::vector<std::string> gpus;       // one per GPU row (may be blank)
    bool full_cpu = false, full_gpu = false, full_mem = false;
    bool graph = false, gpuname = true, gamemode = false, core_clock = false, mem_clock = false;
    bool pos_set = false;
    std::string pos = "top-left";
    int ox = 0, oy = 0;
    std::vector<std::string> pcis;       // PCI per row (for gpu_list)
    int font_size = 0;                   // from the existing config
    std::vector<std::string> mangohud_gpu_order;
};
// Everything the "Write to MangoHud config" button sets.
Managed build_managed(const Editor& e);
std::optional<std::string> hud_width(const std::vector<std::string>& labels, bool full, int font_size);
std::optional<std::string> gpu_list(int n_gpu, const std::vector<std::string>& pcis, const std::vector<std::string>& order);
std::string clean_baseline();           // "Reset config (clean)" starting file

constexpr int kBoxW = 300, kBoxH = 240;
struct Anchor { std::string pos; int ox, oy; };
Anchor anchor_offset(double bx, double by, int sw, int sh);
std::pair<double, double> box_xy(const std::string& pos, int ox, int oy, int sw, int sh);

}  // namespace tt::mhedit
