// Hardware pages: Power & Limits, Battery, Display, Touchpad.
#include <algorithm>
#include <atomic>
#include <cmath>
#include <memory>
#include <mutex>

#include "../hwtools.hpp"
#include "../sensors.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {

// Live readouts shared by the pages (read on a worker; ryzenadj/nvidia-smi can take ~1 s each).
struct Live {
    std::mutex m;
    std::optional<sensors::RyzenInfo> tdp;
    std::optional<sensors::NvPowerLimit> nvpl;
    sensors::ChargeLimitInfo bat;
    std::optional<sensors::NvClockInfo> nvclk;
    sensors::BatteryHealth health;
    std::atomic<bool> inflight{false};
    double last = 0;
};
Live& live() { static Live l; return l; }

void poll(App& a, bool power, bool health) {
    Live& l = live();
    a.ui.wake_after(3000);
    double now = now_seconds();
    if (l.inflight || now - l.last < 3.0) return;
    l.inflight = true;
    l.last = now;
    a.run_bg([power, health] {
        Live& L = live();
        std::optional<sensors::RyzenInfo> tdp;
        std::optional<sensors::NvPowerLimit> nvpl;
        std::optional<sensors::NvClockInfo> nvclk;
        if (power) {
            if (sensors::ryzenadj_available()) tdp = sensors::read_ryzenadj_info();
            if (sensors::has_nvidia_gpu()) { nvpl = sensors::nvidia_power_limit_info(); nvclk = sensors::nvidia_clock_info(); }
        }
        auto bat = sensors::battery_charge_limit_info();
        sensors::BatteryHealth h;
        if (health) h = sensors::battery_health_info();
        std::lock_guard<std::mutex> g(L.m);
        L.tdp = tdp; L.nvpl = nvpl; L.nvclk = nvclk; L.bat = bat;
        if (health) L.health = h;
        L.inflight = false;
    });
}

// ---- shared battery charge-limit state (one instance synced across the Power and Battery pages) ----
struct BatState { int pct = 100; bool init = false; } g_bat;

void battery_section(App& a) {
    Ui& ui = a.ui;
    Live& l = live();
    sensors::ChargeLimitInfo info;
    { std::lock_guard<std::mutex> g(l.m); info = l.bat; }
    ui.card_begin("Battery charge limit");
    if (!info.supported) {
        std::string msg = "This machine doesn't expose a charge-stop threshold (no charge_control_end_threshold in sysfs).";
        if (info.dell_libsmbios_possible) msg += "  On this Dell you can still get a firmware-level charge limit - install the \"Dell battery threshold (libsmbios)\" tweak on the Power tab, then reopen this tab.";
        ui.wrapped(msg, Tone::Muted);
        ui.card_end();
        return;
    }
    if (!g_bat.init && info.current) { g_bat.pct = *info.current; g_bat.init = true; }
    ui.wrapped(std::string("Stops charging at the set level to spare the cell when the laptop mostly runs on AC. 80 % is the usual longevity sweet spot. Controlled via ") + (info.method == "libsmbios" ? "firmware (libsmbios)." : "kernel sysfs."), Tone::Muted);
    auto apply = [&](int p) {
        g_bat.pct = p;
        Json j = Json::obj();
        j.set("percent", p);
        a.write_state("battery.json", j);
        auto r = sensors::set_battery_charge_limit(p);
        a.log("[Power] battery charge limit -> " + std::to_string(p) + "%" + (r.first ? "" : "  FAILED: " + r.second));
    };
    ui.label_c("Stop charging at", Tone::Normal, Font::Regular, ui.S(180));
    ui.same_line(ui.S(10));
    int v = g_bat.pct;
    ui.push_id("batlimit");
    ui.slider("s", &v, 50, 100, ui.S(300), "%");
    ui.pop_id();
    v = static_cast<int>(std::lround(v / 5.0)) * 5;
    g_bat.pct = v;
    if (ui.released()) apply(v);
    ui.same_line(ui.S(12));
    ui.label_c(info.current ? "now: " + std::to_string(*info.current) + " %" : "now: - %", Tone::Muted);
    for (auto [lbl, p] : {std::pair<const char*, int>{"60 %", 60}, {"80 %", 80}, {"Full (100 %)", 100}}) {
        if (ui.button_flow(lbl, Btn::Outline)) apply(p);
    }
    ui.flow_reset();
    ui.card_end();
}

