// tuxthrottle-tray: StatusNotifierItem tray icon (own D-Bus client, no Qt) with a popup menu, a mini always-on-top
// overlay strip, Game Mode toggle, power/fan quick actions, crash-watch notifications and the MangoHud status line.
#include <fcntl.h>
#include <poll.h>
#include <sys/file.h>
#include <unistd.h>

#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>

#include "../dbus.hpp"
#include "../devices.hpp"
#include "../gamecfg.hpp"
#include "../mangohud.hpp"
#include "../sensors.hpp"
#include "../ui/icon.hpp"
#include "../ui/ui.hpp"
#include "../userenv.hpp"
#include "../util.hpp"

using namespace tt;
using namespace tt::ui;

namespace {

const char* kAppName = "TuxThrottle";
const char* kBlurb = "Linux power & gaming tuning";
const char* kAuthor = "by BeanGreen247";
const char* kProjectUrl = "https://github.com/BeanGreen247/tuxthrottle";

std::atomic<bool> g_quit{false};
void on_signal(int) { g_quit = true; }

// ------------------------------------------------------------------ live readings
struct Readings {
    std::string cpu = "CPU: ...", igpu = "iGPU: ...", dgpu = "dGPU: ...", tip_cpu, tip_dgpu;
    bool game_mode = false, rapl_ok = true, daemon = false;
    std::string backends;
    int boost_pct = 0;
    double mini_freq = 0, mini_temp = 0;
};

Readings read_all() {
    Readings r;
    auto t = sensors::cpu_temp_c();
    auto f = sensors::cpu_freq_ghz();
    auto p = sensors::cpu_power_watts();
    std::string temp = t ? format_double(*t, 0) + " C" : "n/a", freq = f ? format_double(*f, 2) + " GHz" : "n/a";
    r.cpu = "CPU: " + freq + ", " + temp + (p ? ", " + format_double(*p, 1) + " W" : "");
    auto ig = sensors::igpu_values();
    r.igpu = "iGPU: " + (ig.clock_mhz ? std::to_string(*ig.clock_mhz) + " MHz" : std::string("?")) + ", " + (ig.temp_c ? format_double(*ig.temp_c, 0) + " C" : std::string("?"));
    auto dg = sensors::dgpu_values();
    std::string dtxt = !dg.clock_mhz ? "n/a (asleep or no nvidia-smi)" : std::to_string(*dg.clock_mhz) + " MHz, " + std::to_string(dg.temp_c.value_or(0)) + " C, " + std::to_string(dg.util_pct.value_or(0)) + "%";
    r.dgpu = "dGPU: " + dtxt;
    r.tip_cpu = temp;
    r.tip_dgpu = dtxt;
    r.game_mode = sensors::game_mode_state();
    r.rapl_ok = sensors::rapl_permissions_ok();
    r.daemon = sensors::daemon_running();
    r.backends = sensors::backend_summary();
    auto boosts = sensors::get_fan_boost();
    if (!boosts.empty()) r.boost_pct = static_cast<int>(std::lround(*std::max_element(boosts.begin(), boosts.end()) / 255.0 * 100));
    r.mini_freq = f.value_or(0);
    r.mini_temp = t.value_or(0);
    return r;
}

// ------------------------------------------------------------------ tray icon pixmap (ARGB32, network order)
std::vector<uint8_t> make_icon(int n) {
    int w = n, h = n;
    std::vector<uint8_t> rgba;
    app_icon_png(n, &w, &h, &rgba);
    if (w != n) rgba = app_icon_rgba(n);   // the SNI pixmap list advertises n x n, so only an exact-size PNG is used

    std::vector<uint8_t> px(rgba.size());
    for (size_t i = 0; i < rgba.size(); i += 4) { px[i] = rgba[i + 3]; px[i + 1] = rgba[i]; px[i + 2] = rgba[i + 1]; px[i + 3] = rgba[i + 2]; }
    return px;
}

// ------------------------------------------------------------------ StatusNotifierItem
class Sni {
public:
    std::function<void()> on_activate, on_secondary;
    std::function<void(int, int)> on_context;

