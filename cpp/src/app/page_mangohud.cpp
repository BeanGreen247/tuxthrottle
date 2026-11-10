// MangoHud tab: session-wide switch, live per-game gate, per-game switches/profiles, and the overlay editor.
#include <algorithm>
#include <filesystem>
#include <memory>
#include <mutex>
#include <regex>

#include "../formats.hpp"
#include "../gamecfg.hpp"
#include "../mangohud.hpp"
#include "../mhedit.hpp"
#include "../sensors.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;
namespace fs = std::filesystem;

namespace {

struct Pending { bool on = false, force = false; bool has_settings = false; bool own = false; std::string font_size, fps_limit; };

struct Mango : Page {
    Mango() { title = "MangoHud"; group = "Games"; }

    // ---- loading ----
    bool loaded = false, was_busy = false;
    bool pending_load = false;
    std::string list_err, global_lbl = "checking...", gate_lbl = "checking...";
    Tone global_tone = Tone::Muted, gate_tone = Tone::Muted;
    bool gate = false;
    std::vector<Json> games;
    std::map<std::string, Pending> pend;
    std::map<std::string, bool> open_panel;
    std::map<std::string, std::string> row_font, row_fps;
    std::string filter;
    int src_i = 0;   // 0 all, 1 Steam, 2 Heroic, 3 Lutris
    bool only_on = false;
    std::shared_ptr<std::pair<std::mutex, std::string>> out = std::make_shared<std::pair<std::mutex, std::string>>();

    // ---- editor ----
    mhedit::Editor ed;
    std::string profile_appid, profile_name, exe_name;
    std::vector<std::string> gpu_rows;
    bool editor_loaded = false, detecting = false;
    int pos_i = 0;
    // placement dialog
    bool pos_open = false;
    double pbx = 0, pby = 0, pdx = 0, pdy = 0;
    bool pdrag = false, psnap = true;
    std::string saved_pos;
    int saved_ox = 0, saved_oy = 0;
    bool status_line = false;

    void enter(App& a) override { if (!loaded) { loaded = true; load_editor(a); } reload(a); }

    // ---------------------------------------------------------------- list loading
    void reload(App& a) {
        pending_load = true;
        list_err.clear();
        a.job("mhg-list").start_shell(a.user_ctl("mangohud-games list --json") + " ; echo '@@GLOBAL@@' ; " + a.user_ctl("mangohud-games global-status"));
    }

    void poll_load(App& a) {
        if (!pending_load) return;
        Job& j = a.job("mhg-list");
        if (j.busy()) { a.ui.wake_after(300); return; }
        pending_load = false;
        auto lines = j.snapshot();
        std::string json_txt;
        std::vector<std::string> glob;
        bool after = false;
        for (const auto& l : lines) {
            if (l == "@@GLOBAL@@") { after = true; continue; }
            if (after) { if (!l.empty() && l[0] == '/') glob.push_back(l); }
            else json_txt += l + "\n";
        }
        games.clear();
        try {
            size_t b = json_txt.find('['), e = json_txt.rfind(']');
            Json rows = Json::parse(json_txt.substr(b, e - b + 1));
            for (const auto& r : rows.array()) games.push_back(r);
        } catch (...) { list_err = "could not read the Steam library"; }
        if (!glob.empty()) { global_lbl = "ON for every game via " + join(glob, ", "); global_tone = Tone::Danger; }
        else { global_lbl = "off - the overlay only shows in games enabled below"; global_tone = Tone::Success; }
        gate = mangohud::gate_installed();
        bool cur = mangohud::gate_current();
        gate_lbl = gate && !cur ? "installed, but an older version - press Install to update it (the dedicated-GPU switch on Game Tools needs the new one)" : gate ? "installed - changes apply live, Steam can stay open" : "not installed - every change needs Steam closed";
        gate_tone = (!gate || !cur) ? Tone::Warning : Tone::Success;
    }

    void run(App& a, const std::string& desc, const std::string& args) {
        a.run_user_ctl_task("MangoHud", desc, "mangohud-games " + args);
        want_reload = true;
    }
    bool want_reload = false;

    // ---------------------------------------------------------------- editor config files
    std::string user_home(App& a) { return home_of(a.user); }
    std::string conf_path(App& a) {
        std::string base = user_home(a) + "/.config/MangoHud";
        if (!profile_appid.empty()) return base + "/tuxthrottle-" + profile_appid + ".conf";
        static const std::regex exe(R"(\.exe$)", std::regex::icase);
        std::string g = trim(std::regex_replace(trim(exe_name), exe, ""));
        return base + "/" + (g.empty() ? "MangoHud.conf" : g + ".conf");
    }
    std::string read_path(App& a) {
        std::string p = conf_path(a);
        if (path_exists(p) || profile_appid.empty()) return p;
        std::string off = p + ".off";
        return path_exists(off) ? off : fs::path(p).parent_path().string() + "/MangoHud.conf";
    }

