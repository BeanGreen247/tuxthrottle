// Data-driven category pages (tweaks / apps from config/*.json) and the Presets page.
#include <algorithm>

#include "../engine.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {

Tone state_tone(const engine::Item& it) {
    if (!it.hw_supported) return Tone::Muted;
    const std::string& s = it.state;
    if (s == "applied") return Tone::Success;
    if (s == "pending") return Tone::Info;
    if (s == "error" || s == "drifted") return Tone::Warning;
    if (s == "failed") return Tone::Danger;
    return Tone::Muted;
}

std::string state_text(const engine::Item& it) {
    if (!it.hw_supported) return "unsupported";
    if (it.state == "unknown") return "checking...";
    std::string l = engine::state_label(it);
    return l;
}

bool reboot_hint(const std::vector<const engine::Item*>& v) {
    for (const auto* i : v) {
        std::string id = i->id, all;
        std::transform(id.begin(), id.end(), id.begin(), ::tolower);
        for (const auto& c : i->apply_cmds) all += c + " ";
        if (id.find("cmdline") != std::string::npos || all.find("grubby") != std::string::npos || all.find("{KARGS_ADD}") != std::string::npos) return true;
    }
    return false;
}

std::string bullet_list(const std::vector<const engine::Item*>& v) {
    std::string out;
    for (const auto* i : v) out += "  \xE2\x80\xA2  " + i->content + "\n";
    return out;
}

struct Category : Page {
    explicit Category(std::string cat) { title = cat; group = "Tweaks"; }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title(title);
        auto rec = engine::recommended(a.items, title, true);
        if (!rec.empty()) {
            if (ui.button("Apply section recommendations (" + std::to_string(rec.size()) + ")", Btn::OutlineSuccess, 0, !a.busy())) ui.open_popup("confirm:rec-" + title);
            ui.tooltip("Apply the developer's curated picks for THIS category in one go (a snapshot is taken first).");
            ui.spacing(ui.S(6));
            std::string body = "Apply the developer's recommended " + std::to_string(rec.size()) + " item(s) for \"" + title + "\"?\n\n" + bullet_list(rec) + "\nA snapshot is taken first so you can roll back from the Profiles tab.";
            if (reboot_hint(rec)) body += "\nSome of these change kernel boot params - reboot to finish.";
            if (a.confirm("rec-" + title, "Apply section recommendations", body, "Apply")) {
                std::vector<std::string> ids;
                for (const auto* i : rec) ids.push_back(i->id);
                a.apply_ids("Applying recommended - " + title, ids, "recommended-" + title);
            }
        }
        for (auto& it : a.items) {
            if (it.category != title || it.hidden) continue;
            ui.push_id(it.id);
            ui.card_begin("");
            bool tick = a.ticks.count(it.id) ? a.ticks[it.id] : it.done();
            if (ui.checkbox("", &tick, it.hw_supported)) a.ticks[it.id] = tick;
            ui.same_line();
            {   // state dot: green applied, blue pending reboot, amber reverted/error, red failed, grey otherwise
                Rect dr = ui.item_rect(ui.S(10), ui.line_h(ui.S(14), Font::Bold));
                ui.fill({dr.x, dr.y + dr.h / 2 - ui.S(4), ui.S(8), ui.S(8)}, ui.tone_color(state_tone(it)), ui.S(4));
                ui.same_line(ui.S(6));
            }
            ui.label(it.content, Tone::Normal, ui.S(14), Font::Bold);
            if (it.risk == "advanced") { ui.same_line(); ui.pill("ADVANCED", Tone::Warning); }
            if (it.recommended) { ui.same_line(); ui.pill("RECOMMENDED", Tone::Success); }
            std::string st = state_text(it);
            int sw = ui.text_w(st, ui.S(12), Font::Bold);
            Rect row = ui.last_item();
            ui.text_at(ui.region().right() - sw - ui.S(24), row.y + (row.h - ui.line_h(ui.S(12), Font::Bold)) / 2, st, ui.tone_color(state_tone(it)), ui.S(12), Font::Bold);
            ui.indent(ui.S(30));
            ui.wrapped(it.description, Tone::Muted, ui.avail_w());
            ui.unindent(ui.S(30));
            ui.card_end();
            ui.pop_id();
        }
    }
};

