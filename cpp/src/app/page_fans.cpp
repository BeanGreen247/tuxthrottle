#include <algorithm>
#include <cmath>
#include <ctime>

#include "../devices.hpp"
#include "../gamecfg.hpp"
#include "../powerd.hpp"
#include "../sensors.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
constexpr int kPoints = 10;
const int kDefault[kPoints][2] = {{40, 0}, {48, 12}, {55, 25}, {62, 38}, {69, 52}, {75, 66}, {81, 78}, {86, 88}, {91, 95}, {95, 100}};
const int kSilent[kPoints][2] = {{45, 0}, {55, 0}, {62, 10}, {68, 20}, {74, 32}, {80, 45}, {85, 60}, {89, 75}, {93, 90}, {96, 100}};
const int kAggressive[kPoints][2] = {{38, 15}, {45, 30}, {52, 45}, {58, 58}, {64, 70}, {70, 80}, {76, 88}, {82, 94}, {88, 98}, {93, 100}};

struct Fans : Page {
    Fans() { title = "Fans"; group = "Hardware"; }
    bool loaded = false, manual = false, tie = false, fc_enabled = false;
    int profile_idx = 0, sensor_idx = 0, hys = 3;
    std::vector<std::string> choices;
    std::map<int, int> boost, pwm;
    int rows[kPoints][2];
    std::string live_text;
    std::optional<std::pair<double, double>> live_point;

    void enter(App& a) override { a.sampler_active = true; loaded = false; }
    void leave(App& a) override { a.sampler_active = false; }

    static std::vector<std::pair<double, double>> resample(const std::vector<std::pair<double, double>>& in, int n = kPoints) {
        auto clean = in;
        std::sort(clean.begin(), clean.end());
        if (static_cast<int>(clean.size()) == n) return clean;
        std::vector<std::pair<double, double>> out;
        if (clean.size() < 2) { for (int i = 0; i < n; ++i) out.push_back({kDefault[i][0], kDefault[i][1]}); return out; }
        auto at = [&](double t) {
            for (size_t i = 0; i + 1 < clean.size(); ++i)
                if (t <= clean[i + 1].first) {
                    if (clean[i + 1].first == clean[i].first) return clean[i].second;
                    double f = (t - clean[i].first) / (clean[i + 1].first - clean[i].first);
                    return clean[i].second + f * (clean[i + 1].second - clean[i].second);
                }
            return clean.back().second;
        };
        double t0 = clean.front().first, t1 = clean.back().first;
        for (int i = 0; i < n; ++i) { double t = t0 + (t1 - t0) * i / (n - 1); out.push_back({std::round(t), std::round(at(t))}); }
        return out;
    }

    void set_rows(const std::vector<std::pair<double, double>>& pts) {
        auto p = resample(pts);
        for (int i = 0; i < kPoints; ++i) { rows[i][0] = static_cast<int>(p[static_cast<size_t>(i)].first); rows[i][1] = static_cast<int>(p[static_cast<size_t>(i)].second); }
    }
    std::vector<std::pair<double, double>> points() const {
        std::vector<std::pair<double, double>> v;
        for (int i = 0; i < kPoints; ++i) v.push_back({static_cast<double>(rows[i][0]), static_cast<double>(rows[i][1])});
        std::sort(v.begin(), v.end());
        return v;
    }