    void load_editor(App& a) {
        auto conf = mhedit::parse_conf(read_file(read_path(a)).value_or(""));
        ed.cpu = conf.cpu;
        auto devs = sensors::gpu_devices();
        int count = static_cast<int>(std::max<size_t>({devs.size(), conf.gpus.size(), 1}));
        std::vector<std::string> det;
        ed.pcis.clear();
        for (const auto& d : devs) { det.push_back(d.name); ed.pcis.push_back(d.pci); }
        gpu_rows.assign(static_cast<size_t>(count), "");
        const auto& pre = conf.gpus.empty() ? det : conf.gpus;
        for (size_t i = 0; i < gpu_rows.size() && i < pre.size(); ++i) gpu_rows[i] = pre[i];
        remap_pci(count);
        ed.pos = conf.position.empty() ? "top-left" : conf.position;
        ed.ox = conf.offset_x; ed.oy = conf.offset_y; ed.pos_set = !conf.position.empty();
        ed.font_size = conf.font_size;
        ed.full_cpu = mhedit::level_of(conf.elements, "cpu") == "full";
        ed.full_gpu = mhedit::level_of(conf.elements, "gpu") == "full";
        ed.full_mem = mhedit::level_of(conf.elements, "mem") == "full";
        ed.graph = conf.elements.count("frame_timing") || conf.elements.count("frametime");
        ed.gpuname = !conf.off.count("gpu_name");
        ed.gamemode = conf.elements.count("gamemode") > 0;
        ed.core_clock = conf.elements.count("gpu_core_clock") > 0;
        ed.mem_clock = conf.elements.count("gpu_mem_clock") > 0;
        status_line = mangohud::status_line::is_enabled(a.user);
        editor_loaded = true;
        if (conf.cpu.empty() && conf.gpus.empty() && profile_appid.empty()) detect(a);
    }

    void remap_pci(int count) {
        auto matched = sensors::gpu_label_pci_map(gpu_rows);
        std::vector<std::string> pci;
        for (int i = 0; i < count; ++i) {
            size_t k = static_cast<size_t>(i);
            pci.push_back(k < matched.size() && !matched[k].empty() ? matched[k] : k < ed.pcis.size() ? ed.pcis[k] : "");
        }
        ed.pcis = pci;
    }

    std::shared_ptr<std::pair<std::mutex, std::pair<std::string, std::vector<sensors::GpuDevice>>>> det_res = std::make_shared<std::pair<std::mutex, std::pair<std::string, std::vector<sensors::GpuDevice>>>>();
    bool det_ready = false;
    void detect(App& a) {
        ed.cpu = "detecting...";
        for (auto& g : gpu_rows) g = "detecting...";
        detecting = true;
        det_ready = false;
        auto r = det_res;
        a.run_bg([r] {
            auto cpu = sensors::cpu_model_name();
            auto devs = sensors::gpu_devices();
            std::lock_guard<std::mutex> g(r->first);
            r->second = {cpu, devs};
        });
        a.ui.wake_after(300);
    }
    void poll_detect(App& a) {
        if (!detecting) return;
        std::lock_guard<std::mutex> g(det_res->first);
        if (det_res->second.first.empty() && det_res->second.second.empty()) { a.ui.wake_after(300); return; }
        ed.cpu = det_res->second.first;
        auto& devs = det_res->second.second;
        ed.pcis.clear();
        gpu_rows.clear();
        for (const auto& d : devs) { gpu_rows.push_back(d.name); ed.pcis.push_back(d.pci); }
        if (gpu_rows.empty()) gpu_rows.push_back("");
        det_res->second = {};
        detecting = false;
    }

    // ---------------------------------------------------------------- writing
    bool guard_preload(App& a) {
        auto confs = mangohud::global_confs();
        if (confs.empty()) return true;
        preload_confs = confs;
        a.ui.open_popup("confirm:mh-preload");
        return false;
    }
    std::vector<std::string> preload_confs;

    bool write_conf(App& a, const mhedit::Managed& m) {
        std::string p = conf_path(a);
        std::string existing = read_file(read_path(a)).value_or("");
        std::string text = mhedit::merge_conf(existing, m);
        mkdir_p(fs::path(p).parent_path().string());
        chown_to_user(fs::path(p).parent_path().string(), a.user);
        std::string tmp = p + ".tuxthrottle-tmp";
        if (!write_file(tmp, text)) { a.log("[MangoHud] write failed: " + p); return false; }
        chown_to_user(tmp, a.user);
        std::error_code ec;
        fs::rename(tmp, p, ec);
        if (ec) { a.log("[MangoHud] write failed: " + ec.message()); return false; }
        if (!profile_appid.empty()) fs::remove(p + ".off", ec);
        return true;
    }