// ---------------------------------------------------------------- Power & Limits
struct Power : Page {
    Power() { title = "Power & Limits"; group = "Hardware"; }
    struct Preset { const char* name; int stapm, fast, slow; };
    const Preset presets[3] = {{"Quiet", 25, 35, 25}, {"Balanced", 42, 54, 42}, {"Performance", 65, 80, 54}};

    int tdp[3] = {45, 45, 45};   // stapm fast slow
    bool tdp_init = false;
    int co = 0, nv_w = 60, clk = 2100, clk_lo = 210, clk_hi = 2100, mode = 1;
    bool nv_init = false, clk_init = false, co_init = false, aw_init = false, aw_on = false;
    int aw_ac = 1, aw_bat = 0, aw_hz_ac = 0, aw_hz_bat = 0;
    std::vector<int> rates;
    // clock offset probe
    std::shared_ptr<std::pair<std::mutex, std::optional<sensors::NvOffsetInfo>>> off = std::make_shared<std::pair<std::mutex, std::optional<sensors::NvOffsetInfo>>>();
    bool off_checking = false, off_done = false;
    int off_core = 0, off_mem = 0;
    double off_deadline = 0;
    std::string confirm_text;

    bool close_hook_set = false;
    bool off_applied = false;
    void enter(App& a) override { live().last = 0; (void)a; }

