// Game Tools: Proton prefix / save vault, shader cache, Steam low-resource mode, fixes, launch-options builder,
// frame-rate limits, per-game dGPU, last session, benchmark A/B, GPU driver installer.
#include <algorithm>
#include <ctime>
#include <memory>
#include <mutex>

#include "../distro.hpp"
#include "../engine.hpp"
#include "../formats.hpp"
#include "../gamecfg.hpp"
#include "../launchbuild.hpp"
#include "../sensors.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {

std::string human_bytes(double n) {
    const char* u[] = {"B", "KB", "MB", "GB", "TB"};
    int i = 0;
    while (n >= 1024 && i < 4) { n /= 1024; ++i; }
    return format_double(n, i >= 2 ? 1 : 0) + " " + u[i];
}

std::string last_line(const std::vector<std::string>& v) {
    for (auto it = v.rbegin(); it != v.rend(); ++it) { std::string t = trim(*it); if (!t.empty()) return t; }
    return "";
}

// Runs `a.job(key)` to completion once, then hands the output to `on_done` on the UI thread.
struct Watch {
    bool pending = false;
    void start(App& a, const std::string& key, const std::string& cmd) { a.job(key).start_shell(cmd); pending = true; }
    void poll(App& a, const std::string& key, const std::function<void(const std::vector<std::string>&)>& on_done) {
        if (!pending) return;
        Job& j = a.job(key);
        if (j.busy()) { a.ui.wake_after(300); return; }
        pending = false;
        on_done(j.snapshot());
    }
};

struct GameTools : Page {
    GameTools() { title = "Game Tools"; group = "Games"; }

    // ---- prefix / vault ----
    std::string appid_in, vault;
    // ---- shader cache ----
    std::string sc_dir = "~/.cache/tuxthrottle-shaders";
    int sc_gb = 80;
    std::string sc_link = "link status: -", sc_sizes = "sizes: not calculated yet", sc_bg = "-";
    Tone sc_link_tone = Tone::Muted;
    Watch w_link, w_bg, w_sizes, w_sp, w_diag, w_fps, w_dg;
    // ---- steam low-resource ----
    std::string sp_state = "-";
    bool sp_autostart = true;
    // ---- fixes ----
    std::string fx_text;
    std::vector<std::string> fx_unmounted;
    // ---- launch options ----
    launchbuild::Opts lo;
    bool lo_only_empty = true, lo_keep_custom = true, heroic_nowayland = true;
    // ---- fps ----
    struct FpsRow { Json game; bool tick = false; std::string fps; };
    std::vector<FpsRow> fps_rows;
    int fps_method = 0, fps_src = 0;
    std::string fps_copyval = "60", fps_fill, fps_status;
    // ---- dgpu ----
    struct DgGame { std::string name, appid; bool dgpu = false, active = false, hook = false; };
    std::vector<DgGame> dg_games;
    std::map<std::string, bool> dg_pending;
    std::string dg_filter, dg_status;
    bool dg_only_off = false;
    // ---- bench ----
    int bn_dur = 60;
    std::string bn_a, bn_b;
    // ---- gpu drivers ----
    bool gd_32 = true, gd_pw = true;
    bool loaded = false, was_busy = false;
    std::string pending_vault_mode;

    void enter(App& a) override {
        if (!loaded) {
            Json c = a.read_state("shadercache.json");
            if (c["dir"].is_string() && !c["dir"].str().empty()) sc_dir = c["dir"].str();
            if (c["max_size_gb"].is_number()) sc_gb = static_cast<int>(c["max_size_gb"].as_number());
            auto v = read_file(a.cfg_dir() + "/saves_vault");
            if (v) vault = trim(*v);
            lo = launchbuild::defaults(sensors::has_nvidia_gpu(), sensors::has_amd_gpu());
            loaded = true;
            refresh_sizes(a); link_check(a); bg_refresh(a); sp_refresh(a); fps_reload(a); dg_reload(a);
        }
    }

    std::string abs_cache_dir(App& a) {
        std::string raw = sc_dir;
        if (!raw.empty() && raw[0] == '~') raw = home_of(a.user) + raw.substr(1);
        return raw;
    }

    void persist_cache(App& a, const std::string& what) {
        std::string d = trim(sc_dir).empty() ? "~/.cache/tuxthrottle-shaders" : trim(sc_dir);
        int gb = std::clamp(sc_gb, 5, 1000);
        Json j = Json::obj();
        j.set("dir", d); j.set("max_size_gb", gb);
        a.write_state("shadercache.json", j);
        a.log("[Cache] shader cache " + what + " saved -> " + d + "  (max " + std::to_string(gb) + " GB)");
        refresh_sizes(a);
        engine::Item* nv = a.find_item("NvidiaShaderCache");
        if (nv && nv->done()) a.run_shell_task("update NVIDIA shader-cache config", shell_quote(ctl_exe()) + " apply-tweak NvidiaShaderCache");
    }

    void refresh_sizes(App& a) {
        std::string base = abs_cache_dir(a);
        for (const char* s : {"mesa-shader-cache", "dxvk-state-cache", "nv-shader-cache", "steam-shadercache"}) { mkdir_p(base + "/" + s); chown_to_user(base + "/" + s, a.user); }
        sc_sizes = "sizes: calculating...";
        auto du = [base](const std::string& sub) -> double {
            auto r = run_argv({"du", "-sb", "--", base + "/" + sub}, 120000);
            auto w = split_ws(r.out);
            return w.empty() ? 0.0 : std::atof(w[0].c_str());
        };
        a.job("sc-size").start_fn([du](const std::function<void(const std::string&)>& out) {
            double steam = du("steam-shadercache"), other = du("mesa-shader-cache") + du("dxvk-state-cache") + du("nv-shader-cache");
            out("sizes:  total " + human_bytes(steam + other) + "   \xC2\xB7   Steam cache " + human_bytes(steam) + "   \xC2\xB7   other (Mesa+DXVK+NVIDIA) " + human_bytes(other));
            return 0;
        });
        w_sizes.pending = true;
    }
    void link_check(App& a) { sc_link = "link status: checking..."; sc_link_tone = Tone::Muted; w_link.start(a, "sc-link", a.user_ctl("shadercache link-check --json")); }
    void bg_refresh(App& a) { w_bg.start(a, "sc-bg", a.user_ctl("shadercache steam-bg-shaders status")); }
    void sp_refresh(App& a) { w_sp.start(a, "sp-state", a.user_ctl("steamperf status")); }
    void fps_reload(App& a) { fps_status = "Reading Heroic / Lutris / Steam libraries..."; w_fps.start(a, "fps-list", a.user_ctl("fpslimit list --json")); }
    void dg_reload(App& a) { dg_status = "loading games..."; w_dg.start(a, "dg-list", a.user_ctl("mangohud-games list --json")); }