    bool start() {
        if (!bus_.connect()) return false;
        std::string name = "org.kde.StatusNotifierItem-" + std::to_string(getpid()) + "-1";
        if (!bus_.request_name(name)) return false;
        service_ = name;
        bus_.add_match("type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.kde.StatusNotifierWatcher'");
        register_item();
        return true;
    }
    int fd() const { return bus_.fd(); }

    void set_daemon(bool d) {
        if (d == daemon_) return;
        daemon_ = d;
        bus_.emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewOverlayIcon");
    }
    void set_tooltip(const std::string& title, const std::string& text) {
        if (title == title_ && text == text_) return;
        title_ = title; text_ = text;
        bus_.emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip");
    }

    // returns false when the bus connection died
    bool service() {
        if (!bus_.pump()) return false;
        for (auto& m : bus_.take()) handle(m);
        return true;
    }

private:
    void register_item() {
        dbus::Message m, r;
        m.dest = "org.kde.StatusNotifierWatcher"; m.path = "/StatusNotifierWatcher"; m.iface = "org.kde.StatusNotifierWatcher"; m.member = "RegisterStatusNotifierItem";
        dbus::Writer w;
        w.str(service_);
        m.signature = "s"; m.body = w.data();
        bus_.call(m, &r, 1500);
    }

    void pixmaps(dbus::Writer& w) {   // a(iiay)
        auto arr = w.begin_array(8);
        for (int n : {64, 32}) {
            w.align(8);
            w.i32(n); w.i32(n);
            auto px = make_icon(n);
            w.u32(static_cast<uint32_t>(px.size()));
            w.bytes(px);
        }
        w.end_array(arr);
    }

    bool prop(const std::string& name, dbus::Writer& w) {   // writes a variant
        if (name == "Category") w.variant_str("ApplicationStatus");
        else if (name == "Id") w.variant_str("tuxthrottle");
        else if (name == "Title") w.variant_str(kAppName);
        else if (name == "Status") w.variant_str("Active");
        else if (name == "WindowId") w.variant_u32(0);
        else if (name == "IconName") w.variant_str("");
        else if (name == "IconThemePath") w.variant_str("");
        else if (name == "OverlayIconName" || name == "AttentionIconName" || name == "AttentionMovieName") w.variant_str("");
        else if (name == "ItemIsMenu") w.variant_bool(false);
        else if (name == "Menu") w.variant_obj("/NO_DBUSMENU");
        else if (name == "IconPixmap") { w.sig("a(iiay)"); pixmaps(w); }
        else if (name == "OverlayIconPixmap") {
            w.sig("a(iiay)");
            auto a = w.begin_array(8);
            w.align(8);
            const int n = 16;
            w.i32(n); w.i32(n);
            std::vector<uint8_t> px(static_cast<size_t>(n) * n * 4, 0);
            uint8_t cr = daemon_ ? 0x3f : 0xd2, cg = daemon_ ? 0xb9 : 0x99, cb = daemon_ ? 0x50 : 0x22;   // green = daemon, amber = direct
            for (int y = 0; y < n; ++y)
                for (int x = 0; x < n; ++x) {
                    double dx = x + 0.5 - n / 2.0, dy = y + 0.5 - n / 2.0, d = std::sqrt(dx * dx + dy * dy);
                    double al = std::clamp(n / 2.0 - 0.5 - d, 0.0, 1.0);
                    size_t i = (static_cast<size_t>(y) * n + static_cast<size_t>(x)) * 4;
                    px[i] = static_cast<uint8_t>(al * 255); px[i + 1] = cr; px[i + 2] = cg; px[i + 3] = cb;
                }
            w.u32(static_cast<uint32_t>(px.size()));
            w.bytes(px);
            w.end_array(a);
        }
        else if (name == "AttentionIconPixmap") { w.sig("a(iiay)"); auto a = w.begin_array(8); w.end_array(a); }
        else if (name == "ToolTip") {
            w.sig("(sa(iiay)ss)");
            w.align(8);
            w.str("");
            auto a = w.begin_array(8);
            w.end_array(a);
            w.str(title_);
            w.str(text_);
        } else return false;
        return true;
    }