struct Presets : Page {
    Presets() { title = "Presets"; }
    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Presets", "One click applies a curated bundle of tweaks + installs apps.");

        ui.card_begin("Last preset - sensor change (30 s after apply)");
        std::string dt = a.delta_text();
        ui.wrapped(dt.empty() ? "Apply a preset and this shows how temps / clocks / TDP moved." : dt, Tone::Muted);
        ui.card_end();
        auto recs = engine::recommended(a.items, "", true);
        ui.card_begin("Developer recommendations");
        ui.wrapped("Applies every item the developer marked as recommended across all categories in one pass, and offers to turn on the background daemon that powers the fan curve, AC/battery auto-switch and the time schedule. A snapshot is taken first so you can roll back from the Profiles tab.", Tone::Muted);
        engine::Item* daemon = a.find_item("FanCurveDaemon");
        bool want_daemon = daemon && daemon->hw_supported && !daemon->hidden && !daemon->done();
        if (ui.button("Apply all recommendations", Btn::Success, 0, !a.busy())) ui.open_popup("confirm:rec-all");
        ui.same_line();
        ui.label(recs.empty() ? "all applied" : std::to_string(recs.size()) + " not yet applied", recs.empty() ? Tone::Success : Tone::Muted);
        ui.card_end();
        {
            std::string body = "Apply the developer's recommended set - " + std::to_string(recs.size() + (want_daemon ? 1 : 0)) + " item(s) across every category?\n\n" + bullet_list(recs);
            if (want_daemon) body += "  \xE2\x80\xA2  Fan-curve + AC-switch daemon (enables the schedule)\n";
            body += "\nA snapshot is taken first so you can roll back from the Profiles tab.";
            if (reboot_hint(recs)) body += "\nSome of these change kernel boot params - reboot to finish.";
            if (a.confirm("rec-all", "Apply all recommendations", body, "Apply")) {
                std::vector<std::string> ids;
                for (const auto* i : recs) ids.push_back(i->id);
                if (want_daemon) ids.push_back("FanCurveDaemon");
                if (ids.empty()) a.toast("All recommended items are already applied.");
                else a.apply_ids("Applying all recommendations", ids, "recommended-all");
            }
        }

        for (const auto& kv : a.presets.object()) {
            const Json& d = kv.second;
            ui.push_id(kv.first);
            ui.card_begin(d["Content"].str());
            ui.wrapped(d["Description"].str(), Tone::Muted);
            if (ui.button("Apply This Preset", Btn::Success, 0, !a.busy())) ui.open_popup("confirm:preset-" + kv.first);
            if (a.confirm("preset-" + kv.first, "Confirm preset",
                          "Apply preset '" + d["Content"].str() + "'?\n\n" + std::to_string(d["tweaks"].array().size()) + " tweaks + " + std::to_string(d["apps"].array().size()) + " apps will be applied/installed.", "Apply")) {
                std::vector<std::string> ids;
                for (const auto& t : d["tweaks"].array()) ids.push_back(t.str());
                for (const auto& t : d["apps"].array()) ids.push_back(t.str());
                a.start_delta_watch(d["Content"].str());
                a.apply_ids("Applying preset - " + d["Content"].str(), ids, "preset-" + kv.first);
            }
            ui.card_end();
            ui.pop_id();
        }
    }
};

}  // namespace

std::unique_ptr<Page> make_category(const std::string& cat) { return std::make_unique<Category>(cat); }
std::unique_ptr<Page> make_presets() { return std::make_unique<Presets>(); }

}  // namespace tt::app
