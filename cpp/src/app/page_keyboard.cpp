#include <atomic>

#include "../devices.hpp"
#include "../sensors.hpp"
#include "../util.hpp"
#include "pages.hpp"
#include "widgets.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Keyboard : Page {
    Keyboard() { title = "Keyboard"; group = "Hardware"; }
    std::string hex = "#ffffff", mode = "zones";
    int brightness = 100, speed = 50;
    bool match_accent = false, push_accent = false, loaded = false, detected = false;
    std::atomic<bool> busy{false};

    void enter(App&) override { loaded = false; }

    static std::string safe_hex(std::string s) {
        if (s.empty() || s[0] != '#') s = "#" + s;
        return s.size() == 7 ? s : "#ffffff";
    }
    std::map<int, std::string> zones_for(const std::string& h) {
        std::map<int, std::string> m;
        for (int i = 0; i < kbd::kZoneCount; ++i) m[i] = h.substr(1);
        return m;
    }
    std::string static_mode() const { return match_accent ? "accent" : "zones"; }
    bool is_effect(const std::string& m) const { return kbd::kEffectModes.count(m) > 0; }

    void run(App& a, const std::string& desc, std::function<void()> fn) {
        if (busy.exchange(true)) { a.log("[Keyboard] busy - try again in a moment"); return; }
        a.log("[Keyboard] " + desc + " ...");
        a.run_bg([this, &a, desc, fn] {
            try { fn(); a.log("[Keyboard] " + desc + " ok"); }
            catch (const std::exception& e) { a.log(std::string("[Keyboard FAILED] ") + e.what()); }
            busy = false;
        });
    }

    void set_mode(const std::string& m) {
        mode = m;
        if (m != "accent") match_accent = false;
        if (is_effect(m)) push_accent = false;
    }

    void push_accent_now(App& a) {
        std::string h = safe_hex(hex).substr(1);
        Color c = parse_hex("#" + h);
        std::string rgb = std::to_string(c.r) + "," + std::to_string(c.g) + "," + std::to_string(c.b);
        a.log("[Keyboard] desktop accent -> #" + h + " ...");
        a.run_bg([&a, h, rgb] {
            bool applied = false;
            if (!which("plasma-apply-colorscheme").empty()) applied = run_argv(sensors::session_cmd({"plasma-apply-colorscheme", "--accent-color", "#" + h}), 25000).code == 0;
            for (auto [k, v] : {std::pair<const char*, std::string>{"AccentColor", rgb}, {"AccentColorFromWallpaper", "false"}, {"LastUsedCustomAccentColor", rgb}})
                run_argv(sensors::session_cmd({"kwriteconfig6", "--file", "kdeglobals", "--group", "General", "--key", k, v}), 15000);
            if (!applied) {
                run_argv(sensors::session_cmd({"dbus-send", "--session", "--type=signal", "/KGlobalSettings", "org.kde.KGlobalSettings.notifyChange", "int32:0", "int32:0"}), 10000);
                run_argv(sensors::session_cmd({"qdbus-qt6", "org.kde.KWin", "/KWin", "reconfigure"}), 10000);
            }
            a.log("[Keyboard] desktop accent set to #" + h + (applied ? "" : " (relogin if it didn't repaint)"));
        });
    }

    void apply_all(App& a) {
        std::string h = safe_hex(hex);
        int b = brightness;
        bool pa = push_accent;
        set_mode("zones");
        auto z = zones_for(h);
        run(a, "colour " + h + " @ " + std::to_string(b) + "%", [h, b, z, pa] { kbd::set_all(h, b); kbd::save_state(z, b, "zones", 50, pa); });
        if (push_accent && !is_effect(mode)) push_accent_now(a);
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) {
            detected = kbd::info()["detected"].as_bool();
            auto saved = kbd::load_state();
            if (saved) {
                brightness = saved->brightness;
                if (!saved->zones.empty()) { auto z = saved->zones.begin()->second; char b[16]; std::snprintf(b, sizeof b, "#%02x%02x%02x", z.r, z.g, z.b); hex = b; }
            }
            auto m = kbd::load_meta();
            speed = m.speed;
            mode = m.mode;
            match_accent = mode == "accent";
            push_accent = m.push_accent;
            loaded = true;
        }
        a.page_title("Keyboard");
        if (which("openrgb").empty()) {
            ui.wrapped("Install the OpenRGB app (Software tab) to use this.\n\nThe AW-ELC keyboard has no kernel driver and ignores raw HID writes - OpenRGB's 16-zone protocol is the only thing that drives it. The backlight must also be enabled in BIOS setup (F2 -> Keyboard Backlight) or nothing lights.", Tone::Warning);
            return;
        }
        if (!detected) { ui.wrapped("No Alienware AW-ELC RGB keyboard (USB 187c:0550) found.\nThis tab drives the RGB backlight on the Dell G15 5515.", Tone::Muted); return; }

        ui.card_begin("How this works");
        ui.wrapped("Driven through OpenRGB (the AW-ELC has no kernel driver). Enable the backlight in BIOS setup (F2 -> Keyboard Backlight) first - if the keys stay dark, that's why. This keyboard is a single controllable zone: it does one solid colour (pick or preset), a brightness level, or the firmware Spectrum Cycle. There is no per-zone colour or gradient - the hardware ignores zone-scoped writes. Colours don't survive a reboot on their own - apply the KbdBacklightFix tweak (Power tab) to re-assert the last setting at login and after resume.", Tone::Muted);
        ui.card_end();

        ui.card_begin("Brightness");
        ui.slider("bright", &brightness, 0, 100, ui.S(420), "%");
        if (ui.released()) {
            std::string h = safe_hex(hex);
            int b = brightness;
            auto z = zones_for(h);
            std::string m = static_mode();
            bool pa = push_accent;
            run(a, "brightness " + std::to_string(b) + "%", [h, b, z, m, pa] { kbd::set_all(h, b); kbd::save_state(z, b, m, 50, pa); });
        }
        ui.card_end();

        ui.card_begin("Whole keyboard");
        color_edit(ui, "kbdcolor", &hex);
        if (ui.button("Apply colour", Btn::Success, 0, !busy)) apply_all(a);
        ui.label_c("Presets:", Tone::Muted);
        static const std::pair<const char*, const char*> presets[] = {{"White", "#ffffff"}, {"Red", "#ff0000"}, {"Green", "#00ff00"}, {"Blue", "#0000ff"}, {"Cyan", "#00ffff"}, {"Magenta", "#ff00ff"}, {"Amber", "#ff6a00"}};
        for (auto& [n, h] : presets) { ui.same_line(); if (ui.button(n, Btn::Secondary, 0, !busy)) { hex = h; apply_all(a); } }
        ui.card_end();

        ui.card_begin("Desktop accent colour");
        bool m = match_accent;
        if (ui.toggle("Keyboard follows the desktop accent colour", &m)) {
            match_accent = m;
            if (m) {
                push_accent = false;
                hex = "#" + [] { std::string h = kbd::accent_hex(); for (auto& c : h) c = static_cast<char>(std::tolower(c)); return h; }();
                mode = "accent";
                std::string h = hex;
                int b = brightness;
                auto z = zones_for(h);
                run(a, "follow desktop accent " + h + " @ " + std::to_string(b) + "%", [h, b, z] { kbd::set_all(h, b); kbd::save_state(z, b, "accent", 50, false); });
            } else apply_all(a);
        }
        ui.tooltip("On: the keyboard takes the Plasma accent now and re-reads the CURRENT accent on every re-assert (login, resume, tray start) - it follows the accent if you change it later. Turning this on turns off the option below.");
        bool p = push_accent;
        if (ui.toggle("Desktop accent follows the keyboard colour", &p, !is_effect(mode))) {
            push_accent = p;
            auto z = zones_for(safe_hex(hex));
            int b = brightness;
            if (p) {
                match_accent = false;
                if (mode == "accent") mode = "zones";
                kbd::save_state(z, b, "zones", 50, true);
                push_accent_now(a);
            } else kbd::save_state(z, b, static_mode(), 50, false);
        }
        ui.tooltip("On: every keyboard colour you set here is also written into Plasma's accent-colour setting (kdeglobals AccentColor), with accent-from-wallpaper turned off - the desktop repaints to match. Disabled while Spectrum Cycle is running (no single colour to copy). Turning this on turns off the option above.");
        ui.card_end();

        ui.card_begin("Effect");
        ui.label_c("Speed", Tone::Normal, Font::Regular, ui.S(60));
        ui.same_line();
        ui.slider("speed", &speed, 0, 100, ui.S(360));
        if (ui.button("Spectrum Cycle", Btn::Secondary, 0, !busy)) {
            int b = brightness, sp = speed;
            auto z = zones_for(safe_hex(hex));
            set_mode("spectrum");
            run(a, "effect spectrum @ speed " + std::to_string(sp) + ", " + std::to_string(b) + "%", [b, sp, z] { kbd::set_effect("spectrum", sp, b); kbd::save_state(z, b, "spectrum", sp, false); });
        }
        ui.same_line();
        if (ui.button("Solid colour  (leave the effect)", Btn::Success, 0, !busy)) apply_all(a);
        ui.card_end();

        if (ui.button("Turn backlight off", Btn::Outline, 0, !busy)) { brightness = 0; run(a, "backlight off", [] { kbd::off(); }); }
        ui.same_line();
        if (ui.button("Reset backlight  (unfreeze)", Btn::OutlineWarn, 0, !busy)) run(a, "reset backlight (restart OpenRGB + re-apply)", [] { kbd::reset(); });
    }
};
}  // namespace

std::unique_ptr<Page> make_keyboard() { return std::make_unique<Keyboard>(); }

}  // namespace tt::app