    static const std::vector<std::string>& prop_names() {
        static const std::vector<std::string> v = {"Category", "Id", "Title", "Status", "WindowId", "IconName", "IconPixmap", "OverlayIconName", "OverlayIconPixmap", "AttentionIconName", "AttentionIconPixmap", "AttentionMovieName", "ToolTip", "ItemIsMenu", "Menu"};
        return v;
    }

    void handle(const dbus::Message& m) {
        if (m.type == dbus::kSignal) {
            if (m.member == "NameOwnerChanged") {
                dbus::Reader r(m.body.data(), m.body.size());
                std::string n = r.str(), old_owner = r.str(), new_owner = r.str();
                if (n == "org.kde.StatusNotifierWatcher" && !new_owner.empty()) register_item();
            }
            return;
        }
        if (m.type != dbus::kMethodCall) return;
        dbus::Reader r(m.body.data(), m.body.size());
        if (m.iface == "org.freedesktop.DBus.Properties") {
            if (m.member == "Get") {
                std::string iface = r.str(), name = r.str();
                dbus::Writer w;
                if (prop(name, w)) bus_.reply_ok(m, "v", w.data()); else bus_.reply_error(m, "org.freedesktop.DBus.Error.UnknownProperty", name);
            } else if (m.member == "GetAll") {
                dbus::Writer w;
                auto arr = w.begin_array(8);
                for (const auto& n : prop_names()) { w.align(8); w.str(n); prop(n, w); }
                w.end_array(arr);
                bus_.reply_ok(m, "a{sv}", w.data());
            } else bus_.reply_ok(m, "", {});
        } else if (m.iface == "org.kde.StatusNotifierItem") {
            int x = 0, y = 0;
            if (m.signature == "ii") { x = r.i32(); y = r.i32(); }
            if (m.member == "Activate") { if (on_activate) on_activate(); }
            else if (m.member == "SecondaryActivate") { if (on_secondary) on_secondary(); }
            else if (m.member == "ContextMenu") { if (on_context) on_context(x, y); }
            bus_.reply_ok(m, "", {});
        } else if (m.iface == "org.freedesktop.DBus.Peer") {
            bus_.reply_ok(m, "", {});
        } else if (m.iface == "org.freedesktop.DBus.Introspectable") {
            dbus::Writer w;
            w.str("<node><interface name=\"org.kde.StatusNotifierItem\"><method name=\"Activate\"><arg type=\"i\" direction=\"in\"/><arg type=\"i\" direction=\"in\"/></method>"
                  "<method name=\"SecondaryActivate\"><arg type=\"i\" direction=\"in\"/><arg type=\"i\" direction=\"in\"/></method>"
                  "<method name=\"ContextMenu\"><arg type=\"i\" direction=\"in\"/><arg type=\"i\" direction=\"in\"/></method>"
                  "<signal name=\"NewToolTip\"/></interface></node>");
            bus_.reply_ok(m, "s", w.data());
        } else bus_.reply_error(m, "org.freedesktop.DBus.Error.UnknownMethod", m.member);
    }

    dbus::Connection bus_;
    std::string service_, title_, text_;
    bool daemon_ = false;
};

// ------------------------------------------------------------------ windows
struct MenuItem {
    enum Kind { Header, Action, Info, Check, Sep, Sub } kind = Action;
    std::string text;
    bool checked = false, bold = false, enabled = true;
    std::function<void()> fn;
};

class Popup {
public:
    bool open() const { return gfx_ != nullptr; }
    uint32_t id() const { return gfx_ ? gfx_->window_id() : 0; }

