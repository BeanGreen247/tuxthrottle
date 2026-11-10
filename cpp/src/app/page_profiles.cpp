#include <algorithm>

#include "../profiles.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Profiles : Page {
    Profiles() { title = "Profiles"; group = "System"; }
    static constexpr int kRows = 4;

    std::string new_name, preview, pending_name, pending_target, export_name;
    bool pending_gpu = false, loaded = false;
    std::vector<std::string> names;
    std::vector<profiles::SnapshotInfo> snaps;
    bool gp_enabled = false;
    std::string gp_proc[kRows];
    int gp_prof[kRows] = {0, 0, 0, 0};
    int gp_default = 0;
    bool sc_enabled = false;
    std::string sc_from[kRows], sc_to[kRows];
    int sc_apply[kRows] = {0, 0, 0, 0}, sc_outside = 0;
    bool sc_day[kRows][7];

    void enter(App& a) override { reload(a); }

    void reload(App& a) {
        names = profiles::list_profiles(a.user);
        snaps = profiles::list_snapshots(a.user);
        if (snaps.size() > 15) snaps.resize(15);
        try {
            Json st = profiles::capture_state(a.user);
            std::string keys;
            for (const auto& kv : st.object()) if (kv.first != "captured") keys += (keys.empty() ? "" : ", ") + kv.first;
            preview = "will capture: " + (keys.empty() ? "(nothing readable)" : keys);
        } catch (const std::exception& e) { preview = std::string("(capture preview failed: ") + e.what() + ")"; }
        Json pd = a.read_state("powerd.json");
        const Json& gp = pd["game_profiles"];
        gp_enabled = gp["enabled"].as_bool();
        size_t r = 0;
        for (const auto& kv : gp["match"].object()) { if (r >= kRows) break; gp_proc[r] = kv.first; gp_prof[r] = index_of(gp_names(), kv.second.str()); ++r; }
        for (; r < kRows; ++r) { gp_proc[r].clear(); gp_prof[r] = 0; }
        gp_default = index_of(gp_names(), gp["default"].str());
        const Json& sc = pd["schedule"];
        sc_enabled = sc["enabled"].as_bool();
        const auto& rules = sc["rules"].array();
        for (size_t i = 0; i < kRows; ++i) {
            const Json empty;
            const Json& rule = i < rules.size() ? rules[i] : empty;
            sc_from[i] = rule["from"].str();
            sc_to[i] = rule["to"].str();
            sc_apply[i] = index_of(sched_names(), rule["apply"].str());
            for (int d = 0; d < 7; ++d) {
                bool all = !rule["days"].is_array();
                bool on = all;
                for (const auto& x : rule["days"].array()) if (static_cast<int>(x.as_number()) == d) on = true;
                sc_day[i][d] = on;
            }
        }
        sc_outside = index_of(sched_names(), sc["outside"].str());
        loaded = true;
    }

    std::vector<std::string> gp_names() const { std::vector<std::string> v{""}; v.insert(v.end(), names.begin(), names.end()); return v; }
    std::vector<std::string> sched_names() const { std::vector<std::string> v{"", "Quiet", "Balanced", "Performance"}; v.insert(v.end(), names.begin(), names.end()); return v; }
    static int index_of(const std::vector<std::string>& v, const std::string& s) { auto it = std::find(v.begin(), v.end(), s); return it == v.end() ? 0 : static_cast<int>(it - v.begin()); }

    void save_game_map(App& a) {
        Json match = Json::obj();
        auto gn = gp_names();
        std::string summary;
        for (int i = 0; i < kRows; ++i) {
            std::string p = trim(gp_proc[i]);
            if (!p.empty() && gp_prof[i] > 0) { match.set(p, gn[static_cast<size_t>(gp_prof[i])]); summary += p + "=" + gn[static_cast<size_t>(gp_prof[i])] + " "; }
        }
        Json pd = a.read_state("powerd.json");
        Json g = Json::obj();
        g.set("enabled", gp_enabled);
        g.set("poll_s", 6);
        g.set("match", match);
        g.set("default", gp_default > 0 ? Json(gn[static_cast<size_t>(gp_default)]) : Json());
        pd.set("game_profiles", g);
        a.write_state("powerd.json", pd);
        a.log(std::string("[Profiles] game map saved (") + (gp_enabled ? "on" : "off") + "): " + (summary.empty() ? "(empty)" : summary));
        a.toast("Game map saved", Tone::Success);
    }

    void save_schedule(App& a) {
        Json rules = Json::arr();
        auto sn = sched_names();
        for (int i = 0; i < kRows; ++i) {
            std::string f = trim(sc_from[i]), t = trim(sc_to[i]), ap = sn[static_cast<size_t>(sc_apply[i])];
            if (f.empty() || t.empty() || ap.empty()) continue;
            Json rule = Json::obj();
            rule.set("from", f); rule.set("to", t); rule.set("apply", ap);
            Json days = Json::arr();
            int n = 0;
            for (int d = 0; d < 7; ++d) if (sc_day[i][d]) { days.push(d); ++n; }
            if (n > 0 && n < 7) rule.set("days", days);
            rules.push(rule);
        }
        Json pd = a.read_state("powerd.json");
        Json s = Json::obj();
        s.set("enabled", sc_enabled);
        s.set("poll_s", 60);
        s.set("rules", rules);
        s.set("outside", sc_outside > 0 ? Json(sn[static_cast<size_t>(sc_outside)]) : Json());
        pd.set("schedule", s);
        a.write_state("powerd.json", pd);
        a.log(std::string("[Profiles] schedule saved (") + (sc_enabled ? "on" : "off") + "): " + std::to_string(rules.array().size()) + " rule(s)");
        a.toast("Schedule saved", Tone::Success);
    }

    void log_rows(App& a, const std::string& tag, const std::vector<profiles::ResultRow>& rows) {
        for (const auto& r : rows) a.log("[Profiles] " + tag + ": " + r.key + (r.ok ? " ok" : " FAILED - " + r.msg) + (r.ok && !r.msg.empty() ? " (" + r.msg + ")" : ""));
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) reload(a);
        a.page_title("Profiles");
        ui.wrapped("A profile is a named snapshot of the whole power surface - thermal profile, CPU TDP, battery limit, NVIDIA limit, fan curve, auto-switch, keyboard colour. Applying one (or the tweak \"Apply Selected\", or a rollback) first drops an automatic snapshot here, so there is always a known-good state to return to if something misbehaves.", Tone::Muted);

        ui.card_begin("Capture current state");
        ui.input("newname", &new_name, ui.S(240), "profile name");
        ui.same_line();
        if (ui.button("Save as profile", Btn::Success)) {
            std::string n = trim(new_name);
            if (n.empty()) a.message("Name needed", "Type a name for the profile first.");
            else {
                try {
                    Json st = profiles::capture_state(a.user);
                    profiles::save_profile(n, st, a.user);
                    a.log("[Profiles] saved '" + n + "'");
                } catch (const std::exception& e) { a.log(std::string("[Profiles] save failed: ") + e.what()); }
                new_name.clear();
                reload(a);
            }
        }
        ui.label(preview, Tone::Muted, ui.S(12), Font::Mono);
        ui.card_end();

        ui.card_begin("Saved profiles");
        ui.wrapped("A saved profile is a plain JSON file - export one to share a known-good curve/TDP loadout with another owner, or import one someone shared with you.", Tone::Muted);
        if (ui.button("Import profile...", Btn::OutlineInfo)) a.open_file_dialog("prof-import", 0, home_of(a.user), "", ".json");
        std::string picked;
        if (a.file_result("prof-import", &picked)) {
            try { std::string n = profiles::import_profile(picked, "", a.user); a.log("[Profiles] imported '" + n + "' from " + picked); }
            catch (const std::exception& e) { a.message("Import failed", e.what()); }
            reload(a);
        }
        if (a.file_result("prof-export", &picked)) {
            try { std::string dest = profiles::export_profile(export_name, picked, a.user); chown_to_user(dest, a.user); a.log("[Profiles] exported '" + export_name + "' -> " + dest); }
            catch (const std::exception& e) { a.log(std::string("[Profiles] export failed: ") + e.what()); }
        }
        if (names.empty()) ui.label("(no profiles yet)", Tone::Muted);
        for (const auto& n : names) {
            ui.push_id(n);
            ui.label_c(n, Tone::Normal, Font::Bold);
            ui.same_line(ui.S(16));
            if (ui.button("Apply", Btn::Success)) { pending_name = n; pending_gpu = false; ui.open_popup("confirm:prof-apply"); }
            ui.same_line();
            if (ui.button("Apply +GPU", Btn::OutlineWarn)) { pending_name = n; pending_gpu = true; ui.open_popup("confirm:prof-apply"); }
            ui.same_line();
            if (ui.button("Export...", Btn::OutlineInfo)) {
                export_name = n;
                std::string safe;
                for (char c : n) if (std::isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_' || c == ' ') safe += c;
                a.open_file_dialog("prof-export", 1, home_of(a.user), (safe.empty() ? "profile" : safe) + ".tuxthrottle-profile.json", ".json");
            }
            ui.same_line();
            if (ui.button("Delete", Btn::OutlineDanger)) { pending_name = n; ui.open_popup("confirm:prof-del"); }
            ui.pop_id();
        }
        ui.card_end();

        if (a.confirm("prof-apply", "Apply profile", "Apply profile '" + pending_name + "'? A snapshot is taken first so you can roll back." + (pending_gpu ? "\n\nThis will ALSO switch hybrid-graphics mode (needs a logout)." : ""), "Apply")) {
            std::string n = pending_name, user = a.user;
            bool gpu = pending_gpu;
            a.run_task("Applying profile " + n, [this, &a, n, user, gpu](const engine::LogFn&) {
                try {
                    profiles::snapshot(user, "pre-apply-" + n);
                    Json st = profiles::load_profile(n, user);
                    log_rows(a, n, profiles::apply_state(st, user, gpu));
                } catch (const std::exception& e) { a.log("[Profiles] apply '" + n + "' failed: " + e.what()); }
            }, [this, &a] { reload(a); });
        }
        if (a.confirm("prof-del", "Delete profile", "Delete profile '" + pending_name + "'?", "Delete")) {
            profiles::delete_profile(pending_name, a.user);
            a.log("[Profiles] deleted '" + pending_name + "'");
            reload(a);
        }

        ui.card_begin("Snapshots - automatic rollback points");
        if (ui.button("Roll back to the latest snapshot", Btn::OutlineWarn)) { pending_target = "last"; ui.open_popup("confirm:prof-roll"); }
        if (snaps.empty()) ui.label("(no snapshots yet)", Tone::Muted);
        for (const auto& s : snaps) {
            ui.push_id(s.path);
            ui.label_c(s.captured + "   " + s.label, Tone::Normal, Font::Mono);
            ui.same_line(ui.S(16));
            if (ui.button("Roll back", Btn::OutlineWarn)) { pending_target = s.path; ui.open_popup("confirm:prof-roll"); }
            ui.pop_id();
        }
        ui.card_end();
        if (a.confirm("prof-roll", "Roll back", "Restore this saved state? The current state is snapshotted first, so this is itself undoable.", "Roll back")) {
            std::string target = pending_target, user = a.user;
            a.run_task("Rolling back", [this, &a, target, user](const engine::LogFn&) {
                try { log_rows(a, "rollback", profiles::rollback(target, user)); }
                catch (const std::exception& e) { a.log(std::string("[Profiles] rollback failed: ") + e.what()); }
            }, [this, &a] { reload(a); });
        }

        ui.card_begin("Per-game auto-profiles");
        ui.wrapped("When a listed process is running (match on the executable name - for Proton games that's the Windows .exe), the daemon snapshots the current state and applies the chosen profile, then restores it when the game exits. Use \"*\" to match any Feral GameMode session. Needs the \"Fan-curve + AC-switch daemon\" tweak enabled.", Tone::Muted);
        ui.toggle("Per-game auto-profiles enabled", &gp_enabled);
        auto gn = gp_names();
        ui.label("Process (exe name)", Tone::Muted, ui.S(12));
        ui.same_line(ui.S(150));
        ui.label("Profile", Tone::Muted, ui.S(12));
        for (int i = 0; i < kRows; ++i) {
            ui.push_id(i);
            ui.input("proc", &gp_proc[i], ui.S(240));
            ui.same_line();
            ui.combo("prof", &gp_prof[i], gn, ui.S(200));
            ui.pop_id();
        }
        ui.label("When no game runs ->");
        ui.same_line();
        ui.combo("gpdef", &gp_default, gn, ui.S(200));
        ui.same_line();
        ui.label("(blank = roll back the pre-game snapshot)", Tone::Muted);
        if (ui.button("Save game map", Btn::Success)) save_game_map(a);
        ui.card_end();

        ui.card_begin("Time schedule");
        ui.wrapped("The daemon applies a profile by time of day - e.g. Quiet 22:00-07:00. \"Apply\" is a preset (Quiet / Balanced / Performance) or a saved profile name; times are 24-hour and may wrap past midnight. Tick the weekdays a rule runs on (all ticked = every day). A running per-game profile wins. Needs the \"Fan-curve + AC-switch daemon\" tweak enabled.", Tone::Muted);
        ui.toggle("Time schedule enabled", &sc_enabled);
        auto sn = sched_names();
        ui.label("From", Tone::Muted, ui.S(12));
        ui.same_line(ui.S(70));
        ui.label("To", Tone::Muted, ui.S(12));
        ui.same_line(ui.S(70));
        ui.label("Apply", Tone::Muted, ui.S(12));
        const char* dn[7] = {"M", "T", "W", "T", "F", "S", "S"};
        for (int i = 0; i < kRows; ++i) {
            ui.push_id(1000 + i);
            ui.input("from", &sc_from[i], ui.S(84), "22:00");
            ui.same_line();
            ui.input("to", &sc_to[i], ui.S(84), "07:00");
            ui.same_line();
            ui.combo("apply", &sc_apply[i], sn, ui.S(170));
            for (int d = 0; d < 7; ++d) {
                ui.same_line(ui.S(6));
                ui.push_id(d);
                ui.checkbox(dn[d], &sc_day[i][d]);
                ui.pop_id();
            }
            ui.pop_id();
        }
        ui.label("Outside every rule ->");
        ui.same_line();
        ui.combo("outside", &sc_outside, sn, ui.S(200));
        ui.same_line();
        ui.label("(blank = leave the profile alone)", Tone::Muted);
        if (ui.button("Save schedule", Btn::Success)) save_schedule(a);
        ui.card_end();
    }
};
}  // namespace

std::unique_ptr<Page> make_profiles() { return std::make_unique<Profiles>(); }

}  // namespace tt::app
