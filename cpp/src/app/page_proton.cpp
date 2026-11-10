// Proton & Runtimes: Steam tools, default Proton, community builds (GE-Proton, Wine, DXVK, VKD3D).
#include <algorithm>
#include <memory>
#include <mutex>

#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {

struct Proton : Page {
    Proton() { title = "Proton & Runtimes"; group = "Games"; }
    Json state;
    std::map<std::string, Json> rel_cache;
    std::map<std::pair<std::string, std::string>, Json> vmap;
    bool loaded = false, was_busy = false, loading = false, verifying = false, rel_loading = false;
    std::string verify_lbl = "not checked yet";
    Tone verify_tone = Tone::Muted;
    Json verify_res;
    int def_i = 0, target_i = 0, tool_i = 0, tag_i = 0;
    bool def_all = true;
    std::string pending_name, pending_kind, pending_appid;
    long long pending_size = 0;
    std::string rel_pending;

    void enter(App& a) override { if (!loaded) { loaded = true; } reload(a); }

    void reload(App& a) { loading = true; a.job("ct-status").start_shell(a.user_ctl("compat status --json")); }

    static Json parse_obj(const std::vector<std::string>& v, char open, char close) {
        std::string t = join(v, "\n");
        size_t b = t.find(open), e = t.rfind(close);
        if (b == std::string::npos || e == std::string::npos) return Json();
        try { return Json::parse(t.substr(b, e - b + 1)); } catch (...) { return Json(); }
    }

    void poll(App& a) {
        if (loading) {
            Job& j = a.job("ct-status");
            if (j.busy()) a.ui.wake_after(300);
            else {
                loading = false;
                Json r = parse_obj(j.snapshot(), '{', '}');
                state = r.is_object() ? r : Json::parse("{\"error\":true}");
                load_releases(a);
            }
        }
        if (verifying) {
            Job& j = a.job("ct-verify");
            if (j.busy()) a.ui.wake_after(300);
            else {
                verifying = false;
                Json r = parse_obj(j.snapshot(), '{', '}');
                if (!r.is_object()) { verify_lbl = "could not run the check"; verify_tone = Tone::Danger; }
                else {
                    verify_res = r;
                    size_t n = r["items"].array().size();
                    int bad = static_cast<int>(r["bad"].as_number());
                    verify_lbl = bad ? std::to_string(bad) + " of " + std::to_string(n) + " have problems" : "all " + std::to_string(n) + " OK";
                    verify_tone = bad ? Tone::Danger : Tone::Success;
                    vmap.clear();
                    for (const auto& i : r["items"].array()) vmap[{i["where"].str(), i["what"].str()}] = i;
                    a.log("[Proton] verify: " + std::to_string(n - static_cast<size_t>(bad)) + "/" + std::to_string(n) + " OK");
                    reload(a);
                }
            }
        }
        if (rel_loading) {
            Job& j = a.job("ct-rel");
            if (j.busy()) a.ui.wake_after(400);
            else {
                rel_loading = false;
                Json r = parse_obj(j.snapshot(), '[', ']');
                rel_cache[rel_pending] = r.is_array() ? r : Json::arr();
            }
        }
    }

    std::vector<std::string> tools_for_target() {
        std::vector<std::string> out;
        const Json& tgt = cur_target();
        for (const auto& kv : state["tools"].object()) {
            for (const auto& l : kv.second["launchers"].array()) if (l.str() == tgt["launcher"].str()) { out.push_back(kv.first); break; }
        }
        if (out.empty()) for (const auto& kv : state["tools"].object()) out.push_back(kv.first);
        return out;
    }
    const Json& cur_target() {
        const auto& t = state["targets"].array();
        static const Json none;
        return target_i >= 0 && target_i < static_cast<int>(t.size()) ? t[static_cast<size_t>(target_i)] : none;
    }
    std::vector<std::string> present_targets() {
        std::vector<std::string> out;
        for (const auto& t : state["targets"].array()) if (t["present"].as_bool()) out.push_back(t["name"].str());
        return out;
    }

    void load_releases(App& a) {
        auto tools = tools_for_target();
        if (tools.empty()) return;
        if (tool_i >= static_cast<int>(tools.size())) tool_i = 0;
        std::string tool = tools[static_cast<size_t>(tool_i)];
        if (rel_cache.count(tool)) return;
        rel_pending = tool;
        rel_loading = true;
        a.job("ct-rel").start_shell(a.user_ctl("compat releases " + shell_quote(tool) + " --json"));
    }