    void show(std::vector<MenuItem> items, int x, int y) {
        close();
        items_ = std::move(items);
        gfx_ = std::make_unique<Gfx>();
        // create with a temporary size, resize once we know the scale + metrics
        if (!gfx_->init("TuxThrottle menu", 320, 200, true, true)) { gfx_.reset(); return; }
        ui_ = std::make_unique<Ui>(*gfx_);
        Theme t = find_theme(kDefaultTheme);
        ui_->set_theme(t);
        int w = ui_->S(300), h = ui_->S(12);
        for (const auto& it : items_) h += item_h(it);
        SDL_SetWindowSize(gfx_->window(), w, h);
        SDL_Rect dr;
        if (SDL_GetDisplayBounds(0, &dr) != 0) dr = {0, 0, 1920, 1080};
        int px = std::clamp(x, dr.x, dr.x + dr.w - w), py = y + h > dr.y + dr.h ? y - h : y;
        py = std::clamp(py, dr.y, dr.y + dr.h - h);
        SDL_SetWindowPosition(gfx_->window(), px, py);
        SDL_ShowWindow(gfx_->window());
        SDL_RaiseWindow(gfx_->window());
        SDL_SetWindowInputFocus(gfx_->window());
        opened_at_ = SDL_GetTicks();
        got_focus_ = false;
    }

    void close() { ui_.reset(); gfx_.reset(); items_.clear(); }

    void handle(const SDL_Event& e) {
        if (!gfx_) return;
        if (e.type == SDL_WINDOWEVENT && e.window.windowID == id()) {
            if (e.window.event == SDL_WINDOWEVENT_FOCUS_GAINED) got_focus_ = true;
            if (e.window.event == SDL_WINDOWEVENT_FOCUS_LOST && got_focus_) { close(); return; }
            if (e.window.event == SDL_WINDOWEVENT_CLOSE) { close(); return; }
        }
        if (ui_) ui_->handle(e);
    }

    void draw() {
        if (!gfx_ || !ui_) return;
        if (SDL_GetTicks() - opened_at_ > 30000) { close(); return; }
        Ui& ui = *ui_;
        ui.begin_frame();
        int W = ui.win_w(), H = ui.win_h();
        ui.fill({0, 0, W, H}, ui.th.panel);
        ui.stroke({0, 0, W, H}, ui.th.border_hi, 1);
        ui.begin_region({ui.S(6), ui.S(6), W - ui.S(12), H - ui.S(12)});
        ui.regions_back_space(0);
        std::function<void()> pending;
        bool close_after = false;
        for (auto& it : items_) {
            int h = item_h(it);
            if (it.kind == MenuItem::Sep) { Rect r = ui.item_rect(ui.avail_w(), h); ui.fill({r.x + ui.S(4), r.y + h / 2, r.w - ui.S(8), 1}, ui.th.border); continue; }
            Rect r = ui.item_rect(ui.avail_w(), h);
            bool hot = it.enabled && it.kind != MenuItem::Info && it.kind != MenuItem::Header && ui.hovered();
            if (hot) ui.fill(r, alpha(ui.th.accent, 70), ui.S(5));
            Color tc = it.kind == MenuItem::Info || it.kind == MenuItem::Header ? ui.tone_color(Tone::Muted) : ui.th.fg;
            if (!it.enabled) tc = ui.tone_color(Tone::Muted);
            int tx = r.x + ui.S(12);
            if (it.kind == MenuItem::Check) {
                Rect b = {tx, r.y + (h - ui.S(14)) / 2, ui.S(14), ui.S(14)};
                ui.fill(b, it.checked ? ui.th.accent : ui.th.sunken, ui.S(3));
                ui.stroke(b, it.checked ? ui.th.accent : ui.th.border_hi, 1, ui.S(3));
                tx += ui.S(22);
            }
            ui.text_at(tx, r.y + (h - ui.line_h()) / 2, it.text, tc, 0, it.bold ? Font::Bold : Font::Regular);
            if (hot && ui.clicked_in(r)) { pending = it.fn; if (it.kind != MenuItem::Sub) close_after = true; }
        }
        ui.end_region();
        if (ui.key_pressed(SDLK_ESCAPE)) close_after = true;
        ui.end_frame();
        if (pending) pending();
        if (close_after) close();
    }
    std::vector<MenuItem> items_;