    void write_tdp(App& a, const std::string& note) {
        Json j = Json::obj();
        j.set("stapm", tdp[0]); j.set("fast", tdp[1]); j.set("slow", tdp[2]);
        a.write_state("tdp.json", j);
        int s = tdp[0], f = tdp[1], sl = tdp[2];
        a.run_bg([&a, s, f, sl, note] {
            auto r = sensors::set_ryzenadj_limits(f, sl, s);
            a.log("[Power] TDP -> STAPM " + std::to_string(s) + " / fast " + std::to_string(f) + " / slow " + std::to_string(sl) + " W" + (note.empty() ? "" : " (" + note + ")") + (r.first ? "" : "  FAILED: " + r.second));
        });
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Power & Limits", "Live power/thermal envelope controls - the Linux equivalent of ThrottleStop / the ASUS Armoury tuning sliders. Changes apply immediately; installing the matching tweak on the Power tab makes them stick across a reboot.");
        poll(a, true, false);
        Live& l = live();
        std::optional<sensors::RyzenInfo> tdp_i;
        std::optional<sensors::NvPowerLimit> nvpl;
        std::optional<sensors::NvClockInfo> nvclk;
        { std::lock_guard<std::mutex> g(l.m); tdp_i = l.tdp; nvpl = l.nvpl; nvclk = l.nvclk; }

        // --- CPU TDP ---
        ui.card_begin("CPU power limits (ryzenadj)");
        if (!sensors::ryzenadj_available()) ui.wrapped("ryzenadj isn't installed. Add the \"CPU TDP control (ryzenadj)\" tweak on the Power tab, then reopen this tab.", Tone::Warning);
        else {
            if (!tdp_init) {
                Json sv = a.read_state("tdp.json");
                if (sv["stapm"].is_number()) { tdp[0] = static_cast<int>(sv["stapm"].as_number()); tdp[1] = static_cast<int>(sv["fast"].as_number()); tdp[2] = static_cast<int>(sv["slow"].as_number()); tdp_init = true; }
                else if (tdp_i && tdp_i->stapm_limit) { tdp[0] = static_cast<int>(*tdp_i->stapm_limit); tdp[1] = static_cast<int>(tdp_i->fast_limit.value_or(tdp[0])); tdp[2] = static_cast<int>(tdp_i->slow_limit.value_or(tdp[0])); tdp_init = true; }
            }
            ui.wrapped("STAPM = sustained limit (long window), Fast = short burst, Slow = medium window. Higher = more performance, more heat.", Tone::Muted);
            const char* caps[3] = {"STAPM (sustained)", "Fast (burst)", "Slow (medium)"};
            const std::optional<double>* nowv[3] = {nullptr, nullptr, nullptr};
            std::optional<double> nv[3];
            if (tdp_i) { nv[0] = tdp_i->stapm_limit; nv[1] = tdp_i->fast_limit; nv[2] = tdp_i->slow_limit; }
            (void)nowv;
            for (int i = 0; i < 3; ++i) {
                ui.push_id(i);
                ui.label_c(caps[i], Tone::Normal, Font::Regular, ui.S(180));
                ui.same_line(ui.S(10));
                ui.slider("w", &tdp[i], 10, 90, ui.S(300), " W");
                if (ui.released()) write_tdp(a, "");
                ui.same_line(ui.S(10));
                ui.label_c(nv[i] ? "now: " + format_double(*nv[i], 0) + " W" : "now: - W", Tone::Muted);
                ui.pop_id();
            }
            ui.label_c("Presets:", Tone::Muted);
            for (const auto& p : presets) {
                ui.same_line();
                if (ui.button(p.name, Btn::OutlineInfo)) { tdp[0] = p.stapm; tdp[1] = p.fast; tdp[2] = p.slow; write_tdp(a, std::string("preset ") + p.name); }
            }
            ui.same_line(ui.S(14));
            if (ui.button("Firmware default", Btn::Outline)) { tdp[0] = 54; tdp[1] = 65; tdp[2] = 54; write_tdp(a, "firmware default"); }
        }
        ui.card_end();

        // --- Curve Optimizer ---
        if (sensors::ryzenadj_co_supported()) {
            if (!co_init) { co = static_cast<int>(a.read_state("co.json")["offset"].as_number()); co_init = true; }
            Json cj = a.read_state("co.json");
            ui.card_begin("Curve Optimizer - all-core undervolt  (advanced)");
            ui.wrapped("An undervolt that's too aggressive causes silent errors, a segfault storm, or a hard hang (needs a full power-off). 'Apply & stress-test' snapshots first, runs a stress-ng + GPU load for 5 min while watching dmesg for MCE/WHEA, and auto-reverts on any fault. The offset is NOT kept across a reboot until you press \"Keep\".", Tone::Danger);
            ui.label_c("All-core offset", Tone::Normal, Font::Regular, ui.S(180));
            ui.same_line(ui.S(10));
            ui.slider("co", &co, -40, 0, ui.S(300));
            ui.same_line(ui.S(10));
            ui.label_c(cj.object().empty() ? "now: stock (0)" : "now: " + format_double(cj["offset"].as_number(), 0) + "  " + (cj["confirmed"].as_bool() ? "kept" : "not kept (reboots off)"), Tone::Muted);
            if (ui.button("Apply & stress-test (5 min)", Btn::OutlineWarn, 0, !a.busy())) {
                if (co >= 0) a.message("Curve Optimizer", "Set a negative offset first (e.g. -20)."); else ui.open_popup("confirm:co-stress");
            }
            ui.same_line();
            if (ui.button("Keep (confirm)", Btn::OutlineSuccess)) a.run_shell_task("Curve Optimizer confirm", shell_quote(ctl_exe()) + " co confirm --user " + shell_quote(a.user));
            ui.same_line();
            if (ui.button("Revert to 0", Btn::Outline)) a.run_shell_task("Curve Optimizer revert", shell_quote(ctl_exe()) + " co revert --user " + shell_quote(a.user));
            ui.card_end();
            if (a.confirm("co-stress", "Stress-test undervolt", "Apply --set-coall=" + std::to_string(co) + " and hammer the CPU + GPU for 5 minutes?\n\nIt snapshots first and auto-reverts on any kernel error, but a bad offset can still hard-hang the machine (recoverable only by a full power-off). Continue?", "Apply & test"))
                a.run_shell_task("Curve Optimizer stress-test (offset " + std::to_string(co) + ")", shell_quote(ctl_exe()) + " co apply " + std::to_string(co) + " --minutes 5 --user " + shell_quote(a.user));
        }

        if (sensors::has_nvidia_gpu()) {
            // --- NVIDIA board power limit ---
            ui.card_begin("NVIDIA board power limit");
            if (nvpl && !nvpl->supported) ui.wrapped("This laptop's GPU firmware locks the board power limit (NVIDIA Dynamic Boost manages it) - nvidia-smi -pl is rejected. Nothing to set here. Use the 'nvidia-max-perf' GPU tweak + the CPU TDP slider above to influence the shared power/thermal budget instead.", Tone::Warning);
            else {
                if (!nv_init && nvpl && nvpl->current) { nv_w = *nvpl->current; nv_init = true; }
                int lo = nvpl ? nvpl->min : 30, hi = nvpl ? nvpl->max : 80;
                ui.label_c("Power limit", Tone::Normal, Font::Regular, ui.S(180));
                ui.same_line(ui.S(10));
                ui.slider("nvpl", &nv_w, lo, hi, ui.S(300), " W");
                if (ui.released()) {
                    Json j = Json::obj();
                    j.set("watts", nv_w);
                    a.write_state("nvpl.json", j);
                    int w = nv_w;
                    a.run_bg([&a, w] { auto r = sensors::set_nvidia_power_limit(w); a.log("[Power] NVIDIA power limit -> " + std::to_string(w) + " W" + (r.first ? "" : "  FAILED: " + r.second)); });
                }
                ui.same_line(ui.S(10));
                ui.label_c(nvpl && nvpl->current ? "now: " + std::to_string(*nvpl->current) + " W" : "now: asleep", Tone::Muted);
                if (nvpl && nvpl->def && ui.button("Default (" + std::to_string(*nvpl->def) + " W)", Btn::Outline)) {
                    nv_w = *nvpl->def;
                    int w = nv_w;
                    a.run_bg([&a, w] { auto r = sensors::set_nvidia_power_limit(w); a.log("[Power] NVIDIA power limit -> " + std::to_string(w) + " W" + (r.first ? "" : "  FAILED: " + r.second)); });
                }
                if (!nvpl) ui.wrapped("dGPU is asleep - wake it (run something on it) to read/set the limit.", Tone::Muted);
            }
            ui.card_end();

            // --- clock lock ---
            ui.card_begin("NVIDIA GPU clock lock");
            ui.wrapped("Clamps the dGPU graphics clock. Lowering the ceiling is the one GPU lever that works on chassis where the board power limit is firmware-locked - good for heat and battery; raising it back to the max is the default. Applies immediately; the \"GPU clock lock at boot\" tweak re-applies it after a reboot / resume.", Tone::Muted);
            if (!nvclk) ui.label("dGPU is asleep - run something on it to read the clock range.", Tone::Muted);
            else {
                clk_lo = nvclk->gr_min ? nvclk->gr_min : 210;
                clk_hi = nvclk->gr_max.value_or(2100);
                if (!clk_init) { Json sv = a.read_state("nvclk.json"); clk = sv["gr_max"].is_number() ? static_cast<int>(sv["gr_max"].as_number()) : clk_hi; clk_init = true; }
                auto apply = [&](int hi_) {
                    Json j = Json::obj();
                    j.set("gr_min", clk_lo); j.set("gr_max", hi_);
                    a.write_state("nvclk.json", j);
                    int lo_ = clk_lo;
                    a.run_bg([&a, lo_, hi_] { auto r = sensors::set_nvidia_clock_lock(lo_, hi_); a.log("[Power] GPU clock lock -> " + std::to_string(lo_) + "-" + std::to_string(hi_) + " MHz" + (r.first ? "" : "  FAILED: " + r.second)); });
                };
                ui.label_c("Max graphics clock", Tone::Normal, Font::Regular, ui.S(180));
                ui.same_line(ui.S(10));
                ui.slider("clk", &clk, clk_lo, clk_hi, ui.S(300), " MHz");
                if (ui.released()) apply(clk);
                ui.same_line(ui.S(10));
                ui.label_c(nvclk->gr_cur ? "now: " + std::to_string(*nvclk->gr_cur) + " MHz" : "now: asleep", Tone::Muted);
                for (auto [lbl, frac] : {std::pair<const char*, double>{"Battery (-45%)", 0.55}, {"Cool (-25%)", 0.75}, {"Full", 1.0}})
                    if (ui.button_flow(lbl, Btn::OutlineInfo)) { clk = static_cast<int>(std::lround(clk_lo + (clk_hi - clk_lo) * frac)); apply(clk); }
                if (ui.button_flow("Unlock / reset", Btn::Outline)) {
                    a.run_bg([&a] { std::remove((a.cfg_dir() + "/nvclk.json").c_str()); auto r = sensors::reset_nvidia_clocks(); a.log(std::string("[Power] GPU clock lock -> reset (unlocked)") + (r.first ? "" : "  FAILED: " + r.second)); });
                    clk = clk_hi;
                }
                ui.flow_reset();
                ui.wrapped("After applying, watch the Report a Bug log / dmesg for Xid errors; if the GPU misbehaves, hit \"Unlock / reset\".", Tone::Warning);
            }
            ui.separator();
            ui.label("Clock offset (overclock)", Tone::Normal, 0, Font::Bold);
            ui.same_line();
            ui.pill("RISKY", Tone::Danger);
            if (!off_done) {
                ui.wrapped("A GPU clock offset needs the NVIDIA 'Coolbits' option and a reachable X display - a muxless laptop dGPU on Wayland normally can't. Check whether this box can:", Tone::Muted);
                if (ui.button(off_checking ? "Checking..." : "Check availability", Btn::OutlineInfo, 0, !off_checking)) {
                    off_checking = true;
                    auto o = off;
                    a.run_bg([o] { auto r = sensors::nvidia_clock_offset_info(); std::lock_guard<std::mutex> g(o->first); o->second = r; });
                }
                { std::lock_guard<std::mutex> g(off->first); if (off->second) { off_done = true; off_checking = false; off_core = off->second->core.value_or(0); off_mem = off->second->mem.value_or(0); } }
                if (off_checking) ui.wake_after(300);
            } else {
                sensors::NvOffsetInfo oi;
                { std::lock_guard<std::mutex> g(off->first); oi = *off->second; }
                if (!oi.available) {
                    ui.wrapped("Not available: " + (oi.reason.empty() ? std::string("unsupported") : oi.reason) + ".", Tone::Muted);
                    if (ui.button("Re-check", Btn::OutlineInfo)) { off_done = false; std::lock_guard<std::mutex> g(off->first); off->second.reset(); }
                } else {
                    ui.label_c("Core MHz", Tone::Normal, Font::Regular, ui.S(180));
                    ui.same_line(ui.S(10));
                    ui.slider("oc", &off_core, oi.core_range.first, oi.core_range.second, ui.S(300), "");
                    ui.label_c("Mem MHz", Tone::Normal, Font::Regular, ui.S(180));
                    ui.same_line(ui.S(10));
                    ui.slider("om", &off_mem, -500, 2000, ui.S(300), "");
                    if (ui.button("Apply offset", Btn::OutlineDanger)) {
                        int c = off_core, m = off_mem;
                        if (c == 0 && m == 0) a.run_bg([&a] { auto r = sensors::set_nvidia_clock_offset(0, 0); a.log("[Power] GPU clock offset -> " + r.second); });
                        else {
                            a.run_bg([&a, c, m] { auto r = sensors::set_nvidia_clock_offset(c, m); a.log("[Power] GPU clock offset -> " + r.second + (r.first ? "" : "  FAILED")); });
                            off_deadline = now_seconds() + 20;
                            off_applied = true;
                            if (!close_hook_set) { close_hook_set = true; a.add_close_hook([this] { if (off_applied) sensors::set_nvidia_clock_offset(0, 0); }); }
                        }
                    }
                    ui.same_line();
                    if (ui.button("Zero / revert", Btn::Outline)) { off_core = off_mem = 0; a.run_bg([&a] { auto r = sensors::set_nvidia_clock_offset(0, 0); a.log("[Power] GPU clock offset -> " + r.second); }); off_deadline = 0; }
                }
            }
            ui.card_end();
            if (off_deadline > 0) {
                double left = off_deadline - now_seconds();
                if (left <= 0) { off_deadline = 0; off_core = off_mem = 0; a.run_bg([&a] { auto r = sensors::set_nvidia_clock_offset(0, 0); a.log("[Power] GPU clock offset auto-reverted -> " + r.second); }); }
                else {
                    ui.begin_modal("Confirm GPU overclock", ui.S(460), ui.S(240));
                    ui.wrapped("Applied core " + std::to_string(off_core) + " / mem " + std::to_string(off_mem) + " MHz.\n\nIf the screen is stable, click Keep. Auto-reverts to 0 in " + std::to_string(static_cast<int>(left) + 1) + " s.", Tone::Normal);
                    if (ui.button("Keep", Btn::Success)) { off_deadline = 0; a.log("[Power] GPU clock offset kept by user"); }
                    ui.same_line();
                    if (ui.button("Revert now", Btn::Danger)) { off_deadline = 0; off_core = off_mem = 0; a.run_bg([&a] { auto r = sensors::set_nvidia_clock_offset(0, 0); a.log("[Power] GPU clock offset -> " + r.second); }); }
                    ui.end_modal();
                    ui.wake_after(250);
                }
            }

            // --- hybrid graphics mode ---
            ui.card_begin("Hybrid graphics mode");
            if (!sensors::envycontrol_available()) ui.wrapped("EnvyControl isn't installed. Install the \"EnvyControl\" app (Presets / Software tab), then reopen this tab.", Tone::Warning);
            else {
                static const std::vector<std::string> modes{"integrated", "hybrid", "nvidia"};
                static bool mi = false;
                static std::string cur;
                if (!mi) { cur = sensors::gpu_mode_get().value_or(""); mode = static_cast<int>(std::find(modes.begin(), modes.end(), cur.empty() ? "hybrid" : cur) - modes.begin()); mi = true; }
                ui.wrapped("hybrid = both GPUs, dGPU on demand (default, best battery/perf balance). integrated = dGPU fully off (max battery, no NVIDIA rendering). nvidia = dGPU always on (max performance, worst battery). A switch takes effect after logging out or rebooting.", Tone::Muted);
                ui.segmented("gpumode", &mode, {"Integrated", "Hybrid", "Nvidia"});
                ui.same_line(ui.S(12));
                if (ui.button("Apply", Btn::OutlineWarn)) ui.open_popup("confirm:gpumode");
                ui.label("current: " + (cur.empty() ? std::string("unknown") : cur), Tone::Muted);
                if (a.confirm("gpumode", "Switch graphics mode", "Switch to '" + modes[static_cast<size_t>(mode)] + "' graphics mode?\n\nThis rewrites the Xorg / display-manager config and only takes effect after you log out or reboot.", "Switch")) {
                    std::string m = modes[static_cast<size_t>(mode)];
                    a.run_bg([&a, m] { auto r = sensors::gpu_mode_set(m); a.log("[Power] graphics mode -> " + m + (r.first ? "  (log out / reboot to apply)" : "  FAILED: " + r.second)); });
                    cur = m + "  - log out or reboot to apply";
                }
            }
            ui.card_end();
        }

        battery_section(a);

        // --- auto switch ---
        Json pd = a.read_state("powerd.json");
        auto pm = sensors::panel_modes();
        static const std::vector<std::string> bundles{"Quiet", "Balanced", "Performance"};
        if (!aw_init) {
            const Json& aw = pd["autoswitch"];
            aw_on = aw["enabled"].as_bool();
            auto idx = [&](const std::string& s, int def) { auto it = std::find(bundles.begin(), bundles.end(), s); return it == bundles.end() ? def : static_cast<int>(it - bundles.begin()); };
            aw_ac = idx(aw["on_ac"].str("Balanced"), 1);
            aw_bat = idx(aw["on_battery"].str("Quiet"), 0);
            if (pm) {
                rates = pm->rates;
                auto hz = [&](const char* k) { int v = static_cast<int>(aw[k].as_number()); auto it = std::find(rates.begin(), rates.end(), v); return it == rates.end() ? 0 : static_cast<int>(it - rates.begin()) + 1; };
                aw_hz_ac = hz("refresh_ac");
                aw_hz_bat = hz("refresh_battery");
            }
            aw_init = true;
        }
        ui.card_begin("AC / battery auto profile switch");
        ui.wrapped("When the charger is plugged or pulled, the fan-curve daemon applies a bundle: Quiet = balanced profile + 25/35/25 W TDP, Balanced = 42/54/42 W, Performance = performance profile + 65/80/54 W. Needs the \"Fan-curve + AC-switch daemon\" tweak enabled.", Tone::Muted);
        ui.toggle("Auto-switch enabled", &aw_on);
        ui.label_c("On AC ->");
        ui.same_line();
        ui.combo("awac", &aw_ac, bundles, ui.S(160));
        ui.same_line(ui.S(16));
        ui.label_c("On battery ->");
        ui.same_line();
        ui.combo("awbat", &aw_bat, bundles, ui.S(160));
        if (!rates.empty()) {
            std::vector<std::string> opts{"leave alone"};
            for (int h : rates) opts.push_back(std::to_string(h) + " Hz");
            ui.label_c("Refresh AC ->");
            ui.same_line();
            ui.combo("hzac", &aw_hz_ac, opts, ui.S(160));
            ui.same_line(ui.S(16));
            ui.label_c("Refresh batt ->");
            ui.same_line();
            ui.combo("hzbat", &aw_hz_bat, opts, ui.S(160));
        }
        if (ui.button("Save auto-switch", Btn::Success)) {
            Json aw = Json::obj();
            aw.set("enabled", aw_on);
            aw.set("on_ac", bundles[static_cast<size_t>(aw_ac)]);
            aw.set("on_battery", bundles[static_cast<size_t>(aw_bat)]);
            if (!rates.empty()) {
                aw.set("refresh_ac", aw_hz_ac > 0 ? rates[static_cast<size_t>(aw_hz_ac - 1)] : 0);
                aw.set("refresh_battery", aw_hz_bat > 0 ? rates[static_cast<size_t>(aw_hz_bat - 1)] : 0);
            }
            pd.set("autoswitch", aw);
            a.write_state("powerd.json", pd);
            a.log(std::string("[Power] auto-switch saved (") + (aw_on ? "on" : "off") + ": AC->" + bundles[static_cast<size_t>(aw_ac)] + ", battery->" + bundles[static_cast<size_t>(aw_bat)] + ")");
            a.toast("Auto-switch saved", Tone::Success);
        }
        ui.card_end();
    }
};