    void apply_editor(App& a) {
        if (!guard_preload(a)) return;
        ed.gpus = gpu_rows;
        ed.mangohud_gpu_order = sensors::mangohud_gpu_order();
        auto m = mhedit::build_managed(ed);
        if (write_conf(a, m)) a.log("[MangoHud] " + conf_path(a) + "  (cpu=" + (trim(ed.cpu).empty() ? "-" : trim(ed.cpu)) + ", deduped)");
        a.toast("MangoHud config written", Tone::Success);
    }

    void reset_editor(App& a) {
        if (!guard_preload(a)) return;
        std::string p = conf_path(a);
        std::error_code ec;
        if (fs::is_regular_file(p, ec)) { write_file(p + ".bak", read_file(p).value_or("")); chown_to_user(p + ".bak", a.user); fs::remove(p, ec); }
        mkdir_p(fs::path(p).parent_path().string());
        write_file(p, mhedit::clean_baseline());
        chown_to_user(p, a.user);
        apply_editor(a);
        a.log("[MangoHud] reset to a clean config -> " + p + " (.bak kept)");
    }

    void write_position(App& a) {
        if (!guard_preload(a)) return;
        mhedit::Managed m;
        mhedit::set(m, "position", mhedit::Val::value(ed.pos));
        mhedit::set(m, "offset_x", ed.ox ? mhedit::Val::value(std::to_string(ed.ox)) : mhedit::Val::remove());
        mhedit::set(m, "offset_y", ed.oy ? mhedit::Val::value(std::to_string(ed.oy)) : mhedit::Val::remove());
        if (write_conf(a, m)) a.log("[MangoHud] position -> " + conf_path(a) + "  (" + ed.pos + ", offset " + std::to_string(ed.ox) + "," + std::to_string(ed.oy) + "; deduped)");
    }

    void select_profile(App& a, const std::string& appid, const std::string& name) {
        profile_appid = appid;
        profile_name = name;
        load_editor(a);
        a.ui.scroll_to("page", 1e9f);
    }

    // ---------------------------------------------------------------- pending plan
    void apply_pending(App& a) {
        Json on = Json::obj(), off = Json::arr(), force = Json::arr();
        for (auto& [id, p] : pend) {
            if (p.on) {
                Json s;
                if (p.has_settings) { s = Json::obj(); s.set("own", p.own); s.set("font_size", p.font_size); s.set("fps_limit", p.fps_limit); }
                on.set(id, s);
                if (p.force) force.push(id);
            } else off.push(id);
        }
        Json plan = Json::obj();
        plan.set("on", on); plan.set("off", off); plan.set("force", force);
        std::string blob = base64_encode(plan.dump());
        size_t n = pend.size();
        pend.clear();
        run(a, "apply " + std::to_string(n) + " game change(s)", "apply --plan " + blob);
    }