    void load(App& a) {
        choices = sensors::platform_profile_choices();
        std::string cur = sensors::get_platform_profile();
        profile_idx = 0;
        for (size_t i = 0; i < choices.size(); ++i) if (choices[i] == cur) profile_idx = static_cast<int>(i);
        auto fans = sensors::read_fans();
        auto boosts = sensors::get_fan_boost();
        for (size_t k = 0; k < fans.size(); ++k) {
            boost[fans[k].index] = static_cast<int>(std::lround((k < boosts.size() ? boosts[k] : 0) / 255.0 * 100));
            pwm[fans[k].index] = 50;
        }
        tie = a.read_state("kbd_profile_tie.json")["enabled"].as_bool();
        Json fc = a.read_state("powerd.json")["fan_curve"];
        fc_enabled = fc["enabled"].as_bool();
        std::string s = fc["sensor"].str("max");
        sensor_idx = s == "cpu" ? 1 : s == "gpu" ? 2 : 0;
        hys = static_cast<int>(fc["hysteresis_c"].as_number(3));
        std::vector<std::pair<double, double>> pts;
        for (const auto& p : fc["points"].array()) if (p.array().size() >= 2) pts.push_back({p.array()[0].as_number(), p.array()[1].as_number()});
        if (pts.empty()) for (int i = 0; i < kPoints; ++i) pts.push_back({kDefault[i][0], kDefault[i][1]});
        set_rows(pts);
        loaded = true;
    }

    static std::string tie_color(const std::string& p) { return p == "quiet" ? "3B82F6" : p == "balanced" ? "FFFFFF" : p == "performance" ? "FF3B3B" : ""; }

    void tie_to_profile(App& a, const std::string& name) {
        if (!tie) return;
        std::string color = tie_color(name);
        if (color.empty()) return;
        a.run_bg([&a, name, color] {
            try { kbd::set_all(color); a.log("[Keyboard] tied to profile '" + name + "' -> #" + color); }
            catch (const std::exception& e) { a.log(std::string("[Keyboard] profile tie-in failed: ") + e.what()); }
        });
    }

    void set_boost(App& a, int index) {
        auto r = sensors::set_fan_boost(index, static_cast<int>(std::lround(boost[index] * 255 / 100.0)));
        a.log("[Fans] fan " + std::to_string(index) + " boost -> " + std::to_string(boost[index]) + "%" + (r.first ? "" : "  FAILED: " + r.second));
    }

    void boost_60s(App& a) {
        std::string restore;
        std::string was;
        for (auto& [i, pct] : boost) {
            restore += (restore.empty() ? "" : "; ") + shell_quote(ctl_exe()) + " set fan-boost " + std::to_string(i) + " " + std::to_string(pct);
            was += (was.empty() ? "" : ", ") + std::string("fan ") + std::to_string(i) + ": " + std::to_string(pct) + "%";
        }
        for (auto& [i, pct] : boost) { pct = 100; set_boost(a, i); }
        std::string unit = "tuxthrottle-fanboost-" + std::to_string(std::time(nullptr));
        auto r = run_argv({"systemd-run", "--unit=" + unit, "--on-active=60", "--collect", "--description=TuxThrottle: restore fan boost after a 60s burst", "/bin/sh", "-c", restore}, 10000);
        if (r.code == 0) {
            a.log("[Fans] boosted to 100% for 60s - auto-restoring to (" + was + ") after; the revert runs as an independent timer, so it still fires even if you close TuxThrottle");
            fixlog::log_event("fan-boost-60s", "boosted to 100% for 60s, will restore " + was, "info", a.user);
        } else a.log("[Fans] boosted to 100% but couldn't schedule the auto-revert - set the sliders back manually in ~60s");
    }

    void preset(App& a, const std::string& kind) {
        std::string prof = kind == "auto" ? "balanced" : "performance";
        int b = kind == "auto" ? 0 : kind == "cool" ? 60 : 100;
        if (kind == "auto") sensors::restore_fan_auto();
        if (std::find(choices.begin(), choices.end(), prof) != choices.end()) {
            sensors::set_platform_profile(prof);
            for (size_t i = 0; i < choices.size(); ++i) if (choices[i] == prof) profile_idx = static_cast<int>(i);
            tie_to_profile(a, prof);
        }
        for (auto& [i, pct] : boost) { pct = b; sensors::set_fan_boost(i, static_cast<int>(std::lround(b * 255 / 100.0))); }
        a.log("[Fans] preset: " + kind + " (profile " + prof + ", boost " + std::to_string(b) + "%)");
    }