    void run(App& a, const std::string& desc, const std::string& args) {
        a.run_user_ctl_task("Proton", desc, "compat " + args);
        want_reload = true;
    }
    bool want_reload = false;

    void mark(Ui& ui, const std::string& where, const std::string& what, bool installed) {
        auto it = vmap.find({where, what});
        if (!installed || it == vmap.end()) { ui.label_c("\xC2\xB7", Tone::Muted, Font::Regular, ui.S(20)); return; }
        const Json& v = it->second;
        ui.label_c(v["ok"].as_bool() ? "\xE2\x9C\x93" : "x", v["ok"].as_bool() ? Tone::Success : Tone::Danger, Font::Regular, ui.S(20));
        std::vector<std::string> d;
        for (const auto& p : v["problems"].array()) d.push_back(p.str());
        for (const auto& p : v["notes"].array()) d.push_back(p.str());
        if (!d.empty()) ui.tooltip(join(d, "; "));
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        poll(a);
        if (was_busy && !a.busy()) reload(a);
        was_busy = a.busy();
        bool en = !a.busy();
        a.page_title("Proton & Runtimes", "Everything a Windows game needs to run: Valve's own Proton builds and Steam Linux Runtimes, and the community Proton builds for Steam, Heroic, Lutris and Bottles. Community builds always install under your home folder - the OS drive.");

        ui.card_begin("Verify");
        if (ui.button("Verify everything", Btn::OutlineInfo, 0, en && !verifying)) { verifying = true; verify_lbl = "checking..."; verify_tone = Tone::Muted; a.job("ct-verify").start_shell(a.user_ctl("compat verify --json")); }
        ui.tooltip("Check every installed build and every Steam tool: the files a launcher needs are there and executable, Steam reports the tool as fully installed, and nothing sits on a drive that can't hold it properly. Read-only.");
        ui.same_line(ui.S(12));
        ui.label_c(verify_lbl, verify_tone);
        ui.wrapped("Steam and the other launchers find these on their own when they start, so anything installed or moved while one is open shows up after restarting it.", Tone::Muted);
        for (const auto& i : verify_res["items"].array()) {
            if (i["ok"].as_bool() && i["notes"].array().empty()) continue;
            ui.push_id(i["what"].str() + i["where"].str());
            ui.label_c((i["ok"].as_bool() ? "\xE2\x9C\x93 " : "x ") + i["what"].str() + "  (" + i["where"].str() + ")", i["ok"].as_bool() ? Tone::Success : Tone::Danger, Font::Regular, ui.S(380));
            ui.same_line();
            std::vector<std::string> d;
            for (const auto& p : i["problems"].array()) d.push_back(p.str());
            for (const auto& p : i["notes"].array()) d.push_back(p.str());
            ui.label_c(join(d, "; "), Tone::Muted);
            if (!i["ok"].as_bool() && i["appid"].is_string()) { ui.same_line(); if (ui.button("Verify files in Steam", Btn::OutlineWarn, 0, en)) run(a, "Steam - verify " + i["what"].str(), "steam-validate " + i["appid"].str()); }
            ui.pop_id();
        }
        ui.card_end();

        ui.card_begin("Steam's own tools");
        ui.wrapped("Install (queue) puts the tool straight into the Steam library on your OS drive and Steam downloads it at its next start, so queue what you want and restart Steam once. Install via Steam dialog needs no restart but you click through Steam. Move to OS drive needs Steam closed.", Tone::Muted);
        int queued = 0;
        for (const auto& t : state["steam_tools"].array()) if (t["installed"].as_bool() && !(t["ready"].is_bool() ? t["ready"].as_bool() : true)) ++queued;
        bool running = state["steam_running"].as_bool();
        if (queued) {
            ui.wrapped(std::to_string(queued) + " tool(s) queued. Steam only reads its library when it starts - " + (running ? "restart it once to begin the downloads." : "start it to begin the downloads."), Tone::Warning);
            if (ui.button(running ? "Restart Steam now" : "Start Steam", Btn::Warning, 0, en)) { if (running) ui.open_popup("confirm:ct-restart"); else run(a, "Steam - restart to pick up queued tools", "restart-steam"); }
        }
        if (a.confirm("ct-restart", "Restart Steam", "Close Steam and start it again now?\n\nA running game or an active download is interrupted. The queued tools start downloading as soon as Steam is back.", "Restart")) run(a, "Steam - restart to pick up queued tools", "restart-steam");
        for (const auto& t : state["steam_tools"].array()) {
            ui.push_id(t["appid"].str());
            bool inst = t["installed"].as_bool();
            mark(ui, "Steam tool", t["name"].str(), inst);
            ui.same_line();
            ui.label_c(t["name"].str(), Tone::Normal, Font::Regular, ui.S(300));
            ui.same_line();
            if (!inst) {
                ui.label_c("not installed", Tone::Muted, Font::Regular, ui.S(140));
                ui.same_line();
                if (ui.button("Install (queue)", Btn::OutlineSuccess, 0, en)) run(a, "Steam - install " + t["name"].str(), "queue-install " + t["appid"].str());
                ui.tooltip("No dialog: queued straight into the Steam library on your OS drive. Steam downloads it when it next starts - queue several, then restart Steam once.");
                ui.same_line();
                if (ui.button("Install via Steam dialog", Btn::OutlineInfo, 0, en)) run(a, "Steam - install " + t["name"].str() + " (dialog)", "steam-install " + t["appid"].str());
                ui.tooltip("No restart: Steam opens its own install dialog right away (starting Steam if it's closed). You pick the library there - choose the one on your OS drive.");
            } else if (!(t["ready"].is_bool() ? t["ready"].as_bool() : true)) ui.label_c("queued - downloads once Steam (re)starts", Tone::Info);
            else if (t["on_os_drive"].as_bool()) ui.label_c("installed \xC2\xB7 OS drive", Tone::Success);
            else {
                ui.label_c("installed \xC2\xB7 " + t["library"].str(), Tone::Warning, Font::Regular, ui.S(300));
                ui.same_line();
                if (ui.button("Move to OS drive", Btn::OutlineWarn, 0, en)) { pending_appid = t["appid"].str(); pending_name = t["name"].str(); pending_size = static_cast<long long>(t["size"].as_number()); ui.open_popup("confirm:ct-move"); }
            }
            ui.pop_id();
        }
        if (a.confirm("ct-move", "Move to OS drive", "Move " + pending_name + " (~" + std::to_string(pending_size >> 20) + " MiB) into the Steam library on your OS drive?\n\nIt is copied first and only then removed from the other drive. Steam must be closed.", "Move")) run(a, "Steam - move " + pending_name + " to the OS drive", "move-to-os " + pending_appid);
        ui.card_end();

        // default proton
        ui.card_begin("Default Proton");
        ui.wrapped("Which Proton Steam uses when a game has no choice of its own. Steam's own setting only covers titles Valve has no recommendation for; for the rest Valve's pick wins, which is how a game ends up on Proton Hotfix. Tick the box to pin your choice on those games too.", Tone::Muted);
        std::vector<std::string> labels;
        std::map<std::string, std::string> names;
        for (const auto& c : state["compat_names"].array()) { labels.push_back(c["label"].str()); names[c["label"].str()] = c["name"].str(); }
        if (labels.empty()) labels.push_back("Proton Experimental");
        if (def_i >= static_cast<int>(labels.size())) def_i = 0;
        ui.label_c("Use:");
        ui.same_line();
        ui.combo("def", &def_i, labels, ui.S(280));
        ui.same_line(ui.S(12));
        ui.toggle("also for installed games Steam picks a build for", &def_all);
        ui.tooltip("Writes a per-game choice for every installed Windows game that has none of its own or sits on Proton Hotfix. Native Linux games and games you already forced to another build are left alone.");
        ui.same_line(ui.S(12));
        if (ui.button("Apply", Btn::Success, 0, en)) {
            std::string label = labels[static_cast<size_t>(def_i)];
            if (!names.count(label)) a.message("Default Proton", label + " is not installed - install it above first.");
            else if (running) ui.open_popup("confirm:ct-default");
            else do_default(a, label, names[label], false);
        }
        if (a.confirm("ct-default", "Default Proton", "Make " + labels[static_cast<size_t>(def_i)] + " the default" + (def_all ? " for every installed Windows game without its own choice" : "") + "?\n\nSteam is running and rewrites this setting when it exits, so it has to be restarted for the change to stick. A running game or download is interrupted. Restart Steam now?", "Restart Steam"))
            do_default(a, labels[static_cast<size_t>(def_i)], names[labels[static_cast<size_t>(def_i)]], true);
        std::string cur = state["compat_default"].str(), cur_label = cur;
        for (auto& [l, n] : names) if (n == cur) cur_label = l;
        ui.label("Steam's default now: " + (cur_label.empty() ? std::string("not set") : cur_label) + "  -  " + std::to_string(state["compat_forced"].object().size()) + " game(s) have their own choice", Tone::Muted);
        ui.card_end();

        // community builds
        ui.card_begin("Community builds - Proton, Wine, DXVK, VKD3D");
        ui.wrapped("Downloaded from each project's GitHub releases, checked against the published sha512sum where the project provides one, and unpacked into the launcher's own tools folder. Steam takes Proton builds; Heroic, Lutris and Bottles also take Wine builds and the DXVK / vkd3d-proton layers.", Tone::Muted);
        auto present = present_targets();
        std::vector<std::string> tnames;
        std::vector<int> map_i;
        {
            int i = 0;
            for (const auto& t : state["targets"].array()) { if (t["present"].as_bool()) { tnames.push_back(t["name"].str()); map_i.push_back(i); } ++i; }
        }
        if (tnames.empty()) { tnames.push_back("Steam"); map_i.push_back(0); }
        int sel = static_cast<int>(std::find(map_i.begin(), map_i.end(), target_i) - map_i.begin());
        if (sel >= static_cast<int>(tnames.size())) sel = 0;
        ui.label_c("Launcher:");
        ui.same_line();
        if (ui.combo("target", &sel, tnames, ui.S(170))) { target_i = map_i[static_cast<size_t>(sel)]; tool_i = 0; tag_i = 0; load_releases(a); }
        else target_i = map_i[static_cast<size_t>(sel)];
        auto tools = tools_for_target();
        if (tool_i >= static_cast<int>(tools.size())) tool_i = 0;
        ui.same_line(ui.S(12));
        ui.label_c("Build:");
        ui.same_line();
        if (!tools.empty() && ui.combo("tool", &tool_i, tools, ui.S(170))) { tag_i = 0; load_releases(a); }
        std::string tool = tools.empty() ? "" : tools[static_cast<size_t>(tool_i)];
        std::vector<std::string> tags{"latest"};
        for (const auto& r : rel_cache[tool].array()) tags.push_back(r["tag"].str() + "   (" + r["date"].str() + ", " + std::to_string(static_cast<long long>(r["size"].as_number()) >> 20) + " MiB)");
        if (tag_i >= static_cast<int>(tags.size())) tag_i = 0;
        ui.same_line(ui.S(12));
        ui.label_c("Version:");
        ui.same_line();
        ui.combo("tag", &tag_i, tags, ui.S(300));
        ui.same_line(ui.S(12));
        if (ui.button("Install", Btn::Success, 0, en && !tool.empty())) {
            std::string tag = tags[static_cast<size_t>(tag_i)].substr(0, tags[static_cast<size_t>(tag_i)].find("   "));
            std::string args = "install " + shell_quote(tool) + " --target " + shell_quote(tnames[static_cast<size_t>(sel)]);
            if (tag != "latest") args += " --tag " + shell_quote(tag);
            run(a, "install " + tool + " " + tag + " for " + tnames[static_cast<size_t>(sel)], args);
        }
        ui.tooltip("Download, verify and unpack this build into the selected launcher's tools folder.");
        ui.wrapped(state["tools"][tool]["about"].str(), Tone::Muted);
        ui.label("Installed in this launcher:");
        const Json& tgt = cur_target();
        if (tgt["installed"].array().empty()) ui.label("nothing yet", Tone::Muted);
        for (const auto& it : tgt["installed"].array()) {
            ui.push_id(it["name"].str());
            mark(ui, tgt["name"].str(), it["name"].str(), true);
            ui.same_line();
            ui.label_c(it["name"].str(), Tone::Normal, Font::Regular, ui.S(330));
            ui.same_line();
            ui.label_c(it["kind"].str(), Tone::Muted, Font::Regular, ui.S(60));
            ui.same_line();
            if (ui.button("Remove", Btn::OutlineDanger, 0, en)) { pending_name = it["name"].str(); pending_kind = it["kind"].str(); ui.open_popup("confirm:ct-remove"); }
            ui.pop_id();
        }
        ui.card_end();
        std::string target_name = tnames[static_cast<size_t>(sel)];
        if (a.confirm("ct-remove", "Remove build", "Delete " + pending_name + " from " + target_name + "?\n\nA game still set to use it will fall back to the launcher's default.", "Delete"))
            run(a, "remove " + pending_name, "remove " + shell_quote(target_name) + " " + shell_quote(pending_name) + (pending_kind.empty() ? "" : " --kind " + pending_kind));
    }

    void do_default(App& a, const std::string& label, const std::string& tool, bool restart) {
        std::string args = "set-default " + shell_quote(tool) + (def_all ? " --all-games" : "") + (restart ? " --restart-steam" : "");
        run(a, "default " + label, args);
    }
};
}  // namespace

std::unique_ptr<Page> make_proton() { return std::make_unique<Proton>(); }

}  // namespace tt::app
