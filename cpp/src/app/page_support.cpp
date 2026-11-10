// About and Report a Bug pages.
#include <ctime>
#include <mutex>

#include "../diag.hpp"
#include "../distro.hpp"
#include "../engine.hpp"
#include "../sensors.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "../ui/icon.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
const char* kProjectUrl = "https://github.com/BeanGreen247/tuxthrottle";
const char* kIssuesUrl = "https://github.com/BeanGreen247/tuxthrottle/issues/new";

// ------------------------------------------------------------------ About
struct About : Page {
    About() { title = "About"; group = "Support"; }
    bool features_open = false, tray_auto = false, loaded = false;
    std::string tray_status;

    static std::string tray_exec() {
        std::string w = which("tuxthrottle-tray");
        if (!w.empty()) return w;
        std::string sib = dir_name(self_exe()) + "/tuxthrottle-tray";
        return path_exists(sib) ? sib : "tuxthrottle-tray";
    }
    std::string autostart_path(App& a) { return home_of(a.user) + "/.config/autostart/tuxthrottle-tray.desktop"; }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) { tray_auto = path_exists(autostart_path(a)); loaded = true; }
        a.page_title("About");

        ui.card_begin("Appearance");
        ui.wrapped("Colour theme for the whole GUI. 'Desktop Accent' follows your KDE accent colour; the others pin their own palette.", Tone::Muted);
        std::vector<std::string> names;
        int sel = 0, i = 0;
        for (const auto& t : themes()) { names.push_back(t.name); if (t.name == ui.th.name) sel = i; ++i; }
        ui.label_c("Theme");
        ui.same_line();
        if (ui.combo("theme", &sel, names, ui.S(220))) a.set_theme_by_name(names[static_cast<size_t>(sel)]);
        ui.card_end();

        {
            static std::vector<uint8_t> big;
            static int bw = 0;
            if (big.empty()) { int h; app_icon_png(128, &bw, &h, &big); }
            Rect ir = ui.item_rect(ui.S(96), ui.S(96));
            ui.draw([ir](Gfx& g) { g.image("about", big.data(), bw, bw, ir); });
            ui.same_line(ui.S(16));
        }
        ui.heading("TuxThrottle", ui.S(24));
        ui.label("version " + engine::toolkit_version(), Tone::Muted, ui.S(13), Font::Mono);
        ui.wrapped("A checkbox-driven GUI and tray monitor for gaming on Linux: tweaks, drivers and app installs, Steam and Proton management, per-game MangoHud and GPU switches, drive health and updates. Those parts work on Nobara, Fedora, Debian, Ubuntu, Arch and Gentoo machines. The hardware tabs (keyboard RGB, fans, power limits, the G-key) are written for the Dell G15 5515 it was developed on; on other hardware the tool says so at the top of the window.", Tone::Normal);

        if (ui.button(features_open ? "v   What's inside  -  click to collapse" : ">   What's inside  -  every section, expanded", Btn::Secondary, ui.avail_w())) features_open = !features_open;
        if (features_open) {
            ui.card_begin("");
            static const std::pair<const char*, const char*> feats[] = {
                {"Dashboard", "8 live ring gauges - CPU temp/clock/power, iGPU clock, dGPU temp/clock/util/power - with rolling history charts, a session CSV log and a Game Mode toggle"},
                {"Keyboard", "Alienware AW-ELC RGB via OpenRGB - whole-keyboard colour, presets, brightness, firmware Spectrum Cycle; sync with the KDE accent colour in either direction"},
                {"Fans", "thermal profile, additive fan boost + presets, guarded manual PWM, and a 10-point closed-loop custom fan curve run by the daemon"},
                {"Power & Limits", "CPU TDP sliders (ryzenadj) + presets; Curve Optimizer undervolt with a stress-test and auto-revert; NVIDIA power limit / clock lock / offset; hybrid-graphics mode; battery charge limit; AC/battery auto-switch"},
                {"Battery", "wear %, charge cycles, chemistry; live charge / power flow / time remaining; the charge-limit control; express/standard charging"},
                {"VRAM", "live per-GPU video-memory bars + top consumers; Regular/Medium/Extreme KWin budget; Free-VRAM action; desktop-GPU selector; dGPU runtime power management"},
                {"Profiles", "named full-state bundles with automatic snapshots + rollback, per-game auto-profiles and a time-of-day schedule run by the daemon"},
                {"Presets", "one-click curated bundles of tweaks + app installs, plus a global \"apply all recommended\" button"},
                {"Updates", "nobara-sync or the distro package manager, Flatpak and fwupd sections; pending count tagged with the metadata age"},
                {"Setup Games", "per-game click-through walkthroughs - each step has a status and either a streamed Run button or a manual Copy-command step"},
                {"Game Tools", "Proton-prefix relocation, save-game vault, shared shader-cache folder, Steam low-resource mode, launch-options builder, frame-rate limits, per-game dGPU, benchmark A/B and the one-click GPU driver installer"},
                {"MangoHud", "per-game overlay switches with a live gate, per-game profiles and a full overlay editor with a drag-to-place picker"},
                {"Proton & Runtimes", "Valve's Proton / Linux Runtimes and community Proton, Wine, DXVK and VKD3D builds for Steam, Heroic, Lutris and Bottles"},
                {"Tweaks & Apps", "reversible system tweaks by category with check / undo, plus native / Flatpak app installs"},
                {"System tray", "a tray icon - left-click opens this window, middle-click toggles Game Mode, right-click shows live readouts and quick actions"},
                {"tuxthrottled", "the daemon: fan curve, AC/battery switch, per-game profiles, schedule, thermal alerts and a control socket"},
                {"tuxthrottlectl", "headless CLI for scripts, keybinds and ssh (status / get / set / profile / snapshot / rollback / tweaks / drives ...)"},
                {"Report a Bug", "read-only hardware / OS dump + a hardware-bundle tarball for GitHub issues and new-board onboarding"},
            };
            for (auto& [n, d] : feats) {
                ui.label(std::string("> ") + n, Tone::Normal, ui.S(14), Font::Bold);
                ui.indent(ui.S(18));
                ui.wrapped(d, Tone::Muted, ui.avail_w());
                ui.unindent(ui.S(18));
            }
            ui.wrapped("FEATURES.md in the repo has the full, detailed list with examples for every control.", Tone::Muted);
            ui.card_end();
        }

        ui.card_begin("Project");
        if (ui.button("Open on GitHub", Btn::Info)) a.open_url(kProjectUrl);
        ui.same_line();
        if (ui.button("Report an issue", Btn::OutlineWarn)) a.open_url(kIssuesUrl);
        ui.same_line();
        if (ui.button("Copy link", Btn::Outline)) a.copy_text(kProjectUrl, "link");
        ui.label(kProjectUrl, Tone::Accent, ui.S(13), Font::Mono);
        ui.card_end();

        ui.card_begin("System tray");
        ui.wrapped("A small tray icon (left-click opens this window, middle-click toggles Game Mode, right-click for CPU/GPU readouts + quick actions).", Tone::Muted);
        bool t = tray_auto;
        if (ui.toggle("Start the tray icon automatically at login", &t)) {
            tray_auto = t;
            std::string p = autostart_path(a);
            if (t) {
                mkdir_p(dir_name(p));
                write_file(p, "[Desktop Entry]\nType=Application\nName=TuxThrottle Tray\nComment=Tray icon + quick launcher for TuxThrottle\nExec=" + tray_exec() + "\nIcon=tuxthrottle\nTerminal=false\nX-GNOME-Autostart-enabled=true\n");
                chown_to_user(p, a.user);
                chown_to_user(dir_name(p), a.user);
                tray_status = "will start at next login";
            } else { std::remove(p.c_str()); tray_status = "autostart removed"; }
        }
        if (ui.button("Launch tray now", Btn::OutlineInfo)) {
            if (run_argv({"pgrep", "-f", "tuxthrottle-tray"}, 3000).code == 0) tray_status = "already running";
            else {
                auto argv = sensors::session_cmd({"bash", "-lc", "setsid " + shell_quote(tray_exec()) + " >/dev/null 2>&1 &"});
                tray_status = spawn_quiet(argv, {}, true) > 0 ? "launched - check your tray" : "could not start it";
            }
        }
        ui.same_line();
        ui.label_c(tray_status, Tone::Muted);
        ui.card_end();

        ui.card_begin("Backends in use");
        ui.wrapped("What answers each job on this machine. A line marked \"not available\" means that part of the tool is off until the tool or hardware behind it is present.", Tone::Muted);
        for (const auto& b : sensors::backends()) {
            ui.label_c(b.area, Tone::Muted, Font::Regular, ui.S(170));
            ui.same_line();
            ui.label_c(b.name, b.ok ? Tone::Normal : Tone::Warning);
        }
        ui.card_end();

        ui.card_begin("Details");
        auto m = sensors::detect_model();
        std::pair<const char*, std::string> rows[] = {
            {"Reference hardware", "Dell G15 5515 Ryzen Edition (0R3CDX)"},
            {"This machine", m.vendor + " " + m.product + (m.bios.empty() ? "" : ", BIOS " + m.bios)},
            {"Distro", current_distro().name + " (" + current_distro().family + (current_distro().atomic ? ", atomic" : "") + ")"},
            {"Status", "developed and tested live on the reference hardware"},
            {"License", "MIT - (c) 2026 BeanGreen247"},
            {"Install path", sensors::app_dir()},
        };
        for (auto& [k, v] : rows) {
            ui.label(std::string(k) + ":", Tone::Muted);
            ui.same_line(ui.S(10));
            ui.label(v, Tone::Normal);
        }
        ui.card_end();
        ui.wrapped("Built in the spirit of WinUtil-style Windows tweak tools and Div-Acer-Manager-Max. Not affiliated with Dell or Alienware.", Tone::Muted);
    }
};