    void save_curve(App& a) {
        Json pd = a.read_state("powerd.json");
        Json fc = Json::obj();
        fc.set("enabled", fc_enabled);
        fc.set("sensor", sensor_idx == 1 ? "cpu" : sensor_idx == 2 ? "gpu" : "max");
        fc.set("hysteresis_c", hys);
        Json pts = Json::arr();
        for (const auto& p : points()) { Json x = Json::arr(); x.push(static_cast<int>(p.first)); x.push(static_cast<int>(p.second)); pts.push(x); }
        fc.set("points", pts);
        pd.set("fan_curve", fc);
        a.write_state("powerd.json", pd);
        a.run_bg([&a] {
            auto r = run_shell(shell_quote(ctl_exe()) + " daemon reload 2>&1");
            std::string out = trim(r.out);
            if (!out.empty()) a.log("[Fans] " + split_lines(out).back());
        });
        a.log(std::string("[Fans] fan curve saved (") + (fc_enabled ? "on" : "off") + ")");
        a.toast("Fan curve saved", Tone::Success);
    }

    void draw_curve(Ui& ui) {
        int w = ui.avail_w(), h = ui.S(130);
        Rect r = ui.item_rect(w, h);
        ui.fill(r, ui.th.sunken, ui.S(6));
        int pad = ui.S(10);
        double tmin = 30, tmax = 100;
        auto X = [&](double t) { return static_cast<float>(r.x + pad + (t - tmin) / (tmax - tmin) * (r.w - 2 * pad)); };
        auto Y = [&](double b) { return static_cast<float>(r.bottom() - pad - b / 100 * (r.h - 2 * pad)); };
        Color grid = ui.th.chart_axis, acc = ui.th.accent;
        for (int gb : {0, 25, 50, 75, 100}) { float y = Y(gb); ui.draw([=](Gfx& g) { g.line(static_cast<float>(r.x + pad), y, static_cast<float>(r.right() - pad), y, alpha(grid, 120), 1); }); }
        auto pts = points();
        std::vector<SDL_FPoint> line;
        for (auto& p : pts) line.push_back({X(p.first), Y(p.second)});
        ui.draw([=](Gfx& g) { g.polyline(line, acc, 2); for (auto& p : line) g.disc(p.x, p.y, 3.5f, acc); });
        for (auto& p : pts) ui.text_at(static_cast<int>(X(p.first)) - ui.S(8), static_cast<int>(Y(p.second)) - ui.S(16), std::to_string(static_cast<int>(p.first)), ui.tone_color(Tone::Muted), ui.S(9));
        if (live_point) {
            float x = X(std::clamp(live_point->first, tmin, tmax)), y = Y(std::clamp(live_point->second, 0.0, 100.0));
            Color red = ui.th.danger;
            ui.draw([=](Gfx& g) { g.arc(x, y, 5.5f, 2, 0, 360, red); g.disc(x, y, 2.2f, red); });
        }
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) load(a);
        a.page_title("Fans");
        auto fans = sensors::read_fans();
        if (fans.empty()) {
            ui.wrapped("No fan interface found (expected the alienware_wmi / dell_smm hwmon devices). Is the alienware-wmi kernel module loaded on this machine?", Tone::Warning);
            return;
        }
        ui.wake_after(2000);
        ui.card_begin("How this works");
        ui.wrapped("Thermal profile + fan boost steer the firmware (AWCC-style) fan curve - boost only adds airflow on top, it can't slow a fan below the automatic curve. Manual PWM (advanced) takes the EC off its curve entirely; it's floored so the fans never fully stop, but watch temperatures and hit \"Restore automatic\" when done. Nothing here persists across a reboot yet.", Tone::Muted);
        ui.card_end();