    void polls(App& a) {
        w_link.poll(a, "sc-link", [&](const std::vector<std::string>& v) {
            std::string out = join(v, "\n");
            size_t b = out.find('{'), e = out.rfind('}');
            Json r = b != std::string::npos && e != std::string::npos ? Json::parse(out.substr(b, e - b + 1)) : Json();
            bool healthy = r["ok"].is_bool() ? r["ok"].as_bool() : true;
            sc_link = "link status: " + r["summary"].str("could not check");
            sc_link_tone = healthy ? (r["linked"].as_number() > 0 ? Tone::Success : Tone::Muted) : Tone::Danger;
            if (!healthy) a.log("[Cache] " + r["summary"].str() + " - press \"Link Steam's shader cache here\" to repair");
        });
        w_sizes.poll(a, "sc-size", [&](const std::vector<std::string>& v) { sc_sizes = last_line(v); });
        w_bg.poll(a, "sc-bg", [&](const std::vector<std::string>& v) { std::string s = last_line(v); sc_bg = s == "on" ? "ON" : s == "off" ? "OFF" : s.empty() ? "?" : s; });
        w_sp.poll(a, "sp-state", [&](const std::vector<std::string>& v) { sp_state = last_line(v); });
        w_diag.poll(a, "fx-diag", [&](const std::vector<std::string>& v) {
            std::string out = join(v, "\n");
            size_t b = out.find('['), e = out.rfind(']');
            std::string text;
            try {
                Json r = Json::parse(out.substr(b, e - b + 1));
                for (const auto& row : r.array()) {
                    std::string st = row.array().size() > 0 ? row.array()[0].str() : "bad", msg = row.array().size() > 1 ? row.array()[1].str() : "";
                    text += (st == "ok" ? "\xE2\x9C\x93  " : "x  ") + msg + "\n";
                    if (st != "ok") fixlog::log_event("diagnose", msg, "warn", a.user);
                }
            } catch (...) { text = "x  could not run the diagnostic - see the log console"; }
            fx_text = trim(text);
        });
        w_fps.poll(a, "fps-list", [&](const std::vector<std::string>& v) {
            fps_rows.clear();
            try {
                Json rows = Json::parse(last_line(v));
                std::vector<Json> g(rows.array().begin(), rows.array().end());
                static const std::map<std::string, int> order = {{"heroic", 0}, {"lutris", 1}, {"steam", 2}};
                std::stable_sort(g.begin(), g.end(), [&](const Json& x, const Json& y) {
                    auto o = [&](const Json& q) { auto it = order.find(q["launcher"].str()); return it == order.end() ? 9 : it->second; };
                    auto r2 = [&](const Json& q) { return (q["override"].is_number() || q["recommended"].is_number()) ? 0 : 1; };
                    if (o(x) != o(y)) return o(x) < o(y);
                    if (r2(x) != r2(y)) return r2(x) < r2(y);
                    return lower(x["name"].str()) < lower(y["name"].str());
                });
                for (const auto& q : g) {
                    FpsRow r;
                    r.game = q;
                    const Json& sug = q["override"].is_number() ? q["override"] : q["recommended"];
                    r.tick = sug.is_number();
                    const Json& cur = q["current"];
                    std::string now = cur["STRANGLE_FPS"].is_string() ? cur["STRANGLE_FPS"].str() : cur["DXVK_FRAME_RATE"].str();
                    r.fps = sug.is_number() ? format_double(sug.as_number(), 0) : now;
                    fps_rows.push_back(r);
                }
                fps_status = std::to_string(fps_rows.size()) + " titles; ticked ones are the ones with a recommendation.";
            } catch (const std::exception& e) { fps_status = std::string("Could not list titles: ") + e.what(); }
        });
        w_dg.poll(a, "dg-list", [&](const std::vector<std::string>& v) {
            dg_games.clear();
            try {
                Json rows = Json::parse(last_line(v));
                for (const auto& r : rows.array()) {
                    if (r["id"].str().rfind("steam:", 0) != 0) continue;
                    DgGame g;
                    g.name = r["name"].str(); g.appid = r["appid"].str(); g.dgpu = r["dgpu"].as_bool(); g.active = r["dgpu_active"].is_bool() ? r["dgpu_active"].as_bool() : g.dgpu; g.hook = r["hook"].as_bool();
                    dg_games.push_back(g);
                }
                dg_status.clear();
            } catch (...) { dg_status = "could not read the Steam library"; }
        });
    }

    std::string b64(const std::string& s) { return base64_encode(s); }

    void draw(App& a) override {
        Ui& ui = a.ui;
        if (!loaded) enter(a);
        polls(a);
        if (was_busy && !a.busy()) { refresh_sizes(a); sp_refresh(a); bg_refresh(a); link_check(a); dg_reload(a); fps_reload(a); }
        was_busy = a.busy();
        bool en = !a.busy();
        a.page_title("Game Tools", "Steam / Proton helpers that work for any game - not just the ones with a walkthrough on \"Setup Games\": relocate a Proton prefix off a drive that can't host it, pull stray save files back, keep a save-game vault, choose one home for every shader cache, and build a launch-options string.");

        prefix_box(a, en);
        cache_box(a, en);
        steamperf_box(a, en);
        fixes_box(a, en);
        launch_box(a, en);
        fps_box(a, en);
        dgpu_box(a, en);
        session_boxes(a);
        bench_box(a, en);
        gpudrv_box(a, en);
    }

    // ------------------------------------------------------------ prefix + vault
    void prefix_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Proton prefix & save-file tools");
        ui.wrapped("A game installed on an NTFS or exFAT drive can't build its Proton prefix there (those filesystems reject ':' in a filename, so the 'dosdevices/c:' links fail and the game won't start). Relocation moves just the prefix onto your Linux drive and symlinks it back - game files stay put. The save-file scan finds prefixes whose Documents / Saved Games / AppData folder is a symlink onto another drive and pulls it back in. Close Steam first. Lutris Wine prefixes on such a drive are handled too: the scan flags them, the fix builds the prefix under ~/Games and keeps the big game folders on the original drive.", Tone::Muted);
        ui.flow_reset();
        if (ui.button_flow("Scan Steam prefixes", Btn::OutlineInfo, en)) a.run_user_ctl_task("Prefix tools", "scan Steam prefixes for NTFS/exFAT problems", "prefix --scan");
        if (ui.button_flow("Migrate all at-risk prefixes", Btn::OutlineWarn, en)) ui.open_popup("confirm:pfx-all");
        if (ui.button_flow("Scan Lutris prefixes", Btn::OutlineInfo, en)) a.run_user_ctl_task("Prefix tools", "scan Lutris prefixes for symlink problems", "prefix --lutris-scan");
        if (ui.button_flow("Fix all Lutris prefixes", Btn::OutlineWarn, en)) ui.open_popup("confirm:lutris-all");
        ui.flow_reset();
        ui.label_c("or one - AppID:");
        ui.same_line();
        ui.input("appid", &appid_in, ui.S(120));
        ui.same_line();
        if (ui.button("Relocate this prefix", Btn::OutlineWarn, 0, en)) {
            if (!is_digits(appid_in)) a.message("Steam AppID needed", "Enter the numeric Steam AppID of the game (shown on its store-page URL, or in the scan output).");
            else ui.open_popup("confirm:pfx-one");
        }
        if (a.confirm("pfx-all", "Migrate all at-risk prefixes", "Move every Proton prefix that's on an NTFS/exFAT drive onto your Linux drive (symlink left in place). Game files aren't touched.\n\nClose Steam and all games first. Run \"Scan Steam prefixes\" beforehand if you want to see the list.", "Migrate"))
            a.run_user_ctl_task("Prefix tools", "migrate all at-risk Proton prefixes", "prefix --all");
        if (a.confirm("lutris-all", "Fix all Lutris prefixes", "For every Lutris game whose Wine prefix sits on a drive that refuses symlinks (NTFS/exFAT), build the prefix under ~/Games, link the big game folders back from the original drive and point the game at the new prefix.\n\nThe old prefix is left untouched. Close Lutris and all games first. Run \"Scan Lutris prefixes\" beforehand to see the list.", "Fix"))
            a.run_user_ctl_task("Prefix tools", "fix all Lutris prefixes on symlink-hostile drives", "prefix --lutris-all");
        if (a.confirm("pfx-one", "Relocate Proton prefix", "Move AppID " + trim(appid_in) + "'s Proton prefix (compatdata/" + trim(appid_in) + ") onto your Linux drive and leave a symlink in its place?\n\nClose Steam and the game first. The game files are not touched; only the prefix moves. No-op if it's already on a Linux filesystem.", "Relocate"))
            a.run_user_ctl_task("Prefix tools", "relocate prefix for AppID " + trim(appid_in), "prefix " + shell_quote(trim(appid_in)));
        ui.flow_reset();
        if (ui.button_flow("Scan for saves on another drive", Btn::OutlineInfo, en)) a.run_user_ctl_task("Prefix tools", "scan for save files on another drive", "prefix --saves-scan");
        if (ui.button_flow("Move all stray saves into their prefixes", Btn::OutlineWarn, en)) ui.open_popup("confirm:saves-all");
        if (ui.button_flow("Import loose saves for AppID above", Btn::OutlineWarn, en)) {
            if (!is_digits(appid_in)) a.message("Steam AppID needed", "Put the game's numeric Steam AppID in the field above first, then run \"Scan for saves on another drive\" to see which loose folders exist.");
            else ui.open_popup("confirm:saves-import");
        }
        ui.flow_reset();
        if (a.confirm("saves-all", "Move stray saves into prefixes", "For every game whose Documents / Saved Games / AppData folder is a symlink onto another drive, copy that folder into the game's Proton prefix and replace the symlink.\n\nThe original off-drive copy is left in place - nothing is deleted. Close Steam and all games first. Run the scan first to see the list.", "Move"))
            a.run_user_ctl_task("Prefix tools", "move all stray saves into their prefixes", "prefix --saves-all");
        if (a.confirm("saves-import", "Import loose saves", "Copy the loose Documents / My Games / Saved Games folders from the drive that hosts AppID " + trim(appid_in) + " into that game's Proton prefix?\n\nExisting files in the prefix are kept; the originals on the other drive are left untouched. Close Steam and the game first.", "Import"))
            a.run_user_ctl_task("Prefix tools", "import loose saves for AppID " + trim(appid_in), "prefix --saves-import " + shell_quote(trim(appid_in)));