    // ---------------------------------------------------------------- drawing
    void draw(App& a) override {
        Ui& ui = a.ui;
        poll_load(a);
        poll_detect(a);
        if (was_busy && !a.busy()) reload(a);
        was_busy = a.busy();
        want_reload = false;
        bool en = !a.busy();
        a.page_title("MangoHud", "MangoHud is opt-in per game: nothing shows the overlay unless you switch it on for that game below. The session-wide enable (MANGOHUD=1 for every Vulkan game) stays off.");

        ui.card_begin("Session-wide overlay");
        ui.label_c(global_lbl, global_tone);
        ui.same_line(ui.S(12));
        if (ui.button("Turn off session-wide MangoHud", Btn::OutlineWarn, 0, en)) run(a, "session-wide off", "global-off");
        ui.tooltip("Remove MANGOHUD=1 (and any libMangoHud LD_PRELOAD) from your ~/.config/environment.d files, so only games you enable below show the overlay. Takes full effect at next login.");
        ui.card_end();

        ui.card_begin("Live per-game switch");
        ui.wrapped("A gate at /usr/local/bin/mangohud that only loads the overlay for games switched on below. With it a switch applies at the game's next launch and Steam stays open; without it every change rewrites Steam's Launch Options.", Tone::Muted);
        ui.label_c(gate_lbl, gate_tone);
        ui.flow_reset();
        if (ui.button_flow("Install", Btn::OutlineSuccess, en)) {
            std::string gp = "/usr/local/bin/mangohud";
            a.run_shell_task("MangoHud - install live per-game switch", shell_quote(ctl_exe()) + " mangohud-games gate-script > " + gp + ".new && chmod 755 " + gp + ".new && mv -f " + gp + ".new " + gp + " && echo 'gate installed'");
        }
        if (ui.button_flow("Remove", Btn::Outline, en)) a.run_shell_task("MangoHud - remove live per-game switch", "if grep -q tuxthrottle-mangohud-gate /usr/local/bin/mangohud 2>/dev/null; then rm -f /usr/local/bin/mangohud && echo 'gate removed'; else echo 'no TuxThrottle gate at /usr/local/bin/mangohud'; fi");
        if (ui.button_flow("Add the hook to every game (one-time)", Btn::OutlineInfo, en)) {
            if (!gate) a.message("Install the live switch first", "Without the gate, `mangohud` in a game's Launch Options turns the overlay on. Press Install first.");
            else ui.open_popup("confirm:mh-hook");
        }
        ui.tooltip("Put `mangohud` into every game's Launch Options once. Behind the gate it does nothing until a game is switched on, and afterwards no switch ever needs Steam closed again. This one write does need Steam closed; every localconfig.vdf is backed up.");
        ui.card_end();
        if (a.confirm("mh-hook", "Add the hook to every game", "Add `mangohud` to every Steam game's Launch Options?\n\nBehind the gate it stays off until you switch a game on, and from then on switches apply without closing Steam. Steam must be closed for this one write; every localconfig.vdf is backed up.", "Add hook")) run(a, "add the hook to every game", "hook-all");

        // ---- per game
        ui.card_begin("Per-game MangoHud");
        ui.wrapped("Every installed game from Steam, Heroic and Lutris. Flip as many switches as you like, then press Apply changes once. Settings opens a game's own profile, which overrides the global layout while it is on. A game with an anti-cheat is locked until you tick force on. \"No hook\" means Apply has to add `mangohud` to that game's Launch Options, which needs Steam closed once.", Tone::Muted);
        ui.label_c("Filter:");
        ui.same_line();
        ui.input("mhf", &filter, ui.S(220));
        ui.same_line(ui.S(12));
        ui.toggle("only games with MangoHud on", &only_on);
        ui.label_c("Source:", Tone::Muted);
        ui.same_line();
        ui.segmented("src", &src_i, {"All", "Steam", "Heroic", "Lutris"});
        ui.flow_reset();
        if (ui.button_flow("Reload", Btn::OutlineInfo, en)) reload(a);
        ui.tooltip("Re-read the installed games of every launcher and rescan for anti-cheat.");
        if (ui.button_flow("Remove MangoHud from every game", Btn::OutlineDanger, en)) ui.open_popup("confirm:mh-disable-all");
        ui.flow_reset();
        size_t n = pend.size();
        if (ui.button(n ? "Apply changes (" + std::to_string(n) + ")" : "Apply changes", Btn::Success, 0, n > 0 && en)) apply_pending(a);
        ui.tooltip("Apply every switch and settings change you made below in one go. Flip as many games as you like first - nothing is written until you press this.");
        ui.same_line();
        if (ui.button("Discard", Btn::Outline, 0, n > 0)) pend.clear();
        if (a.confirm("mh-disable-all", "Remove MangoHud from every game", std::string("Switch the MangoHud overlay off in every Steam game?") + (gate ? "" : "\n\nThe live switch isn't installed, so this strips the `mangohud` wrapper from every game's Launch Options - Steam must be closed first. Every localconfig.vdf is backed up."), "Remove")) run(a, "remove from every game", "disable-all");

        std::string needle = lower(trim(filter));
        std::vector<const Json*> shown;
        int on = 0, locked = 0;
        std::set<std::string> srcs;
        for (const auto& g : games) {
            if (g["enabled"].as_bool()) ++on;
            if (!g["anticheat"].str().empty()) ++locked;
            std::string s = g["source"].str("Steam");
            srcs.insert(s.substr(0, s.find(" \xC2\xB7 ")));
            if (!needle.empty() && lower(g["name"].str()).find(needle) == std::string::npos && needle != g["appid"].str() && lower(g["source"].str()).find(needle) == std::string::npos && lower(g["anticheat"].str()).find(needle) == std::string::npos) continue;
            if (only_on && !g["wanted"].as_bool()) continue;
            if (src_i > 0) { static const char* names[] = {"", "Steam", "Heroic", "Lutris"}; if (g["source"].str("Steam").rfind(names[src_i], 0) != 0) continue; }
            shown.push_back(&g);
        }
        std::string msg = std::to_string(games.size()) + " installed games (" + (srcs.empty() ? "none found" : join(std::vector<std::string>(srcs.begin(), srcs.end()), ", ")) + "), MangoHud on in " + std::to_string(on) + ", " + std::to_string(locked) + " locked by anti-cheat" + (shown.size() > 60 ? " - showing the first 60 of " + std::to_string(shown.size()) + "; type in the filter to narrow down" : "");
        if (!list_err.empty()) msg = list_err;
        ui.label(msg, Tone::Muted);
        int cnt = 0;
        for (const Json* gp : shown) {
            if (++cnt > 60) break;
            row(a, *gp, en);
        }
        ui.card_end();

        editor(a, en);
        placement_dialog(a);

        if (a.confirm("mh-preload", "MangoHud is loaded into your whole desktop session",
                      "These files force MangoHud into every process with LD_PRELOAD, including KWin and Plasma:\n\n" + join(preload_confs, "\n") + "\n\nRewriting the overlay config while that is active can crash the desktop - it comes back with no power/battery tray.\n\nRemove the global LD_PRELOAD line now (games you enabled on this tab keep the overlay), then log out and back in?", "Fix it")) {
            run(a, "remove global LD_PRELOAD", "global-off");
            a.message("Log out to finish", "Global MangoHud LD_PRELOAD removed. Log out and back in so KWin / Plasma restart without the overlay layer, then the config is safe to edit.");
        }
    }