        if (!choices.empty()) {
            ui.card_begin("Thermal profile");
            std::vector<std::string> labels;
            for (auto c : choices) { c[0] = static_cast<char>(std::toupper(c[0])); labels.push_back(c); }
            if (ui.segmented("profile", &profile_idx, labels)) {
                const std::string& name = choices[static_cast<size_t>(profile_idx)];
                auto r = sensors::set_platform_profile(name);
                a.log("[Fans] thermal profile -> " + name + (r.first ? "" : "  FAILED: " + r.second));
                if (r.first) tie_to_profile(a, name);
            }
            if (ui.toggle("Tie keyboard colour to the active profile (Quiet=blue / Balanced=white / Performance=red)", &tie)) {
                Json j = Json::obj();
                j.set("enabled", tie);
                a.write_state("kbd_profile_tie.json", j);
                if (tie) tie_to_profile(a, sensors::get_platform_profile());
            }
            ui.card_end();
        }

        ui.card_begin("Fans & boost");
        for (const auto& f : fans) {
            ui.push_id(f.index);
            ui.label_c(f.label, Tone::Normal, Font::Bold, ui.S(100));
            ui.same_line(ui.S(12));
            ui.label_c(std::to_string(f.rpm) + " rpm", Tone::Muted);
            ui.same_line(ui.S(18));
            ui.label_c("Boost");
            ui.same_line(ui.S(6));
            ui.slider("boost", &boost[f.index], 0, 100, ui.S(260), "%");
            if (ui.released()) set_boost(a, f.index);
            for (auto [lbl, pct] : {std::pair<const char*, int>{"0", 0}, {"50", 50}, {"Max", 100}}) {
                ui.same_line(ui.S(4));
                if (ui.button(lbl, Btn::Outline, ui.S(50))) { boost[f.index] = pct; set_boost(a, f.index); }
            }
            ui.pop_id();
        }
        ui.spacing(ui.S(4));
        ui.label_c("Presets:", Tone::Muted);
        ui.same_line();
        if (ui.button("Auto / silent", Btn::Success)) preset(a, "auto");
        ui.same_line();
        if (ui.button("Cooler", Btn::OutlineInfo)) preset(a, "cool");
        ui.same_line();
        if (ui.button("Max cooling", Btn::OutlineDanger)) preset(a, "max");
        ui.same_line(ui.S(16));
        if (ui.button("Boost 100% for 60s", Btn::OutlineWarn)) boost_60s(a);
        ui.tooltip("Spin every fan to max right now, then automatically put boost back to whatever it was before - for the 'about to load into a match' moment, without committing to a whole profile change. The revert is a background timer independent of this window, so it still happens even if you close the app.");
        ui.card_end();

        if (!sensors::get_pwm_state().empty()) {
            ui.card_begin("Manual PWM - advanced / risky", Tone::Danger);
            bool m = manual;
            if (ui.toggle("Enable manual PWM control (takes the EC off its automatic curve)", &m)) {
                if (m) ui.open_popup("confirm:fan-manual");
                else { manual = false; }
            }
            if (a.confirm("fan-manual", "Manual fan control", "This takes the fans off the firmware's automatic curve. They stay floored so they can't stop, but keep an eye on temperatures and use \"Restore automatic\" when you're done.\n\nProceed?", "Proceed")) {
                manual = true;
                for (const auto& f : fans) { auto r = sensors::set_pwm_manual(f.index, static_cast<int>(std::lround(pwm[f.index] * 255 / 100.0))); a.log("[Fans] fan " + std::to_string(f.index) + " manual PWM -> " + std::to_string(pwm[f.index]) + "%" + (r.first ? "" : "  FAILED: " + r.second)); }
            }
            for (const auto& f : fans) {
                ui.push_id(1000 + f.index);
                ui.label_c(f.label, Tone::Normal, Font::Regular, ui.S(100));
                ui.same_line(ui.S(12));
                if (ui.slider("pwm", &pwm[f.index], 30, 100, ui.S(300), "%") && manual) { /* applied on release */ }
                if (ui.released() && manual) { auto r = sensors::set_pwm_manual(f.index, static_cast<int>(std::lround(pwm[f.index] * 255 / 100.0))); a.log("[Fans] fan " + std::to_string(f.index) + " manual PWM -> " + std::to_string(pwm[f.index]) + "%" + (r.first ? "" : "  FAILED: " + r.second)); }
                ui.pop_id();
            }
            if (ui.button("Restore automatic", Btn::Success)) {
                auto r = sensors::restore_fan_auto();
                manual = false;
                for (auto& [i, pct] : boost) pct = 0;
                a.log(std::string("[Fans] restored automatic control") + (r.first ? "" : "  (errors: " + r.second + ")"));
            }
            ui.card_end();
        }