    int item_h(const MenuItem& it) { return it.kind == MenuItem::Sep ? ui_->S(9) : ui_->line_h() + ui_->S(14); }

private:
    std::unique_ptr<Gfx> gfx_;
    std::unique_ptr<Ui> ui_;
    uint32_t opened_at_ = 0;
    bool got_focus_ = false;
};

class Mini {
public:
    bool open() const { return gfx_ != nullptr; }
    uint32_t id() const { return gfx_ ? gfx_->window_id() : 0; }
    std::function<void(bool)> on_gamemode;
    std::function<void()> on_boost;

    void show() {
        if (gfx_) return;
        gfx_ = std::make_unique<Gfx>();
        if (!gfx_->init("TuxThrottle mini", 220, 96, true, true)) { gfx_.reset(); return; }
        ui_ = std::make_unique<Ui>(*gfx_);
        ui_->set_theme(find_theme(kDefaultTheme));
        int w = ui_->S(230), h = ui_->S(104);
        SDL_SetWindowSize(gfx_->window(), w, h);
        SDL_Rect dr;
        if (SDL_GetDisplayBounds(0, &dr) != 0) dr = {0, 0, 1920, 1080};
        SDL_SetWindowPosition(gfx_->window(), dr.x + dr.w - w - ui_->S(20), dr.y + ui_->S(40));
        SDL_ShowWindow(gfx_->window());
    }
    void hide() { ui_.reset(); gfx_.reset(); }

    void handle(const SDL_Event& e) {
        if (!gfx_) return;
        if (e.type == SDL_WINDOWEVENT && e.window.windowID == id() && e.window.event == SDL_WINDOWEVENT_CLOSE) { hide(); closed = true; return; }
        // drag the strip by its background
        if (e.type == SDL_MOUSEBUTTONDOWN && e.button.windowID == id() && e.button.button == SDL_BUTTON_LEFT) { drag_ = true; int wx, wy; SDL_GetWindowPosition(gfx_->window(), &wx, &wy); int gx, gy; SDL_GetGlobalMouseState(&gx, &gy); off_x_ = gx - wx; off_y_ = gy - wy; }
        if (e.type == SDL_MOUSEBUTTONUP && e.button.windowID == id()) drag_ = false;
        if (e.type == SDL_MOUSEMOTION && e.motion.windowID == id() && drag_ && ui_ && ui_->hover_in({0, 0, 0, 0}) == false) {
            int gx, gy;
            SDL_GetGlobalMouseState(&gx, &gy);
            if (drag_enabled_) SDL_SetWindowPosition(gfx_->window(), gx - off_x_, gy - off_y_);
        }
        if (ui_) ui_->handle(e);
    }