        ui.separator();
        ui.wrapped("Save-game vault - a folder on a SEPARATE drive (not the OS/Steam drive) holding a copy of every game's saves as <vault>/<appid>/... . Export copies saves out of the prefix(es) into it; Import copies them back. Leave the AppID field blank to do every prefix at once. Close Steam before importing.", Tone::Muted);
        ui.label_c("Vault folder:");
        ui.same_line();
        ui.input("vault", &vault, ui.S(420));
        ui.same_line();
        if (ui.button("Browse...", Btn::Outline)) a.open_file_dialog("vault", 2, home_of(a.user));
        std::string picked;
        if (a.file_result("vault", &picked)) { vault = picked; save_vault(a); }
        if (a.file_result("sc-dir", &picked)) sc_dir = picked;
        ui.flow_reset();
        if (ui.button_flow("List vault", Btn::OutlineInfo, en)) vault_cmd(a, "list");
        if (ui.button_flow("Export saves to vault", Btn::OutlineWarn, en)) vault_cmd(a, "export");
        if (ui.button_flow("Import saves from vault", Btn::OutlineWarn, en)) vault_cmd(a, "import");
        ui.flow_reset();
        if (!pending_vault_mode.empty()) {
            std::string who_txt = is_digits(appid_in) ? "AppID " + trim(appid_in) : "EVERY prefix";
            std::string detail = pending_vault_mode == "export" ? "Copy save data for " + who_txt + " FROM the prefix(es) INTO the vault:\n" + vault + "\n\nExisting vault files are overwritten."
                                                                : "Copy save data for " + who_txt + " FROM the vault:\n" + vault + "\nINTO the prefix(es).\n\nExisting prefix save files are overwritten by the vault copy. Close Steam first.";
            ui.open_popup("confirm:vault-go");
            if (a.confirm("vault-go", pending_vault_mode == "export" ? "Export save vault" : "Import save vault", detail, "Go ahead")) {
                run_vault(a, pending_vault_mode);
                pending_vault_mode.clear();
            } else if (!ui.popup_open("confirm:vault-go")) pending_vault_mode.clear();
        }
        ui.card_end();
    }

    static bool is_digits(const std::string& s) { std::string t = trim(s); return !t.empty() && std::all_of(t.begin(), t.end(), ::isdigit); }

    void save_vault(App& a) { mkdir_p(a.cfg_dir()); write_file(a.cfg_dir() + "/saves_vault", trim(vault) + "\n"); chown_to_user(a.cfg_dir() + "/saves_vault", a.user); }

    void vault_cmd(App& a, const std::string& mode) {
        if (trim(vault).empty()) { a.message("Pick a vault folder", "Choose the save-vault folder first (Browse...). It has to be on a separate drive - not the OS / Steam drive."); return; }
        save_vault(a);
        if (mode == "list") run_vault(a, mode); else pending_vault_mode = mode;
    }
    void run_vault(App& a, const std::string& mode) {
        std::string who = is_digits(appid_in) ? trim(appid_in) : "all";
        a.run_user_ctl_task("Save vault", "save vault " + mode + " (" + (who == "all" ? "EVERY prefix" : "AppID " + who) + ")", "savevault " + mode + " " + shell_quote(trim(vault)) + " " + who);
    }

    // ------------------------------------------------------------ shader cache
    void cache_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Shader / pipeline cache storage");
        ui.wrapped("One folder for every generated shader cache - Mesa (AMD), DXVK (D3D to Vulkan), the NVIDIA driver, and optionally Steam's own - so they can sit on the drive you choose and survive a Proton prefix wipe. The launch-options builder below and the \"NVIDIA shader-cache\" tweak both read this location. Changing it does NOT rewrite launch options you've already pasted into a game - regenerate + re-paste those if you move it.", Tone::Muted);
        ui.label_c("Cache folder:");
        ui.same_line();
        ui.input("scdir", &sc_dir, ui.S(380));
        ui.same_line();
        if (ui.button("Browse...", Btn::Outline)) a.open_file_dialog("sc-dir", 2, home_of(a.user));
        ui.same_line();
        if (ui.button("Save location", Btn::Success)) persist_cache(a, "location");
        ui.tooltip("Save the folder above to shadercache.json, create the mesa/dxvk/nv/steam sub-folders, and point the launch-options builder + NVIDIA tweak at it.");
        ui.label_c("Max size (GB):");
        ui.same_line();
        ui.spinbox("scgb", &sc_gb, 5, 1000, 10, ui.S(130));
        ui.same_line();
        if (ui.button("Apply shader cache size", Btn::Success)) persist_cache(a, "size");
        ui.same_line();
        ui.label_c("caps the Mesa & NVIDIA caches (self-prune); DXVK / Steam have no cap", Tone::Muted);
        ui.flow_reset();
        if (ui.button_flow("Link Steam's shader cache here", Btn::OutlineInfo, en)) ui.open_popup("confirm:sc-link");
        if (ui.button_flow("Undo Steam link", Btn::Outline, en)) ui.open_popup("confirm:sc-unlink");
        if (ui.button_flow("Check links", Btn::Outline, en)) link_check(a);
        ui.tooltip("Verify each Steam library's steamapps/shadercache symlink still points at this folder. A link left dangling - e.g. after moving the cache folder - makes Steam fail every shader write with \"disk write error\". If it reports broken, press \"Link Steam's shader cache here\" to repair it.");
        ui.flow_reset();
        ui.label(sc_link, sc_link_tone);
        ui.label(sc_sizes, Tone::Muted);
        if (ui.button_flow("Refresh", Btn::Outline, en)) refresh_sizes(a);
        if (ui.button_flow("Clean cache", Btn::OutlineWarn, en)) ui.open_popup("confirm:sc-clean");
        ui.flow_reset();
        if (ui.button("Force-rebuild Steam's shader cache", Btn::OutlineWarn, 0, en)) ui.open_popup("confirm:sc-rebuild");
        ui.tooltip("Delete Steam's own shader cache (the fossilize cache in steamapps/shadercache) so Steam regenerates it from scratch on the next launch. Use after a driver update or a corrupt-cache stutter. The Mesa / DXVK / NVIDIA caches are left alone. Close Steam first.");
        ui.label_c("Steam background Vulkan shader processing:", Tone::Muted);
        ui.same_line();
        ui.label_c(sc_bg, sc_bg == "OFF" ? Tone::Success : sc_bg == "ON" ? Tone::Warning : Tone::Muted, Font::Bold);
        ui.same_line();
        if (ui.button("Turn OFF", Btn::OutlineDanger, 0, en)) ui.open_popup("confirm:sc-bg-off");
        ui.tooltip("Untick Steam > Settings > Downloads > \"Allow background processing of Vulkan shaders\" - stops the fossilize_replay background compiles that peg the CPU after every download. Close Steam first; restart Steam after.");
        ui.same_line();
        if (ui.button("Turn ON", Btn::Outline, 0, en)) ui.open_popup("confirm:sc-bg-on");
        ui.card_end();
        if (a.confirm("sc-link", "Steam shader cache", "Move Steam's shadercache folder into your cache directory and symlink it back?\n\nClose Steam first.", "Link")) a.run_user_ctl_task("Cache", "Steam shader-cache link", "shadercache link-steam");
        if (a.confirm("sc-unlink", "Steam shader cache", "Undo the Steam shader-cache symlink?\n\nClose Steam first.", "Undo")) a.run_user_ctl_task("Cache", "Steam shader-cache unlink", "shadercache link-steam --undo");
        if (a.confirm("sc-clean", "Clean shader caches", "Empty every shader/pipeline cache under your cache folder?\n\nThis is optional - the caches rebuild themselves on next launch (first run of each game will stutter while they refill). Close Steam first.", "Clean")) a.run_user_ctl_task("Cache", "clean shader caches", "shadercache clean all");
        if (a.confirm("sc-rebuild", "Force-rebuild Steam's shader cache", "Delete Steam's own shader cache (the fossilize cache in steamapps/shadercache) so Steam rebuilds it from scratch on the next launch?\n\nThe Mesa / DXVK / NVIDIA caches are left alone. The first run of each game will stutter while Steam's cache refills. Close Steam first.", "Rebuild")) a.run_user_ctl_task("Cache", "force-rebuild Steam shader cache", "shadercache rebuild all");
        if (a.confirm("sc-bg-off", "Steam background shader processing", "Disable Steam's \"Allow background processing of Vulkan shaders\"?\n\nClose Steam first - it rewrites config.vdf on exit. Restart Steam afterwards for it to take effect.", "Disable")) a.run_user_ctl_task("Cache", "steam background shaders off", "shadercache steam-bg-shaders off");
        if (a.confirm("sc-bg-on", "Steam background shader processing", "Re-enable Steam's \"Allow background processing of Vulkan shaders\"?\n\nClose Steam first - it rewrites config.vdf on exit. Restart Steam afterwards for it to take effect.", "Re-enable")) a.run_user_ctl_task("Cache", "steam background shaders on", "shadercache steam-bg-shaders on");
    }

    // ------------------------------------------------------------ steam low-resource
    void steamperf_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Steam client - low-resource mode");
        ui.wrapped("Runs the Steam client (not games) as light as it goes - most of Steam's idle CPU/RAM/VRAM is its embedded Chromium UI. Adds launch flags via a user-level launcher override (~/.local/share/applications/steam.desktop, shadows the system one; autostart entry patched too): -silent (start to tray), -cef-disable-gpu + -cef-disable-gpu-compositing (no GPU accel in the store/library/friends web views - the big one on a hybrid GPU), -cef-disable-breakpad / -cef-disable-extra-info-spew (no crash reporter, quieter logs), and -noverifyfiles / -nobootstrapupdate / -norepairfiles (skip the file-scan + self-update + repair passes each launch - Steam still re-verifies on demand). It runs Steam in a systemd scope with a SOFT memory limit (MemoryHigh=1200M - the kernel just reclaims cache above that, it never kills anything), and flips every low-resource setting Steam keeps in a file (needs Steam closed): no auto Friends & Chat sign-in, no friends animations, and background Vulkan-shader processing off (the Steam Overlay + screenshots are kept). A few more toggles live in Steam's own store and can't be scripted - Enable prints them in the log for you to tick. No hard MemoryMax (that OOM-kills Steam). Takes effect next Steam start (quit fully + relaunch from the menu). Trade-off: manual chat sign-in. The toggle below adds a hidden-on-login autostart entry; Disable reverts everything.", Tone::Muted);
        std::string base = sp_state.substr(0, sp_state.find('+'));
        std::string shown = base == "on" ? "ON" : base == "off" ? "OFF" : sp_state;
        if (sp_state.find("+autostart") != std::string::npos) shown += " \xC2\xB7 autostart";
        ui.label_c("Low-resource mode:", Tone::Muted);
        ui.same_line();
        ui.label_c(shown, base == "on" ? Tone::Success : Tone::Muted, Font::Bold);
        ui.same_line();
        if (ui.button("Enable", Btn::Success, 0, en)) a.run_user_ctl_task("Steam", std::string("Steam low-resource mode on"), std::string("steamperf on") + (sp_autostart ? "" : " --no-autostart"));
        ui.same_line();
        if (ui.button("Disable", Btn::Outline, 0, en)) a.run_user_ctl_task("Steam", "Steam low-resource mode off", "steamperf off");
        ui.toggle("Autostart Steam hidden on login", &sp_autostart);
        ui.tooltip("If you have no Steam autostart entry, Enable creates one with -silent so Steam comes up on login straight to the tray (no window). Disable removes it again.");
        ui.card_end();
    }

    // ------------------------------------------------------------ fixes
    void check_mounts(App& a) {
        fx_unmounted.clear();
        auto txt = read_file("/etc/fstab");
        for (const auto& line : split_lines(txt.value_or(""))) {
            std::string l = trim(line);
            if (l.empty() || l[0] == '#') continue;
            auto f = split_ws(l);
            if (f.size() < 4) continue;
            if (f[1] == "none" || f[1] == "swap") continue;
            bool nofail = false;
            std::string o;
            for (char c : f[3] + ",") { if (c == ',') { if (o == "nofail") nofail = true; o.clear(); } else o += c; }
            if (!nofail) continue;
            if (run_argv({"findmnt", "-n", f[1]}, 5000).code != 0) fx_unmounted.push_back(f[1]);
        }
        fx_text = fx_unmounted.empty() ? "\xE2\x9C\x93  every 'nofail' drive in /etc/fstab is mounted" : "x  not mounted:\n" + [&] { std::string s; for (auto& m : fx_unmounted) s += "  " + m + "\n"; return trim(s); }();
        (void)a;
    }

    void fixes_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Fixes - quick diagnosis & one-click repairs");
        ui.wrapped("Checks for the causes behind Steam problems already tracked down on this laptop - Steam started twice at login, the client forced onto the discrete GPU, stale removed CEF flags, an unmounted Steam library drive, an NTFS volume Windows left 'dirty'. Read-only until you press a fix button below.", Tone::Muted);
        ui.flow_reset();
        if (ui.button_flow("Diagnose: \"Steam won't start\"", Btn::OutlineInfo, en)) { fx_text = "checking..."; w_diag.start(a, "fx-diag", a.user_ctl("steamperf diagnose --json")); }
        if (ui.button_flow("Fix Steam starting twice at login", Btn::OutlineWarn, en)) a.run_user_ctl_task("Steam", "Steam - move stray autostart backups out", "steamperf sweep-autostart");
        ui.tooltip("A backup copy of the Steam autostart entry left in ~/.config/autostart is run as a second Steam at login; the two race and one crashes a few seconds after the desktop comes up. This moves any such backup to ~/.local/share/tuxthrottle. Safe to press any time; takes effect at the next login.");
        if (ui.button_flow("Check for unmounted library drives", Btn::OutlineInfo, en)) { check_mounts(a); }
        ui.flow_reset();
        if (!fx_text.empty()) for (const auto& l : split_lines(fx_text)) ui.label(l, l.rfind("x", 0) == 0 ? Tone::Danger : Tone::Normal, ui.S(12), Font::Mono);
        for (const auto& m : fx_unmounted) {
            ui.push_id(m);
            if (ui.button_flow("Mount " + m, Btn::OutlineWarn, en)) {
                auto r = run_argv({"mount", m}, 30000);
                std::string msg = r.code == 0 ? "mounted " + m : "mount " + m + " failed: " + trim(r.out);
                a.log("[Fixes] " + msg);
                fixlog::log_event("mount-now", msg, r.code == 0 ? "info" : "error", a.user);
                check_mounts(a);
            }
            ui.pop_id();
        }
        ui.flow_reset();
        ui.separator();
        ui.label("Recently auto-fixed / diagnosed:", Tone::Muted);
        Json entries = fixlog::read_recent(20, a.user);
        if (entries.array().empty()) ui.label("no fixes logged yet", Tone::Muted, ui.S(12), Font::Mono);
        for (const auto& e : entries.array()) {
            std::time_t ts = static_cast<std::time_t>(e["ts"].as_number());
            std::tm tm;
            localtime_r(&ts, &tm);
            char b[32];
            std::strftime(b, sizeof b, "%Y-%m-%d %H:%M", &tm);
            ui.label(std::string("[") + b + "] " + e["source"].str("?") + ": " + e["message"].str(), Tone::Muted, ui.S(12), Font::Mono);
        }
        ui.card_end();
    }

    // ------------------------------------------------------------ launch options
    std::string lo_string(App& a) { return launchbuild::build(lo, sc_dir, sc_gb, a.user); }

    void launch_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Steam / Lutris launch-options builder");
        ui.wrapped("Tick what you want and copy the string into a game's Properties > Launch Options (Steam) or the wrapper field (Lutris/Heroic). The `%command%` placeholder is where Steam substitutes the game. The persistent-cache toggles write into the folder set in \"Shader / pipeline cache storage\" above.", Tone::Muted);
        struct T { const char* label; bool* v; };
        T toggles[] = {
            {"MangoHud overlay", &lo.mangohud},
            {"Force MangoHud off for this game (MANGOHUD=0 - only matters if something else enables the overlay session-wide)", &lo.mh_off},
            {"MangoHud OpenGL support (MANGOHUD_DLSYM=1 - needed if the overlay doesn't show on an OpenGL game)", &lo.mh_dlsym},
            {"Still show MangoHud when \"Anti-cheat safe\" is on (can still get the LD_PRELOAD flagged - off by default)", &lo.ac_keep_mh},
            {"Feral GameMode", &lo.gamemode},
            {"Render on the NVIDIA dGPU (PRIME offload)", &lo.prime},
            {"Keep NVIDIA shader cache", &lo.nvcache},
            {"NVIDIA threaded optimizations (crashes some Wine/Proton & older OpenGL games - leave off unless it helps)", &lo.nv_threaded},
            {"RADV_PERFTEST=gpl (AMD)", &lo.radv_gpl},
            {"RADV_PERFTEST=gpl,nggc,sam (AMD, replaces the gpl-only toggle)", &lo.radv_full},
            {"Wine shared memory (STAGING_SHARED_MEMORY=1)", &lo.staging_shm},
            {"NVIDIA GL yield (__GL_YIELD=USLEEP)", &lo.gl_yield},
            {"NVIDIA max queued frames (__GL_MaxFramesAllowed=2)", &lo.gl_maxframes},
            {"NVIDIA VRR allowed (__GL_VRR_ALLOWED=1)", &lo.gl_vrr},
            {"Mesa threaded GL (mesa_glthread=true)", &lo.glthread},
            {"Persistent Mesa shader cache (AMD iGPU)", &lo.mesa_cache},
            {"Disable Mesa vsync + threaded GL (AMD)", &lo.no_vsync},
            {"Persistent DXVK state cache (D3D to Vulkan)", &lo.dxvk_cache},
            {"DXVK async shader compile (less stutter, can cause brief visual glitches)", &lo.dxvk_async},
            {"Proton log off", &lo.proton_nolog},
            {"Proton ntsync (PROTON_USE_NTSYNC=1 - lighter sync than esync/fsync for CPU-bound games; needs Proton 9+ & kernel 6.10+, ignored otherwise)", &lo.ntsync},
            {"Anti-cheat safe - no injected Vulkan layers (MangoHud / vkBasalt / all implicit layers off) + Proton BattlEye Runtime (GTA Online)", &lo.anticheat},
        };
        int ti = 0;
        for (auto& t : toggles) { ui.push_id(ti++); ui.toggle(t.label, t.v); ui.pop_id(); }
        ui.toggle("gamescope", &lo.gamescope);
        ui.same_line();
        ui.label_c("W");
        ui.same_line(ui.S(4));
        ui.input("gsw", &lo.w, ui.S(80));
        ui.same_line();
        ui.label_c("H");
        ui.same_line(ui.S(4));
        ui.input("gsh", &lo.h, ui.S(80));
        ui.same_line();
        ui.label_c("fps cap");
        ui.same_line(ui.S(4));
        ui.input("gsf", &lo.fps, ui.S(80));
        ui.wrapped("If a game won't launch or crashes on start, clear these options and add them back a few at a time - the usual offenders are NVIDIA threaded optimizations, then DXVK async, then gamescope.", Tone::Muted);
        std::string out = lo_string(a);
        std::string tmp = out;
        ui.input("lo_out", &tmp, ui.avail_w() - ui.S(100));
        ui.same_line();
        if (ui.button("Copy", Btn::Info)) a.copy_text(out, "launch options");
        ui.tooltip("Copy this string. Paste it into Steam > the game > Properties > Launch Options (or Lutris/Heroic's wrapper field).");
        ui.flow_reset();
        if (ui.button_flow("Apply to every game", Btn::OutlineWarn, en)) ui.open_popup("confirm:lo-apply");
        ui.tooltip("Write this string into the Launch Options of every installed Steam game (localconfig.vdf). Steam must be CLOSED first - it rewrites its config on exit. Each file is backed up (*.tuxthrottle-bak-*). Restart Steam afterwards.");
        ui.flow_reset();
        ui.toggle("only games with no options yet", &lo_only_empty);
        ui.same_line();
        ui.toggle("keep per-game custom options", &lo_keep_custom);
        ui.flow_reset();
        if (ui.button_flow("Show custom", Btn::OutlineInfo, en)) a.run_user_ctl_task("Game Tools", "launch options -> per-game custom options", "launchopts list --custom");
        if (ui.button_flow("Remove from every game", Btn::Outline, en)) ui.open_popup("confirm:lo-remove");
        ui.flow_reset();
        if (ui.button_flow("Lutris: add shader-cache env to every game", Btn::OutlineWarn, en)) lutris_env(a, "apply");
        if (ui.button_flow("Lutris: remove them", Btn::Outline, en)) lutris_env(a, "remove");
        if (ui.button_flow("Lutris: show status", Btn::OutlineInfo, en)) lutris_env(a, "list");
        ui.flow_reset();
        ui.toggle("Disable Wine Wayland", &heroic_nowayland);
        ui.flow_reset();
        if (ui.button_flow("Apply to every Heroic game", Btn::OutlineWarn, en)) { lo_target = "heroic"; lo_verb = "set-all"; ui.open_popup("confirm:lo-launcher"); }
        if (ui.button_flow("Remove from every Heroic game", Btn::Outline, en)) { lo_target = "heroic"; lo_verb = "remove-token"; ui.open_popup("confirm:lo-launcher"); }
        if (ui.button_flow("Apply to every Lutris game", Btn::OutlineWarn, en)) { lo_target = "lutris"; lo_verb = "set-all"; ui.open_popup("confirm:lo-launcher"); }
        if (ui.button_flow("Remove from every Lutris game", Btn::Outline, en)) { lo_target = "lutris"; lo_verb = "remove-token"; ui.open_popup("confirm:lo-launcher"); }
        if (ui.button_flow("dGPU on + gamescope off (Heroic)", Btn::OutlineInfo, en)) a.run_user_ctl_task("Game Tools", "Heroic gamescope + PRIME crash fix", "heroicopts fix");
        ui.flow_reset();
        ui.card_end();

        bool only = lo_only_empty, keep = lo_keep_custom && !only;
        if (out.empty()) out = "%command%";
        std::string blob = b64(trim(out));
        if (a.confirm("lo-apply", "Apply to every game",
                      std::string(only ? "Write this launch-options string into EVERY installed Steam game that has none yet:" : keep ? "Write this launch-options string into EVERY installed Steam game still on the default string (games with their own options are left alone):" : "Write this launch-options string into EVERY installed Steam game, REPLACING whatever each one has now:") +
                          "\n\n" + out + "\n\nSteam must be closed first - it rewrites its config on exit. Every localconfig.vdf is backed up. Restart Steam afterwards.", "Apply"))
            a.run_user_ctl_task("Game Tools", "launch options -> every installed game", "launchopts set-all --b64 " + blob + (only ? " --only-empty" : keep ? " --keep-custom" : ""));
        if (a.confirm("lo-remove", "Remove from every game", "Strip this token out of every installed Steam game's Launch Options that contains it (the rest of each game's string is left as-is):\n\n" + out + "\n\nSteam must be closed first - it rewrites its config on exit. Every localconfig.vdf is backed up.", "Remove"))
            a.run_user_ctl_task("Game Tools", "launch options -> remove from every installed game", "launchopts remove-token --b64 " + blob);
        std::string Which = lo_target.empty() ? "" : std::string(1, static_cast<char>(std::toupper(lo_target[0]))) + lo_target.substr(1);
        if (a.confirm("lo-launcher", Which + " games", std::string(lo_verb == "set-all" ? "Write" : "Strip") + " this launch-options string " + (lo_verb == "set-all" ? "into" : "out of") + " EVERY " + Which + " game config (and the global one):\n\n" + out + "\n\nClose " + Which + " first. Every config is backed up.", "Go ahead")) {
            std::string flag = lo_verb == "set-all" && lo_only_empty ? " --only-empty" : "";
            if (lo_target == "heroic" && heroic_nowayland) flag += " --no-wayland";
            a.run_user_ctl_task("Game Tools", "launch options -> every " + Which + " game", lo_target + "opts " + lo_verb + " --b64 " + blob + flag);
        }
    }
    std::string lo_target, lo_verb;

    void lutris_env(App& a, const std::string& action) {
        if (action == "list") { a.run_user_ctl_task("Game Tools", "Lutris env vars -> list", "lutris-env list"); return; }
        lutris_pending = action;
        a.ui.open_popup("confirm:lutris-env");
    }
    std::string lutris_pending;

    // ------------------------------------------------------------ fps limit
    std::string fps_env_str() {
        int fps = is_digits(fps_copyval) && std::atoi(trim(fps_copyval).c_str()) > 0 ? std::atoi(trim(fps_copyval).c_str()) : 60;
        return fps_method == 0 ? "STRANGLE_FPS=" + std::to_string(fps) + " ENABLE_VK_LAYER_TORKEL104_libstrangle=1" : "DXVK_FRAME_RATE=" + std::to_string(fps);
    }

    void fps_apply(App& a, const std::string& verb) {
        Json sel = Json::arr();
        for (auto& r : fps_rows) {
            if (!r.tick) continue;
            std::string v = trim(r.fps);
            if (verb == "apply" && !(is_digits(v) && std::atoi(v.c_str()) > 0)) { a.log("[Game Tools] FPS limit: '" + r.game["name"].str() + "' needs a whole-number cap"); return; }
            Json o = Json::obj();
            o.set("launcher", r.game["launcher"].str()); o.set("id", r.game["id"].str()); o.set("fps", is_digits(v) ? std::atoi(v.c_str()) : 0);
            sel.push(o);
        }
        if (sel.array().empty()) { a.log("[Game Tools] FPS limit: no titles ticked"); return; }
        fps_verb = verb;
        fps_sel_blob = b64(sel.dump());
        fps_sel_n = static_cast<int>(sel.array().size());
        a.ui.open_popup("confirm:fps-go");
    }
    std::string fps_verb, fps_sel_blob;
    int fps_sel_n = 0;

    void fps_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Frame-rate limit (libstrangle / DXVK) - Heroic, Lutris and Steam titles");
        ui.wrapped("Tick the titles that should get a cap and type the FPS next to each (the recommendation is prefilled; see config/fps_limits.json for why). Apply writes the variables into that title's own settings: Heroic / Lutris environment, Steam launch options. A cap that differs from the recommendation is remembered for that title. libstrangle must be installed first (Gaming apps: 'libstrangle'); DXVK's own limiter needs nothing. Close the launcher first.", Tone::Muted);
        ui.radio("libstrangle", &fps_method, 0);
        ui.same_line(ui.S(12));
        ui.radio("DXVK_FRAME_RATE", &fps_method, 1);
        ui.same_line(ui.S(16));
        bool have = !which("strangle").empty();
        ui.label(have ? "libstrangle: installed" : "libstrangle: not installed (Gaming apps > libstrangle)", have ? Tone::Success : Tone::Warning);
        ui.label_c("Variables for");
        ui.same_line();
        ui.input("fpsval", &fps_copyval, ui.S(70));
        ui.same_line();
        ui.label_c("FPS:");
        ui.same_line();
        std::string env = fps_env_str(), tmp = env;
        ui.input("fpsenv", &tmp, ui.avail_w() - ui.S(100));
        ui.same_line();
        if (ui.button("Copy", Btn::Info)) a.copy_text(env, "FPS limit env vars");
        ui.label_c("Source:", Tone::Muted);
        ui.same_line();
        ui.segmented("fpssrc", &fps_src, {"All", "Heroic", "Lutris", "Steam"});
        ui.flow_reset();
        if (ui.button_flow("Reload titles", Btn::Info, en)) fps_reload(a);
        if (ui.button_flow("Tick all", Btn::Outline)) for (auto& r : fps_rows) r.tick = true;
        if (ui.button_flow("Tick none", Btn::Outline)) for (auto& r : fps_rows) r.tick = false;
        ui.flow_reset();
        ui.label_c("Set ticked to:");
        ui.same_line();
        ui.input("fpsfill", &fps_fill, ui.S(70));
        ui.same_line();
        if (ui.button("Fill", Btn::Outline) && is_digits(fps_fill)) for (auto& r : fps_rows) if (r.tick) r.fps = trim(fps_fill);
        ui.same_line(ui.S(16));
        if (ui.button("Apply to ticked", Btn::OutlineWarn, 0, en)) fps_apply(a, "apply");
        ui.same_line();
        if (ui.button("Remove from ticked", Btn::Outline, 0, en)) fps_apply(a, "remove");
        int idx = 0;
        for (auto& r : fps_rows) {
            if (fps_src > 0) { static const char* nm[] = {"", "heroic", "lutris", "steam"}; if (r.game["launcher"].str() != nm[fps_src]) { ++idx; continue; } }
            ui.push_id(idx++);
            ui.checkbox("", &r.tick);
            ui.same_line();
            std::string l = r.game["launcher"].str();
            if (!l.empty()) l[0] = static_cast<char>(std::toupper(l[0]));
            ui.label_c(l, Tone::Muted, Font::Regular, ui.S(60));
            ui.same_line();
            ui.label_c(ui.g.ellipsize(r.game["name"].str(), ui.S(360), ui.base_size(), Font::Regular), Tone::Normal, Font::Regular, ui.S(360));
            ui.same_line();
            ui.input("fps", &r.fps, ui.S(70));
            ui.same_line();
            const Json& rec = r.game["recommended"];
            ui.label_c(rec.is_number() ? "recommended " + format_double(rec.as_number(), 0) : "no recommendation", rec.is_number() ? Tone::Info : Tone::Muted);
            if (r.game["note"].is_string()) ui.tooltip(r.game["note"].str());
            const Json& cur = r.game["current"];
            std::string now = cur["STRANGLE_FPS"].is_string() ? cur["STRANGLE_FPS"].str() : cur["DXVK_FRAME_RATE"].str();
            if (!now.empty()) { ui.same_line(); ui.label_c("now capped at " + now, Tone::Warning); }
            ui.pop_id();
        }
        ui.label(fps_status, Tone::Muted);
        ui.card_end();
        if (a.confirm("fps-go", "Frame-rate limit", std::string(fps_verb == "apply" ? "Write FPS caps into " : "Remove FPS caps from ") + std::to_string(fps_sel_n) + " ticked title(s)?\n\nClose Heroic / Lutris / Steam first (the ones involved). Every changed file is backed up.", "Go ahead"))
            a.run_user_ctl_task("Game Tools", "FPS limit -> " + fps_verb, "fpslimit " + fps_verb + " --sel " + fps_sel_blob + " --method " + (fps_method == 0 ? "strangle" : "dxvk"));
        if (a.confirm("lutris-env", "Lutris environment variables", std::string(lutris_pending == "apply" ? "Add the shader-cache environment variables to" : "Remove the shader-cache environment variables from") + " every installed Lutris Wine game?\n\nClose any open Lutris Configure dialog first - saving it would write the old values back. Every game yml is backed up.", "Go ahead"))
            a.run_user_ctl_task("Game Tools", "Lutris env vars -> " + lutris_pending, "lutris-env " + lutris_pending);
    }

    // ------------------------------------------------------------ dgpu per game
    void dgpu_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("Dedicated GPU per game");
        ui.wrapped("Force single Steam games onto the dedicated GPU, whatever their Launch Options say. Flip as many as you like, then press Apply once. A game with the `mangohud` hook picks the change up at its next launch; for the others Apply edits their Launch Options, which needs Steam closed once.", Tone::Muted);
        ui.label_c("Filter:");
        ui.same_line();
        ui.input("dgf", &dg_filter, ui.S(220));
        ui.same_line(ui.S(12));
        ui.toggle("only games not on the dedicated GPU", &dg_only_off);
        ui.same_line();
        if (ui.button("Reload", Btn::OutlineInfo, 0, en)) dg_reload(a);
        int n = static_cast<int>(dg_pending.size());
        if (ui.button(n ? "Apply changes (" + std::to_string(n) + ")" : "Apply changes", Btn::Success, 0, n > 0 && en)) {
            Json plan = Json::obj();
            plan.set("on", Json::obj());
            plan.set("off", Json::arr());
            Json on = Json::arr(), off = Json::arr();
            for (auto& [id, v] : dg_pending) (v ? on : off).push(id);
            plan.set("dgpu_on", on);
            plan.set("dgpu_off", off);
            std::string blob = b64(plan.dump());
            dg_pending.clear();
            a.run_user_ctl_task("Game Tools", "Dedicated GPU - apply " + std::to_string(n) + " game change(s)", "mangohud-games apply --plan " + blob);
        }
        ui.same_line();
        if (ui.button("Discard", Btn::Outline, 0, n > 0)) dg_pending.clear();
        std::string needle = lower(trim(dg_filter));
        std::vector<const DgGame*> shown;
        for (const auto& g : dg_games) {
            if (!needle.empty() && lower(g.name).find(needle) == std::string::npos && needle != g.appid) continue;
            if (g.dgpu && dg_only_off) continue;
            shown.push_back(&g);
        }
        int on = 0, waiting = 0;
        for (const auto& g : dg_games) { if (g.dgpu) ++on; if (g.dgpu != g.active) ++waiting; }
        std::string msg = dg_status.empty() ? std::to_string(dg_games.size()) + " Steam games, " + std::to_string(on) + " on the dedicated GPU" + (waiting ? ", " + std::to_string(waiting) + " waiting for a Steam-closed Apply" : "") : dg_status;
        if (dg_games.empty() && dg_status.empty()) msg = "no installed Steam games found";
        else if (shown.size() > 40) msg += " - showing the first 40 of " + std::to_string(shown.size()) + "; type in the filter";
        ui.label(msg, Tone::Muted);
        int cnt = 0;
        for (const DgGame* g : shown) {
            if (++cnt > 40) break;
            ui.push_id(g->appid);
            bool want = dg_pending.count(g->appid) ? dg_pending[g->appid] : g->dgpu;
            if (ui.checkbox("", &want)) { if (want == g->dgpu) dg_pending.erase(g->appid); else dg_pending[g->appid] = want; }
            ui.same_line();
            ui.label_c(ui.g.ellipsize(g->name, ui.S(380), ui.base_size(), Font::Regular), Tone::Normal, Font::Regular, ui.S(380));
            ui.same_line();
            ui.label_c(g->appid, Tone::Muted, Font::Regular, ui.S(80));
            if (dg_pending.count(g->appid)) { ui.same_line(); ui.label_c("pending", Tone::Warning); }
            else if (g->dgpu != g->active) { ui.same_line(); ui.label_c("waiting - close Steam once, then Apply", Tone::Warning); }
            else if (g->dgpu && g->hook) { ui.same_line(); ui.label_c("live", Tone::Muted); }
            ui.pop_id();
        }
        ui.card_end();
    }

    // ------------------------------------------------------------ sessions
    static std::string when_str(double ended) {
        if (ended <= 0) return "?";
        std::time_t t = static_cast<std::time_t>(ended);
        std::tm tm;
        localtime_r(&t, &tm);
        char b[32];
        std::strftime(b, sizeof b, "%b %d %H:%M", &tm);
        return b;
    }

    void session_boxes(App& a) {
        Ui& ui = a.ui;
        ui.card_begin("Last game session");
        Json s = a.read_state("last_session.json");
        if (s.object().empty()) ui.wrapped("No session recorded yet. Turn on per-game auto-profiles (Profiles tab) and the daemon logs a summary here when a mapped game exits.", Tone::Muted);
        else {
            std::vector<std::string> p{s["game"].str("?") + " - " + format_double(s["duration_s"].as_number() / 60, 0) + " min  (" + when_str(s["ended"].as_number()) + ")",
                                       "CPU max " + (s["cpu_temp_max_c"].is_number() ? format_double(s["cpu_temp_max_c"].as_number(), 0) : "?") + " \xC2\xB0""C", "GPU max " + (s["gpu_temp_max_c"].is_number() ? format_double(s["gpu_temp_max_c"].as_number(), 0) : "?") + " \xC2\xB0""C"};
            if (s["cpu_clock_avg_ghz"].is_number()) p.push_back("avg CPU " + format_double(s["cpu_clock_avg_ghz"].as_number(), 2) + " GHz");
            if (s["gpu_clock_avg_mhz"].is_number()) p.push_back("avg GPU " + format_double(s["gpu_clock_avg_mhz"].as_number(), 0) + " MHz");
            if (s["throttle_pct"].is_number()) p.push_back("thermally throttled " + format_double(s["throttle_pct"].as_number(), 0) + "% of the session");
            ui.wrapped(join(p, "   \xC2\xB7   "), Tone::Muted);
        }
        ui.card_end();
        ui.card_begin("Session history");
        ui.wrapped("Every game session the daemon logged (newest first, last 50). Needs per-game auto-profiles on (Profiles tab).", Tone::Muted);
        auto txt = read_file(a.cfg_dir() + "/sessions.jsonl");
        std::vector<Json> rows;
        for (const auto& ln : split_lines(txt.value_or(""))) { try { Json j = Json::parse(ln); if (j.is_object()) rows.push_back(j); } catch (...) {} }
        if (rows.empty()) ui.label("(nothing logged yet)", Tone::Muted);
        else {
            char hdr[200];
            std::snprintf(hdr, sizeof hdr, "%-14s %-22s %-6s %-7s %-7s %-8s %-8s %-10s", "when", "game", "mins", "CPU C", "GPU C", "CPU GHz", "GPU MHz", "throttled");
            ui.label(hdr, Tone::Muted, ui.S(12), Font::MonoBold);
            int c = 0;
            for (auto it = rows.rbegin(); it != rows.rend() && c < 50; ++it, ++c) {
                auto num = [&](const char* k, int d) { return (*it)[k].is_number() ? format_double((*it)[k].as_number(), d) : std::string("-"); };
                char line[240];
                std::snprintf(line, sizeof line, "%-14s %-22s %-6s %-7s %-7s %-8s %-8s %-10s", when_str((*it)["ended"].as_number()).c_str(), (*it)["game"].str("?").substr(0, 21).c_str(), format_double((*it)["duration_s"].as_number() / 60, 0).c_str(),
                              num("cpu_temp_max_c", 0).c_str(), num("gpu_temp_max_c", 0).c_str(), num("cpu_clock_avg_ghz", 2).c_str(), num("gpu_clock_avg_mhz", 0).c_str(), ((*it)["throttle_pct"].is_number() ? format_double((*it)["throttle_pct"].as_number(), 0) : "0") .append("%").c_str());
                ui.label(line, Tone::Normal, ui.S(12), Font::Mono);
            }
        }
        ui.card_end();
    }

    // ------------------------------------------------------------ bench
    void bench_run(App& a, const std::string& args) {
        a.run_bg([&a, args] {
            auto r = run_shell(a.user_ctl("bench " + args));
            for (const auto& l : split_lines(r.out)) a.log(l);
        });
    }

    void bench_box(App& a, bool en) {
        Ui& ui = a.ui;
        (void)en;
        ui.card_begin("Benchmark A/B - did that tweak help?");
        ui.wrapped("Compares two MangoHud frame logs: average FPS, 1% / 0.1% lows, p99 frametime, stutters, load, temps and power, with the % change. 1) Copy the logging prefix into a game's launch options (before '%command%'), 2) play the same scene for the logged time, 3) change one thing, 4) play it again, 5) pick the older log as A and the newer as B.", Tone::Muted);
        ui.label_c("Log length (s):", Tone::Muted);
        ui.same_line();
        ui.spinbox("bdur", &bn_dur, 10, 600, 10, ui.S(130));
        ui.same_line();
        if (ui.button("Copy logging prefix", Btn::Success)) {
            int secs = std::max(10, bn_dur);
            std::string user_cmd = a.user_ctl("bench env --duration " + std::to_string(secs));
            a.run_bg([&a, user_cmd, secs] {
                auto r = run_shell(user_cmd);
                std::string out = trim(r.out);
                if (!out.empty()) { SDL_SetClipboardText((out + " %command%").c_str()); a.log("Copied: " + out + " %command%"); }
            });
        }
        ui.tooltip("Copies MANGOHUD_CONFIG=... mangohud for the launch options. Logging starts 5 s after the game does.");
        ui.same_line();
        if (ui.button("Latest logs", Btn::OutlineInfo)) bench_run(a, "latest");
        for (int w = 0; w < 2; ++w) {
            std::string& v = w == 0 ? bn_a : bn_b;
            ui.push_id(w);
            ui.label_c(w == 0 ? "A (before):" : "B (after):", Tone::Muted, Font::Regular, ui.S(90));
            ui.same_line();
            ui.input("p", &v, ui.S(520));
            ui.same_line();
            if (ui.button("Browse", Btn::Outline)) {
                std::string start = home_of(a.user) + "/.local/share/tuxthrottle/bench-logs";
                if (!path_exists(start)) start = home_of(a.user);
                a.open_file_dialog(w == 0 ? "bn-a" : "bn-b", 0, start, "", ".csv");
            }
            ui.pop_id();
        }
        std::string p;
        if (a.file_result("bn-a", &p)) bn_a = p;
        if (a.file_result("bn-b", &p)) bn_b = p;
        if (ui.button("Compare A vs B", Btn::Success)) {
            if (trim(bn_a).empty() || trim(bn_b).empty()) a.log("Pick both log A and log B first.");
            else bench_run(a, "compare " + shell_quote(trim(bn_a)) + " " + shell_quote(trim(bn_b)));
        }
        ui.tooltip("Prints the side-by-side table in the log.");
        ui.same_line();
        if (ui.button("Stats for A", Btn::OutlineInfo)) {
            if (trim(bn_a).empty()) a.log("Pick log A first."); else bench_run(a, "stats " + shell_quote(trim(bn_a)));
        }
        ui.card_end();
    }

    // ------------------------------------------------------------ gpu drivers
    std::string gd_cmd(const std::string& sub, bool flags = true) {
        std::string f;
        if (flags && !gd_32) f += " --no-32bit";
        if (flags && !gd_pw) f += " --no-power";
        return "gpu-drivers " + sub + f;
    }

    void gpudrv_box(App& a, bool en) {
        Ui& ui = a.ui;
        ui.card_begin("GPU drivers - one-click install (NVIDIA, AMD, Intel, hybrid)");
        ui.wrapped("Detects every GPU, works out the layout (NVIDIA only, AMD only, hybrid laptop, ...) and installs the right driver stack for this distro: NVIDIA proprietary or open modules, Mesa/Vulkan/VA-API, 32-bit libraries for Steam, DRM modesetting, suspend VRAM keep, dGPU power-down on hybrid laptops and the 'run on discrete GPU' helper. 'Show plan' only lists the commands; 'Install' snapshots first (Btrfs) and stops at the first error; 'Roll back' undoes the last install. Secure Boot needs a manual key enrolment step, which the plan spells out.", Tone::Muted);
        ui.toggle("32-bit libraries (Steam/Proton)", &gd_32);
        ui.same_line(ui.S(16));
        ui.toggle("dGPU power-down (hybrid laptops)", &gd_pw);
        if (ui.button("Show plan (dry run)", Btn::Info, 0, en)) a.run_shell_task("GPU drivers: plan", shell_quote(ctl_exe()) + " " + gd_cmd("plan"));
        ui.tooltip("Detect GPUs and print every command that would run. Changes nothing.");
        ui.same_line();
        if (ui.button("Install drivers", Btn::Warning, 0, en)) ui.open_popup("confirm:gd-install");
        ui.tooltip("Run the plan. Needs network; a reboot is required afterwards.");
        ui.same_line();
        if (ui.button("Roll back last install", Btn::OutlineDanger, 0, en)) ui.open_popup("confirm:gd-roll");
        ui.card_end();
        if (a.confirm("gd-install", "Install GPU drivers", "This installs packages and changes boot/initramfs settings. A Btrfs snapshot is taken first when available.\n\nTip: run 'Show plan' first and read it.\n\nContinue?", "Install")) a.run_shell_task("GPU drivers: install", shell_quote(ctl_exe()) + " " + gd_cmd("apply --yes"));
        if (a.confirm("gd-roll", "Roll back", "Undo the last GPU driver install?", "Roll back")) a.run_shell_task("GPU drivers: roll back", shell_quote(ctl_exe()) + " " + gd_cmd("rollback --yes", false));
    }
};
}  // namespace

std::unique_ptr<Page> make_gametools() { return std::make_unique<GameTools>(); }

}  // namespace tt::app
