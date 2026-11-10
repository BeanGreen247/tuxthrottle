// Setup Games: per-game walkthroughs driven by config/games.json.
#include <algorithm>
#include <memory>
#include <mutex>

#include "../engine.hpp"
#include "../gamecfg.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Shared {
    std::mutex m;
    std::map<std::string, std::string> state;   // "<gid>/<idx>" -> done todo manual ready unknown hidden
    std::map<std::string, std::string> pdb;     // appid -> label
    std::atomic<bool> inflight{false};
};

struct Games : Page {
    Games() { title = "Setup Games"; group = "Games"; }
    Json games;
    std::vector<std::string> ids;
    std::shared_ptr<Shared> sh = std::make_shared<Shared>();
    int sel = 0;
    std::map<std::string, bool> manual_done;
    bool loaded = false, was_busy = false;
    std::string pending_gid;

    void enter(App& a) override {
        if (!loaded) {
            games = read_json_file(engine::config_dir() + "/games.json");
            for (const auto& kv : games.object()) ids.push_back(kv.first);
            std::stable_sort(ids.begin(), ids.end(), [&](const std::string& x, const std::string& y) { return games[x]["order"].as_number(99) < games[y]["order"].as_number(99); });
            loaded = true;
        }
        refresh(a);
    }

    std::string subst(App& a, const std::string& gid, const std::string& s) {
        std::string c = replace_all(s, "{APPID}", games[gid]["appid"].str());
        return engine::substitute(c, a.user);
    }

    void refresh(App& a) {
        if (sh->inflight) return;
        sh->inflight = true;
        struct Job_ { std::string gid; size_t idx; std::string check, show_if, key; bool manual; };
        std::vector<Job_> jobs;
        for (const auto& gid : ids) {
            const auto& steps = games[gid]["steps"].array();
            for (size_t i = 0; i < steps.size(); ++i) {
                const Json& st = steps[i];
                jobs.push_back({gid, i, st["check"].is_string() ? subst(a, gid, st["check"].str()) : "", st["show_if"].is_string() ? subst(a, gid, st["show_if"].str()) : "", gid + "/" + std::to_string(i), st["manual"].as_bool() && !st["run"].is_string()});
            }
        }
        auto s = sh;
        a.run_bg([s, jobs] {
            for (const auto& j : jobs) {
                std::string state;
                if (!j.show_if.empty() && run_argv({"bash", "-c", j.show_if}, 25000).code != 0) state = "hidden";
                else if (j.check.empty()) state = j.manual ? "manual" : "ready";
                else state = run_argv({"bash", "-c", j.check}, 25000).code == 0 ? "done" : "todo";
                std::lock_guard<std::mutex> g(s->m);
                s->state[j.key] = state;
            }
            s->inflight = false;
        });
    }

    void pdb_fetch(App& a, const std::string& appid) {
        {
            std::lock_guard<std::mutex> g(sh->m);
            if (sh->pdb.count(appid)) return;
            sh->pdb[appid] = "";
        }
        auto s = sh;
        a.run_bg([s, appid] {
            std::string t;
            try { auto d = protondb::lookup(appid); if (d) t = protondb::label(*d); } catch (...) {}
            std::lock_guard<std::mutex> g(s->m);
            s->pdb[appid] = t;
        });
    }

    void run_all(App& a, const std::string& gid) {
        std::string name = games[gid]["Content"].str(gid);
        struct S { std::string title, check, run; };
        std::vector<S> autos;
        std::vector<std::string> manual;
        for (const auto& st : games[gid]["steps"].array()) {
            if (st["run"].is_string()) autos.push_back({st["title"].str(st["id"].str()), st["check"].is_string() ? subst(a, gid, st["check"].str()) : "", subst(a, gid, st["run"].str())});
            else if (st["manual"].as_bool()) manual.push_back(st["title"].str(st["id"].str()));
        }
        a.run_task("Setup Games - " + name + ": all automatic steps", [&a, autos, manual](const engine::LogFn& lg) {
            std::string failed;
            for (const auto& s : autos) {
                if (!s.check.empty() && run_argv({"bash", "-c", s.check}, 30000).code == 0) { lg("[Setup Games] " + s.title + " - already done, skipping"); continue; }
                lg("[Setup Games] " + s.title + " ...");
                int rc = run_shell_stream(s.run, lg);
                if (rc == 0) { lg("[Setup Games] " + s.title + " - done"); }
                else { lg("[Setup Games] " + s.title + " - FAILED, stopping the run"); failed = s.title; break; }
            }
            if (failed.empty()) lg("=== " + std::to_string(autos.size()) + " automatic step(s) done." + (manual.empty() ? "" : "  Now do the manual steps: " + join(manual, "; ")) + " ===");
            (void)a;
        });
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Per-game setup walkthroughs", "Pick a game from the tabs below, then work down the steps. Steps with a Run button do the work (output streams to the log console at the bottom of the window); manual steps are quick clicks inside the game launcher that can't be scripted - tick \"Mark done\" once you've done them.  Proton-prefix, save-file, shader-cache and launch-option tools are on the \"Game Tools\" tab.");
        if (was_busy && !a.busy()) refresh(a);
        was_busy = a.busy();
        if (ids.empty()) { ui.label("No games configured (config/games.json).", Tone::Muted); return; }
        if (sh->inflight) ui.wake_after(500);

