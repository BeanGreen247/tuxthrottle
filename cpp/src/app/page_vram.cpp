#include <atomic>
#include <memory>
#include <mutex>

#include "../sensors.hpp"
#include "../util.hpp"
#include "../vram.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Snap {
    std::mutex m;
    std::vector<sensors::VramRow> info;
    std::vector<sensors::VramConsumer> cons;
    std::atomic<bool> inflight{false};
    double last = 0;
};

struct Vram : Page {
    Vram() { title = "VRAM"; group = "Hardware"; }
    std::shared_ptr<Snap> snap = std::make_shared<Snap>();
    int tier = 0, gpu = 0;
    bool loaded = false;
    std::vector<std::string> gpu_modes;
    bool rtd3 = false, rtd3_init = false;

    void enter(App&) override { loaded = false; snap->last = 0; }

    static std::string gpu_name(const std::string& pci) {
        for (const auto& d : sensors::gpu_devices()) {
            std::string a = d.pci, b = pci;
            for (auto* s : {&a, &b}) for (auto& c : *s) c = static_cast<char>(std::tolower(c));
            if (a == b) return d.name;
        }
        return "";
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("VRAM", "A laptop's integrated GPU shares a small slice of system RAM as video memory and the KDE/Wayland desktop routinely fills it (spilling to slower GTT); the discrete GPU is kept free for video editing, games and 3D. Lower tiers strip desktop eye-candy to shrink the compositor's footprint. Everything below is read live from your hardware.");
        if (!loaded) {
            auto t = vram::current_tier();
            tier = static_cast<int>(std::find(vram::kTiers.begin(), vram::kTiers.end(), t) - vram::kTiers.begin());
            if (tier >= static_cast<int>(vram::kTiers.size())) tier = 0;
            gpu_modes = vram::compositor_gpu_modes();
            std::string cg = vram::current_compositor_gpu();
            gpu = cg == "igpu" ? 1 : cg == "dgpu" ? 2 : 0;
            loaded = true;
        }
        // poll
        ui.wake_after(5000);
        double now = now_seconds();
        if (!snap->inflight && now - snap->last > 5.0) {
            snap->inflight = true;
            snap->last = now;
            auto s = snap;
            a.run_bg([s] {
                auto info = sensors::vram_info();
                auto cons = sensors::vram_consumers(8);
                std::lock_guard<std::mutex> g(s->m);
                s->info = info; s->cons = cons; s->inflight = false;
            });
        }
        std::vector<sensors::VramRow> info;
        std::vector<sensors::VramConsumer> cons;
        { std::lock_guard<std::mutex> g(snap->m); info = snap->info; cons = snap->cons; }

        ui.card_begin("Live VRAM usage");
        auto gpus = sensors::drm_gpus();
        if (gpus.empty()) ui.label("no render GPU found under /sys/class/drm", Tone::Muted);
        for (const auto& g : gpus) {
            std::string nm = gpu_name(g.pci);
            if (nm.empty()) nm = g.driver.empty() ? g.pci : g.driver;
            ui.label_c(nm + "  (" + g.kind + ")", Tone::Normal, Font::Regular, ui.S(300));
            ui.same_line(ui.S(8));
            float frac = 0;
            std::string txt = "...";
            for (const auto& r : info) {
                std::string x = r.pci, y = g.pci;
                for (auto* s : {&x, &y}) for (auto& c : *s) c = static_cast<char>(std::tolower(c));
                if (x != y) continue;
                if (r.asleep) { txt = "asleep"; break; }
                if (!r.used_mb || !r.total_mb || *r.total_mb == 0) { txt = "n/a"; break; }
                frac = static_cast<float>(*r.used_mb) / static_cast<float>(*r.total_mb);
                txt = std::to_string(*r.used_mb) + " / " + std::to_string(*r.total_mb) + " MiB  (" + std::to_string(r.pct.value_or(0)) + "%)" + (r.gtt_used_mb && *r.gtt_used_mb ? "  +" + std::to_string(*r.gtt_used_mb) + " GTT" : "");
            }
            ui.progress(frac, ui.S(240));
            ui.same_line(ui.S(10));
            ui.label_c(txt, Tone::Muted);
        }
        if (!cons.empty()) {
            std::string txt = "holding VRAM now:\n";
            for (const auto& c : cons) {
                char b[160];
                std::snprintf(b, sizeof b, "%7.0f MiB  %-22s [%s]\n", c.vram_mb, c.comm.substr(0, 22).c_str(), c.driver.c_str());
                txt += b;
            }
            for (const auto& l : split_lines(txt)) ui.label(l, Tone::Muted, ui.S(12), Font::Mono);
        }
        if (ui.button("Free VRAM now", Btn::OutlineInfo, 0, !a.busy())) a.run_shell_task("free VRAM (evict iGPU caches)", shell_quote(ctl_exe()) + " vram free");
        ui.tooltip("Evict the iGPU's cached buffers to system RAM (they page back in as needed). Good before/after a game or a Resolve session to clear accumulated slack.");
        ui.same_line();
        if (ui.button("Restart compositor", Btn::OutlineWarn, 0, !a.busy())) ui.open_popup("confirm:vram-kwin");
        ui.tooltip("Also restart KWin - releases allocations the evict can't. Windows stay open; the screen blacks for about a second.");
        if (a.confirm("vram-kwin", "Restart compositor", "Restart KWin to release its VRAM allocations.\n\nOpen windows stay put; the screen blacks for about a second. Continue?", "Restart"))
            a.run_shell_task("free VRAM + restart compositor", shell_quote(ctl_exe()) + " vram free --restart-compositor");
        ui.card_end();

        static const char* tdesc[3] = {
            "Full desktop - every effect, image wallpaper, all window previews kept in VRAM. The baseline your settings started at.",
            "Blur & background-contrast off, quicker animations, cheaper texture filtering, fewer hidden-window pixmaps kept. Barely visible; frees tens of MiB.",
            "Everything in Medium plus: solid-colour wallpaper (drops a full-screen texture per screen), no Overview / Present-Windows / Desktop-Grid, nearest-neighbour textures, hidden windows drop their pixmaps, on-screen (Maliit) keyboard off. Restarts the panel; the keyboard and some savings only fully apply next login."};
        static const char* tname[3] = {"Regular", "Medium", "Extreme"};
        ui.card_begin("VRAM budget tier");
        for (int i = 0; i < 3; ++i) {
            ui.push_id(i);
            if (ui.radio(tname[i], &tier, i)) a.run_shell_task(std::string("VRAM budget -> ") + vram::kTiers[static_cast<size_t>(i)], shell_quote(ctl_exe()) + " vram profile " + vram::kTiers[static_cast<size_t>(i)]);
            ui.indent(ui.S(26));
            ui.wrapped(tdesc[i], Tone::Muted, ui.avail_w());
            ui.unindent(ui.S(26));
            ui.pop_id();
        }
        ui.wrapped("\"Regular\" restores the exact KWin/Plasma values captured the first time you left it - not necessarily stock Plasma defaults.", Tone::Muted);
        ui.card_end();

        ui.card_begin("Which GPU renders the desktop");
        ui.wrapped("Takes effect after you log out and back in. If the desktop then fails to start and drops you at the login screen: switch to a text console (Ctrl+Alt+F3), log in, and run  rm ~/.config/plasma-workspace/env/09-tuxthrottle-gpu.sh", Tone::Warning);
        std::string ig_name, dg_name;
        for (const auto& g : gpus) { if (g.kind == "integrated" && ig_name.empty()) ig_name = gpu_name(g.pci); if (g.kind == "discrete" && dg_name.empty()) dg_name = gpu_name(g.pci); }
        if (ig_name.empty()) ig_name = "integrated GPU";
        if (dg_name.empty()) dg_name = "discrete GPU";
        struct Row { const char* val; std::string cap, desc; };
        Row rows[3] = {{"auto", "Automatic", "Let KWin choose - normally the " + ig_name + "."},
                       {"igpu", "Integrated - " + ig_name + " (pin)", "Pin the compositor to the integrated GPU so a driver / device re-enumeration can't move it. Usually the same render path as Automatic, just nailed down."},
                       {"dgpu", "Discrete - " + dg_name, "Pin the whole desktop to the discrete GPU. Not possible when the panel is wired to the integrated GPU (muxless) - most hybrid laptops; offered only where a hardware MUX exists."}};
        for (int i = 0; i < 3; ++i) {
            ui.push_id(100 + i);
            bool ok = std::find(gpu_modes.begin(), gpu_modes.end(), rows[i].val) != gpu_modes.end();
            if (!ok) { ui.label(rows[i].cap + "  (unavailable)", Tone::Muted); }
            else if (ui.radio(rows[i].cap, &gpu, i)) {
                a.run_shell_task(std::string("desktop GPU -> ") + rows[i].val, shell_quote(ctl_exe()) + " vram compositor-gpu " + rows[i].val);
                a.message("Log out to apply", "The desktop-GPU choice is written. Log out and back in for KWin to pick it up.");
            }
            ui.indent(ui.S(26));
            ui.wrapped(rows[i].desc, Tone::Muted, ui.avail_w());
            ui.unindent(ui.S(26));
            ui.pop_id();
        }
        ui.card_end();

        ui.card_begin("Discrete GPU idle power");
        auto pm = sensors::nvidia_runtime_pm();
        if (!pm) ui.label("No NVIDIA GPU detected.", Tone::Muted);
        else {
            if (!rtd3_init) { rtd3 = pm->control == "auto"; rtd3_init = true; }
            if (ui.toggle("Let the dGPU power down when idle (runtime PM)", &rtd3)) {
                auto r = sensors::set_nvidia_runtime_pm(rtd3);
                a.status_text = r.second;
                if (!r.first) a.message("Runtime PM", r.second);
            }
            ui.wrapped("Frees its VRAM and ~5 W when nothing uses it; it wakes on its own for a PRIME-offloaded app. Live only - add the \"NVIDIA runtime power management\" tweak on the GPU tab to make it stick across reboots.", Tone::Muted);
        }
        ui.card_end();
    }
};
}  // namespace

std::unique_ptr<Page> make_vram() { return std::make_unique<Vram>(); }

}  // namespace tt::app