    void draw(const Readings& r) {
        if (!gfx_ || !ui_) return;
        Ui& ui = *ui_;
        ui.begin_frame();
        int W = ui.win_w(), H = ui.win_h();
        ui.fill({0, 0, W, H}, alpha(ui.th.panel, 240), ui.S(8));
        ui.stroke({0, 0, W, H}, ui.th.border, 1, ui.S(8));
        ui.begin_region({ui.S(10), ui.S(8), W - ui.S(20), H - ui.S(16)});
        ui.regions_back_space(ui.S(4));
        ui.label("CPU: " + format_double(r.mini_freq, 2) + " GHz, " + format_double(r.mini_temp, 0) + " C", Tone::Normal, ui.S(11));
        ui.label(r.dgpu, Tone::Normal, ui.S(11));
        bool gm = r.game_mode;
        ui.flow_reset();
        drag_enabled_ = true;
        if (ui.button(gm ? "Game Mode: ON" : "Game Mode", gm ? Btn::Success : Btn::Secondary)) { drag_ = false; if (on_gamemode) on_gamemode(!gm); }
        ui.same_line();
        if (ui.button("Boost", Btn::Secondary)) { drag_ = false; if (on_boost) on_boost(); }
        ui.end_region();
        ui.end_frame();
    }
    bool closed = false;

private:
    std::unique_ptr<Gfx> gfx_;
    std::unique_ptr<Ui> ui_;
    bool drag_ = false, drag_enabled_ = false;
    int off_x_ = 0, off_y_ = 0;
};

// ------------------------------------------------------------------ app
std::string gui_exe() {
    std::string w = which("tuxthrottle");
    if (!w.empty()) return w;
    std::string sib = dir_name(self_exe()) + "/tuxthrottle";
    return path_exists(sib) ? sib : "";
}

void launch_gui(const std::string& user) {
    (void)user;
    std::string exe = gui_exe();
    if (exe.empty()) { sensors::notify("TuxThrottle", "Couldn't find the tuxthrottle window binary"); return; }
    // keep the session attached so pkexec can reach the polkit agent: no new session, prefer kstart
    if (!which("kstart").empty()) { if (spawn_quiet({"kstart", exe}, {}, false) > 0) return; }
    spawn_quiet({exe}, {}, false);
}

int run_tray(bool multi) {
    std::string rundir = env_or("XDG_RUNTIME_DIR", "/tmp");
    int lock = ::open((rundir + "/tuxthrottle-tray.lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (!multi && lock >= 0 && flock(lock, LOCK_EX | LOCK_NB) != 0) { std::fprintf(stderr, "TuxThrottle tray is already running.\n"); return 0; }
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    std::string user = real_user();
    // re-assert the saved keyboard colour/effect off-thread at login
    std::thread([] { try { kbd::apply_saved(); } catch (...) {} }).detach();

    Sni sni;
    if (!sni.start()) { std::fprintf(stderr, "tray: cannot reach the session bus\n"); return 1; }

    setenv("SDL_VIDEO_X11_WMCLASS", "tuxthrottle-tray", 1);
    setenv("SDL_VIDEO_WAYLAND_WMCLASS", "tuxthrottle-tray", 1);
    // SDL / GL only exist while a popup or the mini window is open (Gfx brings SDL up and tears it down): idle, the tray is a plain D-Bus client

    Readings r = read_all();
    Popup popup;
    Mini mini;
    bool mini_on = false;
    std::string pending_profile_msg;

    auto privileged = [&](const std::vector<std::string>& args, const std::string& what) {
        auto res = privileged_ctl(args);
        if (!res.first) sensors::notify(what, "Failed: " + res.second);
        r = read_all();
    };
    auto set_game_mode = [&](bool on) {
        auto res = sensors::set_game_mode(on);
        if (!res.first) sensors::notify("Game Mode", "Failed: " + res.second);
        r = read_all();
    };
    auto set_boost = [&](int pct) { privileged({"set", "fan-boost", "both", std::to_string(pct)}, "Fan boost"); };
    mini.on_gamemode = [&](bool on) { set_game_mode(on); };
    mini.on_boost = [&] { set_boost(100); };

    bool sub_profile = false, sub_fan = false;

    // The menu is rebuilt each time it opens (and when a submenu toggles) so readings stay current.
    std::function<std::vector<MenuItem>()> menu_items;
    int last_x = 0, last_y = 0;
    auto reopen = [&] { popup.show(menu_items(), last_x, last_y); };
    menu_items = [&]() {
        std::vector<MenuItem> v;
        auto add = [&](MenuItem::Kind k, const std::string& t, std::function<void()> fn = {}, bool checked = false, bool bold = false) {
            MenuItem m; m.kind = k; m.text = t; m.fn = std::move(fn); m.checked = checked; m.bold = bold; v.push_back(std::move(m));
        };
        add(MenuItem::Action, std::string(kAppName) + " " + kAuthor, [] { run_argv({"xdg-open", kProjectUrl}, 5000); });
        add(MenuItem::Sep, "");
        add(MenuItem::Action, std::string("Open ") + kAppName, [&] { launch_gui(user); }, false, true);
        add(MenuItem::Sep, "");
        add(MenuItem::Info, r.cpu);
        add(MenuItem::Info, r.igpu);
        add(MenuItem::Info, r.dgpu);
        add(MenuItem::Info, std::string("Backends: ") + (r.backends.empty() ? "..." : r.backends));
        if (!r.rapl_ok) add(MenuItem::Info, "! CPU power locked - install RaplPowerPermissions tweak");
        add(MenuItem::Sep, "");
        add(MenuItem::Check, "Game Mode", [&] { set_game_mode(!r.game_mode); }, r.game_mode);
        add(MenuItem::Sub, std::string(sub_profile ? "v" : ">") + "  Power profile", [&] { sub_profile = !sub_profile; reopen(); });
        if (sub_profile) {
            add(MenuItem::Action, "      Balanced", [&] { privileged({"set", "power-profile", "balanced"}, "Power profile"); });
            add(MenuItem::Action, "      Performance", [&] { privileged({"set", "power-profile", "performance"}, "Power profile"); });
        }
        add(MenuItem::Sub, std::string(sub_fan ? "v" : ">") + "  Fan boost", [&] { sub_fan = !sub_fan; reopen(); });
        if (sub_fan) {
            add(MenuItem::Action, "      Off (0%)", [&] { set_boost(0); });
            add(MenuItem::Action, "      Half (50%)", [&] { set_boost(50); });
            add(MenuItem::Action, "      Max (100%)", [&] { set_boost(100); });
        }
        add(MenuItem::Sep, "");
        add(MenuItem::Check, "Mini overlay (always-on-top strip)", [&] { mini_on = !mini_on; if (mini_on) mini.show(); else mini.hide(); }, mini_on);
        add(MenuItem::Sep, "");
        add(MenuItem::Action, "Quit", [&] { g_quit = true; });
        return v;
    };

    sni.on_activate = [&] { launch_gui(user); };
    sni.on_secondary = [&] { set_game_mode(!r.game_mode); };
    sni.on_context = [&](int x, int y) { last_x = x; last_y = y; sub_profile = sub_fan = false; r = read_all(); reopen(); };

    // first crash-watch scan only looks back over the next interval
    crashwatch::scan(1);
    double next_refresh = 0, next_state = 0, next_crash = now_seconds() + 30;
    auto status_line = [&] {
        if (!mangohud::status_line::is_enabled()) return;
        std::string gm = r.game_mode ? "GameMode ON" : "GameMode OFF";
        mangohud::status_line::set_status_line("TuxThrottle: " + gm + " | fan boost " + std::to_string(r.boost_pct) + "% | CPU " + r.tip_cpu + " | dGPU " + r.tip_dgpu);
    };
    while (!g_quit) {
        double now = now_seconds();
        if (now >= next_refresh) {
            r = read_all();
            sni.set_tooltip(std::string(kAppName) + " - " + kBlurb, std::string(kAuthor) + "\nBackends: " + r.backends + "\nClick to open  \xC2\xB7  CPU " + r.tip_cpu + "  \xC2\xB7  dGPU " + r.tip_dgpu);
            sni.set_daemon(r.daemon);
            status_line();
            next_refresh = now + (popup.open() || mini.open() ? 2.0 : 10.0);   // nobody is looking: the tooltip only needs to be roughly current
        }
        if (now >= next_crash) {
            next_crash = now + 30.0;
            std::thread([] {
                try {
                    for (const auto& f : crashwatch::scan(35)) {
                        fixlog::log_event("crashwatch", f.label, f.benign ? "info" : "warn");
                        if (!f.benign) sensors::notify(f.label, f.hint);
                    }
                } catch (...) {}
            }).detach();
        }
        (void)next_state;
        if (!sni.service()) {
            // bus gone (session logout) - exit quietly
            break;
        }
        SDL_Event e;
        if (popup.open() || mini.open())
            while (SDL_PollEvent(&e)) { popup.handle(e); mini.handle(e); }
        if (mini.closed) { mini.closed = false; mini_on = false; }
        popup.draw();
        mini.draw(r);
        int timeout = popup.open() || mini.open() ? 16 : 150;
        pollfd pf{sni.fd(), POLLIN, 0};
        poll(&pf, 1, timeout);
    }
    popup.close();
    mini.hide();
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--toggle") {
        auto r = sensors::toggle_game_mode();
        if (!r.first) { std::fprintf(stderr, "Toggle failed: %s\n", r.second.c_str()); return 1; }
        return 0;
    }
    return run_tray(argc > 1 && std::string(argv[1]) == "--allow-multiple");
}