        ui.flow_reset();
        for (size_t i = 0; i < ids.size(); ++i) {
            const Json& g = games[ids[i]];
            std::string t = g["Tab"].str(g["Content"].str(ids[i]));
            ui.push_id(static_cast<int>(i));
            if (ui.button_flow(t, static_cast<int>(i) == sel ? Btn::Primary : Btn::Secondary)) sel = static_cast<int>(i);
            ui.pop_id();
        }
        ui.flow_reset();
        ui.spacing(ui.S(6));
        if (sel >= static_cast<int>(ids.size())) sel = 0;
        const std::string gid = ids[static_cast<size_t>(sel)];
        const Json& game = games[gid];
        if (!game["Description"].str().empty()) ui.wrapped(game["Description"].str(), Tone::Muted);
        std::string appid = game["appid"].str();
        if (!appid.empty() && std::all_of(appid.begin(), appid.end(), ::isdigit)) {
            pdb_fetch(a, appid);
            std::lock_guard<std::mutex> g(sh->m);
            if (!sh->pdb[appid].empty()) ui.label(sh->pdb[appid], Tone::Muted, ui.S(12));
            else ui.wake_after(500);
        }
        const auto& steps = game["steps"].array();
        int n_auto = 0, n_manual = 0;
        for (const auto& s : steps) { if (s["run"].is_string()) ++n_auto; else if (s["manual"].as_bool()) ++n_manual; }
        if (n_auto) {
            ui.card_begin("");
            if (ui.button("Run all " + std::to_string(n_auto) + " automatic steps", Btn::Success, 0, !a.busy())) { pending_gid = gid; ui.open_popup("confirm:games-all"); }
            ui.same_line();
            ui.label_c("Runs the Run-step actions below in order, skipping any already done. " + std::to_string(n_manual) + " manual step(s) after still need doing by hand.", Tone::Muted);
            ui.card_end();
        }
        if (a.confirm("games-all", "Run all automatic steps", [&] {
                std::string name = games[pending_gid]["Content"].str(pending_gid), lines;
                int n = 0;
                for (const auto& s : games[pending_gid]["steps"].array()) if (s["run"].is_string()) { lines += "  " + s["title"].str(s["id"].str()) + "\n"; ++n; }
                return name + ": run these " + std::to_string(n) + " steps in order?\n\n" + lines + "\nSteps already done are skipped. Steam may open during the BattlEye step.";
            }(), "Run all"))
            run_all(a, pending_gid);

        for (size_t i = 0; i < steps.size(); ++i) {
            const Json& st = steps[i];
            std::string key = gid + "/" + std::to_string(i), state;
            { std::lock_guard<std::mutex> g(sh->m); auto it = sh->state.find(key); state = it == sh->state.end() ? "" : it->second; }
            if (state == "hidden") continue;
            bool manual_only = st["manual"].as_bool() && !st["run"].is_string();
            if (manual_only && manual_done[key] && state == "manual") state = "manual-done";
            ui.push_id(key);
            ui.card_begin("");
            ui.label(st["title"].str(st["id"].str()), Tone::Normal, ui.S(14), Font::Bold);
            const char* txt = state == "done" || state == "manual-done" ? "done" : state == "todo" ? "to do" : state == "manual" ? "manual" : state == "ready" ? "optional" : state == "unknown" ? "check err" : "...";
            Tone tone = state == "done" || state == "manual-done" ? Tone::Success : state == "todo" ? Tone::Warning : state == "manual" ? Tone::Info : state == "unknown" ? Tone::Danger : Tone::Muted;
            ui.text_at(ui.region().right() - ui.text_w(txt, ui.S(12), Font::Bold) - ui.S(24), ui.last_item().y, txt, ui.tone_color(tone), ui.S(12), Font::Bold);
            if (!st["desc"].str().empty()) ui.wrapped(st["desc"].str(), Tone::Muted);
            bool done = state == "done" || state == "manual-done";
            if (st["run"].is_string()) {
                if (ui.button(done ? "Already done" : "Run step", Btn::Success, 0, !done && !a.busy())) {
                    std::string cmd = subst(a, gid, st["run"].str());
                    a.run_shell_task(st["title"].str(st["id"].str()), cmd);
                }
            }
            std::string copy = st["copy"].is_string() ? subst(a, gid, st["copy"].str()) : "";
            if (!copy.empty()) {
                if (st["run"].is_string()) ui.same_line();
                if (ui.button("Copy command", Btn::OutlineInfo)) a.copy_text(copy, "command");
            }
            if (manual_only) {
                ui.same_line();
                bool v = manual_done[key];
                if (ui.toggle("Mark done", &v)) manual_done[key] = v;
            }
            if (!copy.empty()) ui.label("  " + copy, Tone::Muted, ui.S(12), Font::Mono);
            ui.card_end();
            ui.pop_id();
        }
    }
};
}  // namespace

std::unique_ptr<Page> make_games() { return std::make_unique<Games>(); }

}  // namespace tt::app