// ---------------------------------------------------------------- Battery
struct Battery : Page {
    Battery() { title = "Battery"; group = "Hardware"; }
    int mode = 0;
    bool mode_init = false;
    void enter(App&) override { live().last = 0; }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Battery");
        poll(a, false, true);
        sensors::BatteryHealth h;
        { std::lock_guard<std::mutex> g(live().m); h = live().health; }
        if (!h.present) {
            if (live().last == 0 || live().inflight) { ui.label("reading battery...", Tone::Muted); return; }
            ui.wrapped("No battery detected (/sys/class/power_supply/BAT* is empty) - this page is for laptops.", Tone::Muted);
            return;
        }
        ui.wrapped("Battery wear and charge cycles, read straight from the kernel power-supply sysfs. Wear is how much of the pack's original design capacity is gone; keeping the charge limit below 100 % (section further down) slows it.", Tone::Muted);
        ui.card_begin("Health");
        if (!h.wear_pct) { ui.label_c("Wear"); ui.same_line(ui.S(70)); ui.label_c("n/a (battery doesn't report design capacity)", Tone::Muted); }
        else {
            Tone t = *h.wear_pct < 15 ? Tone::Success : *h.wear_pct < 30 ? Tone::Warning : Tone::Danger;
            ui.label_c("Wear");
            ui.same_line(ui.S(70));
            ui.label(format_double(*h.wear_pct, 1) + "%", t, ui.S(20), Font::Bold);
            ui.same_line(ui.S(16));
            ui.label_c("design " + format_double(h.design.value_or(0), 0) + " " + h.unit + "  ->  now holds " + format_double(h.full.value_or(0), 0) + " " + h.unit, Tone::Muted);
        }
        auto kv = [&](const std::string& k, const std::string& v) { if (v.empty()) return; ui.label(k, Tone::Normal); ui.same_line(ui.S(140)); ui.label(v, Tone::Muted); };
        kv("Charge cycles", h.cycle_count ? std::to_string(*h.cycle_count) : "");
        kv("Chemistry", h.technology);
        kv("Manufacturer", h.manufacturer);
        kv("Model", h.model);
        ui.card_end();