    void row(App& a, const Json& g, bool en) {
        Ui& ui = a.ui;
        std::string id = g["id"].str();
        ui.push_id(id);
        std::string ac = g["anticheat"].str();
        Pending* p = pend.count(id) ? &pend[id] : nullptr;
        bool want = p ? p->on : g["wanted"].as_bool();
        bool force = p ? p->force : g["forced"].as_bool();
        bool locked = !ac.empty() && !force;
        auto mark = [&](bool settings) {
            Pending& q = pend[id];
            if (!pend.count(id)) q.on = want;
            q.on = want; q.force = force;
            if (settings) { q.on = true; q.has_settings = true; q.own = own_of(g, id); q.font_size = row_font[id]; q.fps_limit = row_fps[id]; }
            a.ui.request_redraw();
        };
        ui.flow_reset();
        if (ui.toggle("", &want, !locked && en)) mark(false);
        ui.same_line();
        ui.label_c(ui.g.ellipsize(g["name"].str(), ui.S(300), ui.base_size(), Font::Regular), Tone::Normal, Font::Regular, ui.S(300));
        ui.same_line();
        ui.label_c(g["source"].str("Steam") + " \xC2\xB7 " + g["appid"].str(), Tone::Muted, Font::Regular, ui.S(200));
        bool steam = id.rfind("steam:", 0) == 0;
        if (steam) { ui.same_line(); if (ui.button(open_panel[id] ? "Settings ^" : "Settings v", Btn::Ghost)) open_panel[id] = !open_panel[id]; }
        if (!ac.empty()) {
            ui.same_line();
            ui.label_c("! " + ac, Tone::Danger);
            ui.same_line();
            bool f = force;
            if (ui.toggle("force on", &f)) { force = f; if (!f) want = false; mark(false); }
            ui.tooltip(ac + " was found in this game's folder. An overlay injected into an anti-cheat game can get the account flagged or banned, so the switch is locked. Tick this to unlock it anyway - at your own risk.");
        }
        std::string prof = g["profile"].str();
        if (prof == "on") { ui.same_line(); ui.label_c("own profile", Tone::Info); }
        else if (prof == "off") { ui.same_line(); ui.label_c("profile off", Tone::Muted); }
        if (steam) {
            if (g["wanted"].as_bool() && !g["hook"].as_bool()) { ui.same_line(); ui.label_c("waiting for hook - close Steam once, then Apply", Tone::Warning); }
            else if (!g["hook"].as_bool()) { ui.same_line(); ui.label_c("no hook", Tone::Muted); }
        }
        if (pend.count(id)) { ui.same_line(); ui.label_c("pending", Tone::Warning); }
        if (steam && open_panel[id]) {
            ui.indent(ui.S(34));
            bool own = own_of(g, id);
            if (ui.toggle("use this game's own profile (overrides the global one)", &own)) { own_flag[id] = own; mark(true); }
            std::string state = prof == "on" ? "profile in force" : prof == "off" ? "profile saved, switched off" : "no profile yet - starts as a copy of the global one";
            ui.same_line();
            ui.label_c("AppID " + g["appid"].str() + "  \xC2\xB7  " + state, Tone::Muted);
            if (ui.button("Edit layout & placement", Btn::OutlineInfo)) {
                own_flag[id] = true;
                want = true;
                mark(true);
                select_profile(a, g["appid"].str(), g["name"].str());
                a.log("[MangoHud] editing the profile of " + g["name"].str() + " (AppID " + g["appid"].str() + ") - set it up in the overlay editor and press Write, then Apply changes");
            }
            if (!row_font.count(id)) { row_font[id] = g["settings"]["font_size"].str(); row_fps[id] = g["settings"]["fps_limit"].str(); }
            ui.same_line();
            ui.label_c("Font size:");
            ui.same_line();
            if (ui.input("fs", &row_font[id], ui.S(70))) mark(true);
            ui.same_line();
            ui.label_c("FPS limit:");
            ui.same_line();
            if (ui.input("fl", &row_fps[id], ui.S(70))) mark(true);
            ui.wrapped("Launch options now: " + (g["options"].str().empty() ? "-" : g["options"].str()), Tone::Muted, ui.avail_w(), ui.S(12));
            ui.unindent(ui.S(34));
        }
        ui.pop_id();
    }
    std::map<std::string, bool> own_flag;
    bool own_of(const Json& g, const std::string& id) {
        if (own_flag.count(id)) return own_flag[id];
        auto it = pend.find(id);
        if (it != pend.end() && it->second.has_settings) return it->second.own;
        return g["own_settings"].as_bool();
    }