        // closed-loop fan curve
        Sample s = a.sample();
        if (s.valid) {
            double cpu = s.cpu_temp.value_or(0), gt = s.dgpu_temp ? *s.dgpu_temp : -1;
            double temp = sensor_idx == 1 ? cpu : sensor_idx == 2 ? gt : std::max(cpu, gt);
            if (temp >= 0) {
                int tgt = powerd::interp(points(), temp);
                live_text = "now " + format_double(temp, 0) + "\xC2\xB0""C -> target boost " + std::to_string(tgt) + "%";
                live_point = {temp, static_cast<double>(tgt)};
            }
        }
        ui.card_begin("Custom fan curve (closed-loop)");
        ui.wrapped("A background daemon maps temperature to an additive fan boost on a curve you set. It only ever adds airflow over the firmware curve, and restores automatic control when stopped. Needs the \"Fan-curve + AC-switch daemon\" tweak (Power tab) enabled to actually run at boot.", Tone::Muted);
        ui.toggle("Fan curve enabled", &fc_enabled);
        ui.label_c("Drive from:", Tone::Muted);
        ui.same_line();
        ui.segmented("sensor", &sensor_idx, {"Hotter of CPU/GPU", "CPU", "GPU"});
        ui.columns(2, ui.S(24));
        for (int blk = 0; blk < 2; ++blk) {
            ui.label("Temp \xC2\xB0""C", Tone::Muted, ui.S(12));
            ui.same_line(ui.S(70));
            ui.label("Boost %", Tone::Muted, ui.S(12));
            for (int i = blk * (kPoints / 2); i < (blk + 1) * (kPoints / 2); ++i) {
                ui.push_id(2000 + i);
                ui.spinbox("t", &rows[i][0], 25, 105, 1, ui.S(120));
                ui.same_line();
                ui.spinbox("b", &rows[i][1], 0, 100, 1, ui.S(120));
                ui.pop_id();
            }
            if (blk == 0) ui.next_column();
        }
        ui.end_columns();
        draw_curve(ui);
        ui.label_c("Presets:", Tone::Muted);
        for (auto [nm, tab] : {std::pair<const char*, const int(*)[2]>{"Silent", kSilent}, {"Balanced", kDefault}, {"Aggressive", kAggressive}}) {
            ui.same_line();
            if (ui.button(nm, Btn::OutlineInfo)) { std::vector<std::pair<double, double>> v; for (int i = 0; i < kPoints; ++i) v.push_back({tab[i][0], tab[i][1]}); set_rows(v); }
        }
        ui.same_line(ui.S(14));
        if (ui.button("Linear fill", Btn::Outline)) {
            int t0 = rows[0][0], tN = rows[kPoints - 1][0], b0 = rows[0][1], bN = rows[kPoints - 1][1];
            if (tN <= t0) tN = t0 + kPoints;
            for (int i = 0; i < kPoints; ++i) { double f = static_cast<double>(i) / (kPoints - 1); rows[i][0] = static_cast<int>(std::lround(t0 + (tN - t0) * f)); rows[i][1] = static_cast<int>(std::lround(b0 + (bN - b0) * f)); }
        }
        ui.label_c("Cool-down hysteresis");
        ui.same_line();
        ui.spinbox("hys", &hys, 0, 10, 1, ui.S(110));
        ui.same_line();
        ui.label_c("\xC2\xB0""C");
        ui.same_line(ui.S(16));
        if (ui.button("Save curve", Btn::Success)) save_curve(a);
        ui.same_line();
        ui.label_c(live_text, Tone::Muted);
        ui.card_end();
    }
};
}  // namespace

std::unique_ptr<Page> make_fans() { return std::make_unique<Fans>(); }

}  // namespace tt::app