        ui.card_begin("Now");
        std::string st = lower(h.status);
        kv("Charge", h.capacity_pct ? std::to_string(*h.capacity_pct) + "%" : "-");
        kv("State", h.status.empty() ? "-" : h.status);
        std::string arrow = st == "charging" ? " -> in" : st == "discharging" ? " <- out" : "";
        kv("Power flow", h.power_w ? format_double(*h.power_w, 1) + " W" + arrow : "-");
        if (h.eta_min && *h.eta_min > 0 && !h.eta_kind.empty()) {
            int hh = *h.eta_min / 60, mm = *h.eta_min % 60;
            char b[32];
            if (hh) std::snprintf(b, sizeof b, "%d h %02d m", hh, mm); else std::snprintf(b, sizeof b, "%d m", mm);
            kv("Time remaining", std::string("~") + b + " " + h.eta_kind + " at this rate");
        } else kv("Time remaining", st == "charging" || st == "discharging" ? "-" : "full / plugged in");
        kv("Voltage", h.voltage_v ? format_double(*h.voltage_v, 2) + " V" : "-");
        ui.card_end();

        battery_section(a);

        if (!which("smbios-battery-ctl").empty()) {
            ui.card_begin("Charging speed");
            auto cur = sensors::battery_charge_mode();
            if (!mode_init) { mode = cur && *cur == "express" ? 1 : 0; mode_init = true; }
            std::string note = "Express charges the pack faster (more heat, a little more wear); Standard is the gentler default. Firmware setting - persists with no service.";
            if (!cur) note += "  (current mode unreadable on this firmware - setting still works)";
            ui.wrapped(note, Tone::Muted);
            if (ui.segmented("chg", &mode, {"Standard", "Express"})) {
                std::string m = mode == 1 ? "express" : "standard";
                auto r = sensors::set_battery_charge_mode(m);
                a.log("[Battery] charging mode -> " + m + (r.first ? "" : "  FAILED: " + r.second));
            }
            ui.card_end();
        }
    }
};