// ------------------------------------------------------------------ Report a Bug
struct Report : Page {
    Report() { title = "Report a Bug"; group = "Support"; }
    std::string raw, shown = "Click \"Generate report\".\n\nTerminal equivalent:\n  sudo tuxthrottlectl diag --debug\n", bundle_msg;
    bool running = false, bundling = false;
    std::shared_ptr<std::pair<std::mutex, std::string>> res = std::make_shared<std::pair<std::mutex, std::string>>();
    std::shared_ptr<std::pair<std::mutex, std::string>> bres = std::make_shared<std::pair<std::mutex, std::string>>();
    std::vector<std::string> lines;

    void set_text(const std::string& t) {
        shown = t;
        lines = split_lines(t);
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (lines.empty()) set_text(shown);
        // banner
        ui.card_begin("", Tone::Amber);
        ui.wrapped("Bug reports & logs.  This page only READS your system - it gathers hardware + OS + toolkit state so you can attach it to a GitHub issue. Nothing is uploaded automatically: you Copy or Save the report and paste it into the issue yourself. Review it for username / hostname first.", Tone::Amber);
        ui.card_end();
        a.page_title("Report a Bug");
        ui.wrapped("Collected: kernel & DMI, CPU/GPU, thermal/fan, the keyboard / hotkey / media-key evdev map (/proc/bus/input/devices + capability bitmaps), OpenRGB, package versions, filtered dmesg / journal. All read-only, hard-timed-out. Run the toolkit with sudo for dmesg / RAPL. Review it for your username / hostname before sharing.", Tone::Muted);

        {
            std::lock_guard<std::mutex> g(res->first);
            if (running && !res->second.empty()) {
                raw = res->second == "\x01FAILED" ? "" : res->second;
                set_text(diag::wrap_issue_block(raw));
                res->second.clear();
                running = false;
            }
        }
        {
            std::lock_guard<std::mutex> g(bres->first);
            if (bundling && !bres->second.empty()) { bundle_msg = bres->second.rfind("ERROR", 0) == 0 ? bres->second : "Saved: " + bres->second; bundling = false; bres->second.clear(); a.log("[Report] hardware bundle -> " + bundle_msg); }
        }
        if (running || bundling) ui.wake_after(300);

        ui.flow_reset();
        if (ui.button_flow(running ? "Collecting..." : "Generate report", Btn::Success, !running)) {
            running = true;
            set_text("Collecting hardware / OS / toolkit info - ~15-30 s...\n");
            auto r = res;
            a.run_bg([r] {
                std::string rep;
                try { rep = diag::collect_debug_report(false); } catch (const std::exception& e) { rep = std::string("debug report failed: ") + e.what(); }
                std::lock_guard<std::mutex> g(r->first);
                r->second = rep.empty() ? "debug report failed" : rep;
            });
        }
        if (ui.button_flow("Copy for GitHub issue", Btn::Info)) a.copy_text(shown, "report");
        if (ui.button_flow("Copy full issue (template + report)", Btn::OutlineInfo)) {
            if (raw.empty()) a.status_text = "Generate the report first.";
            else {
                std::string body = raw;
                body = replace_all(body, "```", "``\xE2\x80\x8B`");
                a.copy_text(replace_all(diag::issue_template(), "PASTE THE DEBUG REPORT HERE", trim(body)), "full issue");
            }
        }
        if (ui.button_flow("Save .txt...", Btn::Outline)) {
            if (trim(raw).empty()) a.status_text = "Generate the report first.";
            else {
                char b[40];
                std::time_t t = std::time(nullptr);
                std::tm tm;
                localtime_r(&t, &tm);
                std::strftime(b, sizeof b, "%Y%m%d-%H%M%S", &tm);
                a.open_file_dialog("diag-save", 1, home_of(a.user), std::string("tuxthrottle-debug-") + b + ".txt", ".txt");
            }
        }
        std::string p;
        if (a.file_result("diag-save", &p)) {
            if (write_file(p, trim(raw) + "\n")) { chown_to_user(p, a.user); a.status_text = "Saved " + p; a.toast("Saved " + p, Tone::Success); }
            else a.toast("Save failed: " + p, Tone::Danger);
        }
        ui.flow_reset();
        if (ui.button("Collect hardware bundle (.tar.gz)", Btn::OutlineWarn, 0, !bundling)) {
            bundling = true;
            bundle_msg = "Collecting hardware dump bundle...";
            auto r = bres;
            std::string user = a.user;
            a.run_bg([r, user] {
                std::string out;
                try { out = diag::collect_hw_bundle(home_of(user), true); chown_to_user(out, user); } catch (const std::exception& e) { out = std::string("ERROR: ") + e.what(); }
                std::lock_guard<std::mutex> g(r->first);
                r->second = out;
            });
        }
        ui.wrapped(bundle_msg.empty() ? "raw sysfs / DMI / evdev-keycaps / hwmon / PCI / OpenRGB dumps; attach the file to a \"new hardware support\" issue" : bundle_msg, Tone::Muted);

        ui.card_begin("GITHUB ISSUE BLOCK", Tone::Warning);
        ui.wrapped("\"Copy for GitHub issue\" copies exactly what's in here (a collapsible <details> block); paste it straight into the issue.", Tone::Muted);
        ui.log_view("diagtext", lines, std::max(ui.S(260), ui.avail_h() - ui.S(40)));
        ui.card_end();
    }
};
}  // namespace

std::unique_ptr<Page> make_about() { return std::make_unique<About>(); }
std::unique_ptr<Page> make_report() { return std::make_unique<Report>(); }

}  // namespace tt::app
