#include "../sensors.hpp"
#include "../util.hpp"
#include "pages.hpp"
#include "widgets.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Dashboard : Page {
    Dashboard() { title = "Dashboard"; }
    void enter(App& a) override { a.sampler_active = true; }
    void leave(App& a) override { a.sampler_active = false; if (a.csv_logging()) a.set_csv_logging(false); }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Dashboard");
        Sample s = a.sample();
        if (!s.valid) {
            static const char* spin[] = {"|", "/", "-", "\\"};
            ui.label(std::string(spin[(SDL_GetTicks() / 90) % 4]) + "   Loading sensors...", Tone::Muted, ui.S(15));
            ui.wake_after(90);
            return;
        }
        Color acc = ui.th.accent, green = ui.th.success, amber = ui.th.warning, red = ui.th.danger;
        auto num = [](const std::optional<double>& v) { return v ? *v : 0.0; };
        auto numi = [](const std::optional<int>& v) { return v ? static_cast<double>(*v) : 0.0; };

        ui.columns(2, ui.S(14));
        ui.card_begin("CPU");
        ui.columns(2, ui.S(6));
        ring_gauge(ui, "CPU temp", "\xC2\xB0""C", num(s.cpu_temp), 100, acc, 0, 124);
        ui.next_column();
        ring_gauge(ui, "CPU clock", "GHz", num(s.cpu_freq), 5.0, green, 2, 124);
        ui.end_columns();
        ui.columns(2, ui.S(6));
        ring_gauge(ui, "CPU power", "W", num(s.cpu_power), 65, acc, 0, 124);
        ui.next_column();
        ring_gauge(ui, "iGPU clock", "MHz", numi(s.igpu_clock), 2000, green, 0, 124);
        ui.end_columns();
        ui.card_end();
        ui.next_column();
        ui.card_begin("GPU");
        ui.columns(2, ui.S(6));
        ring_gauge(ui, "dGPU temp", "\xC2\xB0""C", numi(s.dgpu_temp), 100, amber, 0, 124);
        ui.next_column();
        ring_gauge(ui, "dGPU clock", "MHz", numi(s.dgpu_clock), 2100, amber, 0, 124);
        ui.end_columns();
        ui.columns(2, ui.S(6));
        ring_gauge(ui, "dGPU util", "%", numi(s.dgpu_util), 100, red, 0, 124);
        ui.next_column();
        ring_gauge(ui, "dGPU power", "W", num(s.dgpu_power), 80, amber, 0, 124);
        ui.end_columns();
        ui.card_end();
        ui.end_columns();

        if (!s.rapl_ok)
            ui.wrapped("CPU power reads 0/blank - Linux locks RAPL power counters to root by default. Install the 'RaplPowerPermissions' tweak (Power tab) to fix this.", Tone::Warning);

        ui.card_begin("Details");
        std::string cpu = s.cpu_temp ? format_double(num(s.cpu_freq), 2) + " GHz, " + format_double(*s.cpu_temp, 0) + " C" : "n/a";
        if (s.cpu_temp && s.cpu_power) cpu += ", " + format_double(*s.cpu_power, 1) + " W";
        const Json& stock = sensors::model_profile()["cpu"]["stock_ppt_w"];
        if (s.stapm_limit && !stock.array().empty()) {
            double d = *s.stapm_limit - stock.array()[0].as_number();
            cpu += "  (STAPM " + format_double(*s.stapm_limit, 0) + "W, " + (d >= 0 ? "+" : "") + format_double(d, 0) + "W vs stock " + format_double(stock.array()[0].as_number(), 0) + "W)";
        }
        ui.label("CPU: " + cpu, Tone::Normal, ui.S(13), Font::Mono);
        std::string ig = s.igpu_clock ? std::to_string(*s.igpu_clock) + " MHz" + (s.igpu_temp ? ", " + format_double(*s.igpu_temp, 0) + " C" : "") : "n/a";
        ui.label("iGPU: " + ig, Tone::Normal, ui.S(13), Font::Mono);
        std::string dg = s.dgpu_clock ? std::to_string(*s.dgpu_clock) + " MHz, " + std::to_string(s.dgpu_temp.value_or(0)) + " C, " + std::to_string(s.dgpu_util.value_or(0)) + "% util" +
                                            (s.dgpu_power ? ", " + format_double(*s.dgpu_power, 0) + " W" : "")
                                      : "n/a (asleep or no nvidia-smi)";
        ui.label("dGPU: " + dg, Tone::Normal, ui.S(13), Font::Mono);
        ui.card_end();

        ui.card_begin("History");
        ui.label("rolling ~3 min", Tone::Muted, ui.S(12));
        int cw = (ui.avail_w() - ui.S(10)) / 2, ch = ui.S(84);
        history_chart(ui, "CPU \xC2\xB0""C", a.hist("cpu_temp"), acc, {{90, red, "throttle"}}, cw, ch);
        ui.same_line(ui.S(10));
        history_chart(ui, "CPU W", a.hist("cpu_power"), acc, s.stapm_limit ? std::vector<Threshold>{{*s.stapm_limit, amber, "STAPM cap"}} : std::vector<Threshold>{}, cw, ch);
        history_chart(ui, "dGPU \xC2\xB0""C", a.hist("dgpu_temp"), amber, {{87, red, "throttle"}}, cw, ch);
        ui.same_line(ui.S(10));
        history_chart(ui, "dGPU W", a.hist("dgpu_power"), amber, {}, cw, ch);
        bool csv = a.csv_logging();
        if (ui.toggle("Log this session to CSV", &csv)) a.set_csv_logging(csv);
        if (a.csv_logging()) { ui.same_line(); ui.label(a.csv_path(), Tone::Muted, ui.S(11), Font::Mono); }
        ui.card_end();

        ui.card_begin("Game Mode");
        bool gm = s.gamemode;
        if (ui.toggle("Performance profile + GPU perf-state forcing", &gm)) {
            auto r = sensors::set_game_mode(gm);
            a.toast(r.first ? (gm ? "Game Mode on" : "Game Mode off") : "Game Mode failed: " + r.second, r.first ? Tone::Success : Tone::Danger);
        }
        ui.wrapped("Same effect as pressing the G-key or clicking the tray icon. Needs the Power/GPU tweaks below installed first.", Tone::Muted);
        ui.card_end();
    }
};
}  // namespace

std::unique_ptr<Page> make_dashboard() { return std::make_unique<Dashboard>(); }

}  // namespace tt::app