// ---------------------------------------------------------------- Display
struct Display : Page {
    Display() { title = "Display"; group = "Hardware"; }
    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Display");
        auto pm = sensors::panel_modes();
        ui.card_begin("Panel refresh rate");
        if (!pm || pm->rates.size() < 2) ui.wrapped("Needs kscreen-doctor (KDE) and a panel with more than one refresh rate. Nothing to switch here.", Tone::Muted);
        else {
            ui.wrapped("Dropping the high-refresh panel to 60 Hz on battery is a real power saving. Resolution is kept; KScreen remembers the choice across reboots.", Tone::Muted);
            int cur = pm->current_hz ? static_cast<int>(std::lround(*pm->current_hz)) : pm->rates.back();
            int sel = static_cast<int>(std::find(pm->rates.begin(), pm->rates.end(), cur) - pm->rates.begin());
            std::vector<std::string> labels;
            for (int h : pm->rates) labels.push_back(std::to_string(h) + " Hz");
            if (sel >= static_cast<int>(labels.size())) sel = 0;
            if (ui.segmented("hz", &sel, labels)) {
                int hz = pm->rates[static_cast<size_t>(sel)];
                a.run_bg([&a, hz] { auto r = sensors::set_panel_refresh(hz); a.log("[Power] panel refresh -> " + std::to_string(hz) + " Hz" + (r.first ? "  (" + r.second + ")" : "  FAILED: " + r.second)); });
            }
            ui.label(pm->current_hz ? "current: " + std::to_string(static_cast<int>(std::lround(*pm->current_hz))) + " Hz" : "current: unknown", Tone::Muted);
            ui.wrapped("Tip: the AC/battery auto-switch on the Power & Limits tab can flip this with the charger - \"AC -> 120 Hz, battery -> 60 Hz\".", Tone::Muted);
        }
        ui.card_end();
        auto vrr = sensors::vrr_status();
        ui.card_begin("Adaptive Sync (VRR)");
        ui.wrapped(vrr.capable.empty() ? "no VRR-capable panel detected on this system" : join(vrr.capable, ", ") + " report VRR-capable - enable it per-display in System Settings > Display, and apply the KDE \"allow tearing\" tweak (Gaming category) for lowest latency.", Tone::Muted);
        ui.card_end();
    }
};