    // ---------------------------------------------------------------- overlay editor
    void editor(App& a, bool en) {
        Ui& ui = a.ui;
        (void)en;
        ui.card_begin("MangoHud overlay");
        ui.wrapped("The overlay's CPU / GPU names, position and detail level. Names load from the config if set, else auto-detect from the hardware. \"Show in full\" toggles each group between load-% only and load + temp + power (+ VRAM for memory); the extra switches add the frametime graph and GPU clocks. FPS, the graphics-API line and each GPU's real name (gpu_name) always stay; on Write, everything else in the stat section is stripped. `width` is pinned to fit your longest name; other config lines (styling, keybinds) are left alone.", Tone::Muted);
        ui.label_c("CPU name:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        ui.input("cpuname", &ed.cpu, ui.S(420));
        bool multi = gpu_rows.size() > 1;
        for (size_t i = 0; i < gpu_rows.size(); ++i) {
            ui.push_id(static_cast<int>(i));
            ui.label_c(multi ? "GPU " + std::to_string(i) + " name:" : "GPU name:", Tone::Normal, Font::Regular, ui.S(110));
            ui.same_line();
            ui.input("g", &gpu_rows[i], ui.S(420));
            if (multi) {
                ui.same_line();
                if (ui.button("swap", Btn::Outline)) {
                    size_t j = (i + 1) % gpu_rows.size();
                    std::swap(gpu_rows[i], gpu_rows[j]);
                    if (i < ed.pcis.size() && j < ed.pcis.size()) std::swap(ed.pcis[i], ed.pcis[j]);
                    a.log("[MangoHud] swapped GPU rows " + std::to_string(i) + " <-> " + std::to_string(j) + " - press \"Write to MangoHud config\" to save");
                }
                ui.tooltip("Swap this GPU's name (and its slot in `gpu_list`) with the next row - reorder if MangoHud has them backwards. Press Write after.");
            }
            ui.same_line();
            ui.label_c(i < ed.pcis.size() && !ed.pcis[i].empty() ? "[" + ed.pcis[i] + "]  name label + gpu_list slot" : "name label + gpu_list slot for this card", Tone::Muted, Font::Regular);
            ui.pop_id();
        }
        ui.label_c("Position:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        ui.label_c(ed.pos + ((ed.ox || ed.oy) ? "   (nudge " + std::to_string(ed.ox) + ", " + std::to_string(ed.oy) + " px)" : ""), Tone::Muted);
        ui.same_line(ui.S(10));
        if (ui.button("Place on screen...", Btn::OutlineInfo)) open_placement(a);
        ui.tooltip("Open a full-screen picker: drag the overlay box to where you want it (snaps to a 16x16 grid). Saved as a MangoHud anchor + pixel offset.");
        ui.label_c("Editing:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        ui.label_c(profile_appid.empty() ? "Global - every game without its own profile" : profile_name + "  \xC2\xB7  AppID " + profile_appid + "  -  this game's own profile; when it is enabled it overrides the global one", profile_appid.empty() ? Tone::Muted : Tone::Info);
        if (!profile_appid.empty()) { ui.same_line(ui.S(10)); if (ui.button("Back to global", Btn::Outline)) select_profile(a, "", ""); }
        ui.label_c("By exe name:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        std::string before = exe_name;
        ui.input("exe", &exe_name, ui.S(240));
        if (exe_name != before && profile_appid.empty()) load_editor(a);
        ui.same_line();
        ui.label_c("optional - the game's .exe / binary name; blank = the global MangoHud.conf", Tone::Muted);

        ui.label_c("Show in full:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        ui.toggle("CPU (temp + power)", &ed.full_cpu);
        ui.same_line(ui.S(12));
        ui.toggle("GPU (temp + power)", &ed.full_gpu);
        ui.same_line(ui.S(12));
        ui.toggle("Memory (+ VRAM)", &ed.full_mem);
        ui.label_c("Also show:", Tone::Normal, Font::Regular, ui.S(110));
        ui.same_line();
        ui.toggle("Frametime graph", &ed.graph);
        ui.tooltip("The frametime number and its graph. Independent of the group toggles - off writes `frame_timing=0` / `frametime=0` so it never shows (MangoHud defaults it ON, so removing the line isn't enough), on writes them =1.");
        ui.same_line(ui.S(12));
        ui.toggle("GPU in use (name)", &ed.gpuname);
        ui.tooltip("MangoHud's `gpu_name` line - the name of the card actually rendering, so on a PRIME / hybrid setup it confirms which GPU the game landed on. Off writes `gpu_name=0`.");
        ui.flow_reset();
        ui.toggle("GPU core clock", &ed.core_clock);
        ui.same_line(ui.S(12));
        ui.toggle("GPU mem clock", &ed.mem_clock);
        ui.same_line(ui.S(12));
        ui.toggle("Feral GameMode status", &ed.gamemode);
        ui.tooltip("Add MangoHud's `gamemode` line - shows GAMEMODE ON/OFF in the overlay so you can see at a glance whether Feral GameMode (gamemoderun) actually engaged for the running game.");
        bool sl = status_line;
        if (ui.toggle("TuxThrottle live status line", &sl)) {
            status_line = sl;
            mangohud::status_line::set_enabled(sl, a.user);
            a.log(std::string("[MangoHud] live status line ") + (sl ? "enabled" : "disabled") + " - shown by the tray monitor, refreshed every ~2s");
            fixlog::log_event("mangohud-status-line", std::string("status line ") + (sl ? "enabled" : "disabled"), "info", a.user);
        }
        ui.tooltip("Show Game Mode / fan boost / CPU+dGPU temps as a line in the overlay, kept live by the tray monitor (~2s refresh) - see throttle-relevant state without alt-tabbing out. Needs the tray running (Report a Bug > Launch tray now, or its autostart entry).");
        ui.flow_reset();
        if (ui.button_flow("Detect", Btn::Outline)) detect(a);
        ui.tooltip("Fill the CPU / GPU name fields from the hardware (/proc/cpuinfo, nvidia-smi, lspci). Overwrites what's there.");
        if (ui.button_flow("Write to MangoHud config", Btn::Success)) apply_editor(a);
        ui.tooltip("Save the names, position and toggles into the MangoHud config (per-game file if the field above is filled). Rewrites the stat section to exactly FPS + graphics API + GPU name(s) + your chosen groups / extras, recomputes `width` for the longest name, and leaves styling / keybind lines untouched. Press again after any change.");
        if (ui.button_flow("Reset config (clean)", Btn::OutlineWarn)) ui.open_popup("confirm:mh-reset");
        ui.tooltip("Replace the config with a fresh minimal one (FPS, GPU name, the groups / extras at the toggle levels above, toggle on Shift_R+F12) plus the current names/position. Old file kept as .bak.");
        ui.flow_reset();
        ui.wrapped("Every write rewrites the file so each key appears once (latest value wins), leading comments are kept and blank lines / junk are dropped. \"Reset config\" goes further - a clean minimal baseline, old file kept as .bak.\nThe overlay `width` is recalculated from the longest name (x the config's font_size) on each \"Write\", not live as you type - change a name and press Write again. Names of 8 characters or fewer get no width line (MangoHud sizes those itself). \"Place on screen...\" only writes the position, not the width.", Tone::Muted);
        ui.card_end();
        if (a.confirm("mh-reset", "Reset MangoHud config", "Replace " + conf_path(a) + " with a clean config - FPS, the graphics-API line, each GPU's name, the CPU/GPU/Memory groups + extras at the levels set by the toggles above, your names + position, toggle on Shift_R+F12 - and nothing else?\n\nThe current file is backed up to a .bak first.", "Reset")) reset_editor(a);
    }

    // ---------------------------------------------------------------- placement picker
    void open_placement(App& a) {
        auto conf = mhedit::parse_conf(read_file(read_path(a)).value_or(""));
        saved_pos = conf.position.empty() ? "top-left" : conf.position;
        saved_ox = conf.offset_x; saved_oy = conf.offset_y;
        SDL_Rect r;
        if (SDL_GetDisplayBounds(0, &r) != 0) { r.w = 1920; r.h = 1080; }
        screen_w = r.w; screen_h = r.h;
        auto xy = mhedit::box_xy(ed.pos, ed.ox, ed.oy, screen_w, screen_h);
        pbx = xy.first; pby = xy.second;
        pos_open = true;
        pdrag = false;
    }
    int screen_w = 1920, screen_h = 1080;

    void placement_dialog(App& a) {
        if (!pos_open) return;
        Ui& ui = a.ui;
        constexpr int GRID = 16;
        ui.begin_modal("Place the MangoHud overlay", ui.win_w() - ui.S(80), ui.win_h() - ui.S(80));
        // preview of the screen, scaled to fit the area under the controls
        Rect avail = {ui.cursor_x(), ui.cursor_y() + ui.S(78), ui.avail_w(), ui.win_h() - ui.cursor_y() - ui.S(78) - ui.S(130)};
        double sc = std::min(static_cast<double>(avail.w) / screen_w, static_cast<double>(avail.h) / screen_h);
        Rect scr = {avail.x + static_cast<int>((avail.w - screen_w * sc) / 2), avail.y, static_cast<int>(screen_w * sc), static_cast<int>(screen_h * sc)};
        double gx = (screen_w - mhedit::kBoxW) / double(GRID - 1), gy = (screen_h - mhedit::kBoxH) / double(GRID - 1);
        auto snap = [&](double bx, double by) { return psnap ? std::make_pair(gx ? std::nearbyint(bx / gx) * gx : bx, gy ? std::nearbyint(by / gy) * gy : by) : std::make_pair(bx, by); };
        Rect box = {scr.x + static_cast<int>(pbx * sc), scr.y + static_cast<int>(pby * sc), static_cast<int>(mhedit::kBoxW * sc), static_cast<int>(mhedit::kBoxH * sc)};
        if (ui.mouse_down() == false) pdrag = false;
        if (!pdrag && ui.mouse_down() && box.contains(ui.mouse_x(), ui.mouse_y())) { pdrag = true; pdx = (ui.mouse_x() - box.x) / sc; pdy = (ui.mouse_y() - box.y) / sc; }
        if (pdrag) {
            double nx = (ui.mouse_x() - scr.x) / sc - pdx, ny = (ui.mouse_y() - scr.y) / sc - pdy;
            auto s = snap(nx, ny);
            pbx = std::clamp(s.first, 0.0, double(screen_w - mhedit::kBoxW));
            pby = std::clamp(s.second, 0.0, double(screen_h - mhedit::kBoxH));
            box = {scr.x + static_cast<int>(pbx * sc), scr.y + static_cast<int>(pby * sc), static_cast<int>(mhedit::kBoxW * sc), static_cast<int>(mhedit::kBoxH * sc)};
            ui.request_redraw();
        }
        auto an = mhedit::anchor_offset(pbx, pby, screen_w, screen_h);
        ui.label(an.pos + "    offset  " + std::to_string(an.ox) + ", " + std::to_string(an.oy) + " px", Tone::Normal, ui.S(15), Font::Bold);
        bool sn = psnap;
        if (ui.toggle("snap to 16x16 grid", &sn)) { psnap = sn; if (psnap) { auto s = snap(pbx, pby); pbx = s.first; pby = s.second; } }
        ui.flow_reset();
        if (ui.button_flow("Save position", Btn::Success)) { ed.pos = an.pos; ed.ox = an.ox; ed.oy = an.oy; ed.pos_set = true; write_position(a); pos_open = false; }
        if (ui.button_flow("Restore last saved", Btn::OutlineInfo)) { auto xy = mhedit::box_xy(saved_pos, saved_ox, saved_oy, screen_w, screen_h); pbx = xy.first; pby = xy.second; }
        if (ui.button_flow("Restore default (top-left)", Btn::Outline)) { auto xy = mhedit::box_xy("top-left", 0, 0, screen_w, screen_h); pbx = xy.first; pby = xy.second; }
        if (ui.button_flow("Cancel", Btn::OutlineDanger) || ui.key_pressed(SDLK_ESCAPE)) pos_open = false;
        ui.flow_reset();
        // draw the preview after the controls so it sits on top of the modal background
        ui.fill(scr, Color(59, 59, 59), 4);
        for (int i = 1; i < GRID - 1; ++i) {
            int lx = scr.x + static_cast<int>(i * scr.w / double(GRID - 1)), ly = scr.y + static_cast<int>(i * scr.h / double(GRID - 1));
            ui.fill({lx, scr.y, 1, scr.h}, Color(72, 72, 72));
            ui.fill({scr.x, ly, scr.w, 1}, Color(72, 72, 72));
        }
        for (int i : {1, 2}) { ui.fill({scr.x + scr.w * i / 3, scr.y, 1, scr.h}, Color(96, 96, 96)); ui.fill({scr.x, scr.y + scr.h * i / 3, scr.w, 1}, Color(96, 96, 96)); }
        ui.fill(box, ui.th.accent, 4);
        ui.stroke(box, Color(255, 255, 255), 2, 4);
        std::string t = "MangoHud";
        ui.text_at(box.x + (box.w - ui.text_w(t, ui.S(13), Font::Bold)) / 2, box.y + ui.S(10), t, Color(255, 255, 255), ui.S(13), Font::Bold);
        ui.end_modal();
    }
};
}  // namespace

std::unique_ptr<Page> make_mangohud() { return std::make_unique<Mango>(); }

}  // namespace tt::app