// ---------------------------------------------------------------- Touchpad
struct Touchpad : Page {
    Touchpad() { title = "Touchpad"; group = "Hardware"; }
    sensors::TouchpadInfo info;
    bool loaded = false;
    void enter(App&) override { loaded = false; }

    void flip(App& a, const std::string& label, bool* v, sensors::Result (*setter)(bool)) {
        bool val = *v;
        a.run_bg([&a, label, val, setter, v] {
            auto r = setter(val);
            a.log("[Touchpad] " + label + " -> " + (val ? "true" : "false") + (r.first ? "" : "  FAILED: " + r.second));
            if (!r.first) *v = !val;
        });
    }

    bool en = true, tap = true, nat = false, dwt = true;
    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) { info = sensors::touchpad_info(); en = info.enabled.value_or(true); tap = info.tap_to_click.value_or(true); nat = info.natural_scroll.value_or(false); dwt = info.disable_while_typing.value_or(true); loaded = true; }
        a.page_title("Touchpad");
        if (!info.available) { ui.wrapped("No touchpad reachable via KWin's D-Bus interface - needs KDE Plasma on Wayland with a touchpad (xinput-based X11 toggles don't apply here).", Tone::Muted); return; }
        ui.label("Device: " + (info.name.empty() ? "(unnamed)" : info.name), Tone::Muted);
        ui.card_begin("Enable / disable");
        ui.wrapped("Takes effect immediately. This is a live session setting, not a boot-persisted tweak - a reboot or logout always brings the touchpad back, so turning it off can't lock you out permanently.", Tone::Muted);
        if (ui.toggle("Touchpad enabled", &en)) flip(a, "enabled", &en, sensors::set_touchpad_enabled);
        ui.card_end();
        ui.card_begin("Behaviour");
        if (ui.toggle("Tap to click", &tap)) flip(a, "tap_to_click", &tap, sensors::set_touchpad_tap_to_click);
        if (ui.toggle("Natural scrolling", &nat)) flip(a, "natural_scroll", &nat, sensors::set_touchpad_natural_scroll);
        if (ui.toggle("Disable while typing", &dwt)) flip(a, "disable_while_typing", &dwt, sensors::set_touchpad_disable_while_typing);
        ui.card_end();
    }
};

}  // namespace

std::unique_ptr<Page> make_power() { return std::make_unique<Power>(); }
std::unique_ptr<Page> make_battery() { return std::make_unique<Battery>(); }
std::unique_ptr<Page> make_display() { return std::make_unique<Display>(); }
std::unique_ptr<Page> make_touchpad() { return std::make_unique<Touchpad>(); }

}  // namespace tt::app
