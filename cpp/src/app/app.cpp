#include "app.hpp"

#include <pwd.h>
#include <unistd.h>

#include <filesystem>

#include <algorithm>
#include <future>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <ctime>

#include "../ui/icon.hpp"
#include "../distro.hpp"
#include "../profiles.hpp"
#include "../safety.hpp"
#include "../json.hpp"
#include "../sensors.hpp"
#include "../userenv.hpp"
#include "../util.hpp"
#include "navicons.hpp"
#include "pages.hpp"

namespace tt::app {

using namespace ui;
uint32_t kWakeEvent = 0;

static long getpwnam_uid(const std::string& u) { struct passwd* p = getpwnam(u.c_str()); return p ? static_cast<long>(p->pw_uid) : static_cast<long>(getuid()); }

// ---------------------------------------------------------------- Job
void Job::start_shell(const std::string& cmd) {
    start_fn([cmd](const std::function<void(const std::string&)>& out) { return run_shell_stream(cmd, out); });
}

void Job::start_fn(std::function<int(const std::function<void(const std::string&)>&)> fn) {
    if (running) return;
    if (th.joinable()) th.join();
    clear();
    running = true;
    th = std::thread([this, fn] {
        int rc_ = fn([this](const std::string& l) {
            { std::lock_guard<std::mutex> g(m); lines.push_back(l); }
            SDL_Event e;
            SDL_zero(e);
            e.type = kWakeEvent;
            SDL_PushEvent(&e);
        });
        rc = rc_;
        running = false;
        SDL_Event e;
        SDL_zero(e);
        e.type = kWakeEvent;
        SDL_PushEvent(&e);
    });
}

std::vector<std::string> Job::snapshot() { std::lock_guard<std::mutex> g(m); return lines; }
void Job::clear() { std::lock_guard<std::mutex> g(m); lines.clear(); }

// ---------------------------------------------------------------- App
App::App() = default;

App::~App() {
    quit_ = true;
    if (sampler_.joinable()) sampler_.join();
    if (task_th_.joinable()) task_th_.join();   // a running apply / undo always finishes
    // the fire-and-forget workers are read-only status probes (some wait up to 25 s on a slow check):
    // give them a moment, then end the process instead of freezing the window on close
    for (int i = 0; i < 30 && bg_active_.load() > 0; ++i) sleep_ms(50);
    if (bg_active_.load() > 0) { std::fflush(nullptr); _exit(0); }
    for (auto& t : bg_) if (t.joinable()) t.join();
    jobs_.clear();
}

std::string App::cfg_dir() const { return config_dir(user); }

void App::wake() {
    SDL_Event e;
    SDL_zero(e);
    e.type = kWakeEvent;
    SDL_PushEvent(&e);
}

void App::toast(const std::string& msg, Tone tone) { toasts_.push_back({msg, tone, SDL_GetTicks() + 4200}); ui.request_redraw(); }

Job& App::job(const std::string& key) {
    auto& p = jobs_[key];
    if (!p) p = std::make_unique<Job>();
    return *p;
}

void App::set_theme_by_name(const std::string& name) {
    theme_name_ = name;
    Theme t = find_theme(name);
    if (t.follow_desktop_accent) t.accent = read_desktop_accent();
    ui.set_theme(t);
    save_config();
    ui.request_redraw();
}

void App::load_config() {
    Json j = read_json_file(cfg_dir() + "/gui.json");
    theme_name_ = j["theme"].str();
    nav_pinned_ = j["rail_pinned"].is_bool() ? j["rail_pinned"].as_bool() : false;
    if (theme_name_.empty()) theme_name_ = kDefaultTheme;
    Theme t = find_theme(theme_name_);
    if (t.follow_desktop_accent) t.accent = read_desktop_accent();
    ui.set_theme(t);
}

void App::save_config() {
    Json j = read_json_file(cfg_dir() + "/gui.json");
    j.set("theme", theme_name_);
    j.set("rail_pinned", nav_pinned_);
    mkdir_p(cfg_dir());
    write_json_file(cfg_dir() + "/gui.json", j, user);
}

void App::select_page(const std::string& title) {
    for (size_t i = 0; i < pages_.size(); ++i)
        if (pages_[i]->title == title && static_cast<int>(i) != cur_) {
            pages_[static_cast<size_t>(cur_)]->leave(*this);
            cur_ = static_cast<int>(i);
            pages_[i]->enter(*this);
            ui.request_redraw();
            return;
        }
}

void App::register_pages() {
    pages_.push_back(make_dashboard());
    pages_.push_back(make_presets());
    pages_.push_back(make_keyboard());
    pages_.push_back(make_touchpad());
    pages_.push_back(make_vram());
    pages_.push_back(make_fans());
    pages_.push_back(make_power());
    pages_.push_back(make_display());
    pages_.push_back(make_battery());
    pages_.push_back(make_games());
    pages_.push_back(make_proton());
    pages_.push_back(make_mangohud());
    pages_.push_back(make_gametools());
    pages_.push_back(make_profiles());
    pages_.push_back(make_drives());
    pages_.push_back(make_updates());
    static const char* order[] = {"Gaming", "Emulation", "Input & Controllers", "GPU", "Power", "Performance", "KDE (Desktop GUI Tweaks)", "Repos", "Software", "Monitoring", "Streaming", "RGB"};
    std::vector<std::string> cats;
    for (const auto& it : items)
        if (!it.hidden && std::find(cats.begin(), cats.end(), it.category) == cats.end()) cats.push_back(it.category);
    auto rank = [&](const std::string& c) { for (size_t i = 0; i < std::size(order); ++i) if (c == order[i]) return static_cast<int>(i); return 99; };
    std::stable_sort(cats.begin(), cats.end(), [&](const std::string& x, const std::string& y) { return rank(x) < rank(y); });
    for (const auto& c : cats) pages_.push_back(make_category(c));
    pages_.push_back(make_about());
    pages_.push_back(make_report());
    auto grank = [](const std::string& g) { return g.empty() ? 0 : g == "Hardware" ? 1 : g == "System" ? 2 : g == "Games" ? 3 : g == "Tweaks" ? 4 : 5; };
    std::stable_sort(pages_.begin(), pages_.end(), [&](const std::unique_ptr<Page>& x, const std::unique_ptr<Page>& y) { return grank(x->group) < grank(y->group); });
}

void App::sampler_loop() {
    int n = 0;
    std::optional<double> stapm;
    while (!quit_) {
        if (!sampler_active) { sleep_ms(200); continue; }
        Sample s;
        s.valid = true;
        s.cpu_temp = sensors::cpu_temp_c();
        s.cpu_freq = sensors::cpu_freq_ghz();
        s.cpu_power = sensors::cpu_power_watts();
        auto ig = sensors::igpu_values();
        s.igpu_clock = ig.clock_mhz;
        s.igpu_temp = ig.temp_c;
        auto dg = sensors::dgpu_values();
        s.dgpu_clock = dg.clock_mhz;
        s.dgpu_temp = dg.temp_c;
        s.dgpu_util = dg.util_pct;
        s.dgpu_power = dg.power_w;
        s.rapl_ok = sensors::rapl_permissions_ok();
        s.gamemode = sensors::game_mode_state();
        if (n++ % 10 == 0) {
            stapm.reset();
            if (sensors::ryzenadj_available()) {
                auto info = sensors::read_ryzenadj_info();
                if (info) stapm = info->stapm_limit;
            }
        }
        s.stapm_limit = stapm;
        {
            std::lock_guard<std::mutex> l(sm_);
            sample_ = s;
            auto push = [&](const char* k, std::optional<double> v) {
                if (!v) return;
                auto& d = hist_[k];
                d.push_back(*v);
                while (d.size() > 90) d.pop_front();
            };
            push("cpu_temp", s.cpu_temp);
            push("cpu_power", s.cpu_power);
            push("dgpu_temp", s.dgpu_temp ? std::optional<double>(*s.dgpu_temp) : std::nullopt);
            push("dgpu_power", s.dgpu_power);
            if (csv_) {
                auto f = [](std::optional<double> v, int d) { return v ? format_double(*v, d) : std::string(); };
                auto i = [](std::optional<int> v) { return v ? std::to_string(*v) : std::string(); };
                char ts[32];
                std::time_t t = std::time(nullptr);
                std::tm tm;
                localtime_r(&t, &tm);
                std::strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &tm);
                std::fprintf(csv_, "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n", ts, f(s.cpu_temp, 0).c_str(), f(s.cpu_freq, 2).c_str(), f(s.cpu_power, 1).c_str(), i(s.igpu_clock).c_str(), f(s.igpu_temp, 0).c_str(),
                             i(s.dgpu_clock).c_str(), i(s.dgpu_temp).c_str(), i(s.dgpu_util).c_str(), f(s.dgpu_power, 1).c_str());
                std::fflush(csv_);
            }
        }
        wake();
        int steps = win_state_ == 2 ? 100 : win_state_ == 1 ? 40 : 19;   // 2 s focused, 4 s unfocused, 10 s hidden/minimised
        for (int i = 0; i < steps && !quit_ && sampler_active; ++i) sleep_ms(100);
    }
}

engine::Item* App::find_item(const std::string& id) {
    for (auto& it : items) if (it.id == id) return &it;
    return nullptr;
}

static std::string phase_from_line(const std::string& line) {
    std::string low = lower(line);
    static const std::pair<const char*, const char*> pairs[] = {
        {"downloading", "downloading"}, {"get:", "downloading"}, {"fetching", "downloading"}, {"resolving dependencies", "resolving"},
        {"dependencies resolved", "resolving"}, {"running transaction check", "checking"}, {"running scriptlet", "running scripts"},
        {"running transaction", "installing"}, {"upgrading ", "upgrading"}, {"installing ", "installing"}, {"reinstalling ", "installing"},
        {"removing ", "removing"}, {"erasing ", "removing"}, {"verifying ", "verifying"}, {"importing gpg key", "importing keys"}};
    for (auto& [needle, label] : pairs) if (low.find(needle) != std::string::npos) return label;
    return "";
}

void App::note_progress(const std::string& line) {   // caller holds log_m_
    auto step = [&](const std::string& tag) { size_t p = line.find(tag); if (p == 0 && line.size() > tag.size() + 4) { busy_step_ = line.substr(tag.size(), line.size() - tag.size() - 4); return true; } return false; };
    if (step("--- Applying: ") || step("--- Reverting: ")) { busy_phase_.clear(); }
    else if (line.rfind("[OK", 0) == 0 || line.rfind("[FAILED]", 0) == 0) ++busy_done_;
    else if (std::string ph = phase_from_line(line); !ph.empty()) busy_phase_ = ph;
}

void App::log(const std::string& line) {
    {
        std::lock_guard<std::mutex> l(log_m_);
        log_lines_.push_back(line);
        if (log_lines_.size() > 4000) log_lines_.erase(log_lines_.begin(), log_lines_.begin() + 1000);
        if (busy_) { note_progress(line); task_tail_.push_back(line); if (task_tail_.size() > 400) task_tail_.erase(task_tail_.begin()); }
    }
    wake();
}

void App::show_output(const std::string& title, const std::vector<std::string>& lines) { out_title_ = title; out_lines_ = lines; ui.request_redraw(); }

static std::string fmt_dur(double sec) {
    int s = static_cast<int>(sec), h = s / 3600, m = (s % 3600) / 60, r = s % 60;
    char b[40];
    if (h) std::snprintf(b, sizeof b, "%dh %02dm %02ds", h, m, r); else if (m) std::snprintf(b, sizeof b, "%dm %02ds", m, r); else std::snprintf(b, sizeof b, "%ds", r);
    return b;
}

static std::vector<std::string> fmt_snapshot_delta(const sensors::SnapshotLight& b, const sensors::SnapshotLight& a) {
    struct Row { const char* label; std::optional<double> before, after; const char* unit; int dp; double dead; };
    auto dbl = [](const std::optional<int>& v) { return v ? std::optional<double>(*v) : std::nullopt; };
    Row rows[] = {{"CPU temp", b.cpu_temp_c, a.cpu_temp_c, "\xC2\xB0""C", 0, 3}, {"CPU clock", b.cpu_freq_ghz, a.cpu_freq_ghz, " GHz", 2, 0.20}, {"STAPM", b.stapm_w, a.stapm_w, " W", 0, 2},
                  {"dGPU temp", b.dgpu_temp_c, a.dgpu_temp_c, "\xC2\xB0""C", 0, 3}, {"dGPU power", b.dgpu_power_w, a.dgpu_power_w, " W", 0, 4}};
    (void)dbl;
    std::vector<std::string> out;
    for (const auto& r : rows) {
        if (!r.before || !r.after) continue;
        double d = *r.after - *r.before;
        if (std::fabs(d) < r.dead) continue;
        std::string unit = r.unit, trimmed = trim(unit);
        out.push_back(std::string(r.label) + " " + format_double(*r.before, r.dp) + unit + " -> " + format_double(*r.after, r.dp) + unit + " (" + (d >= 0 ? "+" : "-") + format_double(std::fabs(d), r.dp) + trimmed + ")");
    }
    auto avg = [](const std::vector<int>& v) { long s = 0; int n = 0; for (int x : v) if (x) { s += x; ++n; } return n ? s / n : 0L; };
    long fb = avg(b.fan_rpm), fa = avg(a.fan_rpm);
    if (fb && fa && std::labs(fa - fb) >= 100) out.push_back("fans ~" + std::to_string(fb) + " -> ~" + std::to_string(fa) + " rpm avg");
    if (out.empty()) out.push_back("no significant sensor change");
    return out;
}

void App::start_delta_watch(const std::string& label) {
    int gen = ++delta_gen_;
    auto before = sensors::snapshot_light();
    run_bg([this, gen, before, label] {
        for (int i = 0; i < 60 && !quit_; ++i) sleep_ms(500);
        if (quit_ || gen != delta_gen_) return;
        auto lines = fmt_snapshot_delta(before, sensors::snapshot_light());
        std::string text = label + " - " + join(lines, "   \xC2\xB7   ");
        { std::lock_guard<std::mutex> l(log_m_); delta_ = text; }
        log("[Preset delta, 30 s after apply] " + join(lines, "  \xC2\xB7  "));
    });
}

std::vector<std::string> App::log_snapshot() { std::lock_guard<std::mutex> l(log_m_); return log_lines_; }

void App::run_task(const std::string& title, std::function<void(const engine::LogFn&)> fn, std::function<void()> on_done, bool modal) {
    if (busy_) { toast("An operation is already running - check the log.", Tone::Warning); return; }
    if (task_th_.joinable()) task_th_.join();
    { std::lock_guard<std::mutex> l(log_m_); busy_done_ = 0; busy_step_.clear(); busy_phase_.clear(); task_tail_.clear(); }
    busy_ = true;
    busy_modal_ = modal;
    busy_title_ = title;
    busy_t0_ = now_seconds();
    task_done_ = false;
    task_after_ = std::move(on_done);
    status_text = title;
    task_th_ = std::thread([this, fn] {
        fn([this](const std::string& l) { log(l); });
        task_done_ = true;
        wake();
    });
    ui.request_redraw();
}

void App::finish_task() {
    if (task_th_.joinable()) task_th_.join();
    busy_ = false;
    busy_steps_ = 0;
    status_text = "Ready.   \xC2\xB7   " + busy_title_ + " took " + fmt_dur(now_seconds() - busy_t0_);
    if (task_after_) { auto f = std::move(task_after_); task_after_ = nullptr; f(); }
    ui.request_redraw();
}

void App::run_items_task(const std::string& title, std::function<engine::BatchResult(std::vector<engine::Item>&, const engine::LogFn&)> fn) {
    work_ = items;
    run_task(title, [this, fn](const engine::LogFn& lg) {
        last_batch_ = fn(work_, lg);
        engine::evaluate_all(work_);
    }, [this] {
        items = std::move(work_);
        for (auto& it : items) ticks[it.id] = it.done();
        if (!last_batch_.watchdog_unit.empty()) { wd_unit_ = last_batch_.watchdog_unit; wd_until_ = now_seconds() + 20; }
        last_batch_ = {};
        status_text = "Ready.";
    });
}

std::string App::user_ctl(const std::string& args) {
    std::string inner = shell_quote(ctl_exe()) + " " + args;
    if (is_root() && !user.empty() && user != "root") return "su - " + shell_quote(user) + " -c " + shell_quote(inner);
    return inner;
}

void App::run_user_ctl_task(const std::string& tag, const std::string& title, const std::string& args) {
    log("[" + tag + "] " + title + " ...");
    auto rc = std::make_shared<int>(-1);
    std::string cmd = user_ctl(args);
    run_task(tag + " - " + title, [this, cmd, rc, tag, title](const engine::LogFn& lg) {
        *rc = run_shell_stream(cmd, lg);
        lg("[" + tag + "] " + title + " - " + (*rc == 0 ? std::string("done") : "exit " + std::to_string(*rc)));
    }, [this, rc, tag, title] {
        if (*rc == 0) toast(title + " - done", Tone::Success);
        else { toast(title + " failed (exit " + std::to_string(*rc) + ") - see the log", Tone::Danger); log_open_ = true; std::vector<std::string> t; { std::lock_guard<std::mutex> l(log_m_); t = task_tail_; } show_output(title + " - failed (exit " + std::to_string(*rc) + ")", t); }
    });
}

void App::run_shell_task(const std::string& title, const std::string& cmd, bool reboot) {
    log("[" + title + "] ...");
    auto rc = std::make_shared<int>(-1);
    run_task("Updates - " + title, [this, cmd, rc, title](const engine::LogFn& lg) {
        *rc = run_shell_stream(cmd, lg);
        lg("[" + title + "] " + (*rc == 0 ? std::string("done") : "exit " + std::to_string(*rc)));
    }, [this, rc, title, reboot] {
        if (*rc == 0) { toast(title + " - done", Tone::Success); if (reboot) reboot_offer_ = true; }
        else { toast(title + " failed (exit " + std::to_string(*rc) + ") - see the log", Tone::Danger); log_open_ = true; std::vector<std::string> t; { std::lock_guard<std::mutex> l(log_m_); t = task_tail_; } show_output(title + " - failed (exit " + std::to_string(*rc) + ")", t); }
    });
}

void App::run_bg(std::function<void()> fn) {
    std::lock_guard<std::mutex> l(bg_m_);
    ++bg_active_;
    bg_.emplace_back([this, fn] { fn(); --bg_active_; wake(); });
}

void App::refresh_status() {
    work_ = items;
    run_task("Refreshing status", [this](const engine::LogFn&) { engine::evaluate_all(work_); }, [this] {
        items = std::move(work_);
        for (auto& it : items) ticks[it.id] = it.done();
    }, false);
}

int App::pending_ticks() const {
    int n = 0;
    for (const auto& it : items) {
        if (it.hidden || !it.hw_supported) continue;
        auto t = ticks.find(it.id);
        if (t != ticks.end() && t->second != it.done()) ++n;
    }
    return n;
}

void App::apply_ids(const std::string& title, const std::vector<std::string>& ids, const std::string& label) {
    std::string user_ = user;
    int n = 0;
    for (const auto& id : ids) if (auto* it = find_item(id)) if (it->hw_supported && !it->done()) ++n;
    busy_steps_ = std::max(1, n);
    run_items_task(title, [ids, label, user_](std::vector<engine::Item>& w, const engine::LogFn& lg) { return engine::apply_batch(w, ids, label, lg, user_); });
}

void App::apply_selected() {
    std::vector<std::string> do_ids, undo_ids;
    for (const auto& it : items) {
        if (it.hidden || !it.hw_supported) continue;
        auto t = ticks.find(it.id);
        if (t == ticks.end()) continue;
        if (t->second && !it.done()) do_ids.push_back(it.id);
        else if (!t->second && it.kind == "tweak" && it.applied() && !it.undo_cmds.empty()) undo_ids.push_back(it.id);
    }
    if (do_ids.empty() && undo_ids.empty()) { toast("Nothing to apply - tick or untick items first."); return; }
    std::string user_ = user;
    busy_steps_ = static_cast<int>(do_ids.size() + undo_ids.size());
    run_items_task("Applying selected tweaks / apps", [do_ids, undo_ids, user_](std::vector<engine::Item>& w, const engine::LogFn& lg) {
        engine::BatchResult r;
        if (!undo_ids.empty()) engine::undo_batch(w, undo_ids, lg, user_);
        if (!do_ids.empty()) r = engine::apply_batch(w, do_ids, "pre-apply", lg, user_);
        return r;
    });
}

Json App::read_state(const std::string& name) { return read_json_file(cfg_dir() + "/" + name); }
void App::write_state(const std::string& name, const Json& data) { profiles::write_config(name, &data, user); }

void App::message(const std::string& title, const std::string& body) { msg_title_ = title; msg_body_ = body; ui.request_redraw(); }

void App::copy_text(const std::string& text, const std::string& what) {
    SDL_SetClipboardText(text.c_str());
    status_text = std::string(1, static_cast<char>(std::toupper(what[0]))) + what.substr(1) + " copied.";
    toast(status_text);
}

void App::open_url(const std::string& url) {
    // the GUI runs as root; hand the link to the real user's session
    std::string uid = std::to_string(getpwnam_uid(user));
    auto r = run_argv({"sudo", "-u", user, "env", "XDG_RUNTIME_DIR=/run/user/" + uid, "DISPLAY=" + env_or("DISPLAY", ":0"), "xdg-open", url}, 8000);
    if (r.code != 0) r = run_argv({"xdg-open", url}, 8000);
    log(r.code == 0 ? "[About] opened " + url : "[About] couldn't open a browser - copy the link instead");
}

void App::open_file_dialog(const std::string& key, int mode, const std::string& start_dir, const std::string& default_name, const std::string& ext) {
    fdlg_.open = true;
    fdlg_.key = key;
    fdlg_.mode = mode;
    fdlg_.dir = start_dir.empty() ? home_of(user) : start_dir;
    fdlg_.name = default_name;
    fdlg_.ext = ext;
    fdlg_.sel.clear();
    ui.set_focus(0);
    ui.request_redraw();
}

bool App::file_result(const std::string& key, std::string* path) {
    if (fdlg_.picked_key == key && !fdlg_.picked.empty()) { *path = fdlg_.picked; fdlg_.picked.clear(); fdlg_.picked_key.clear(); return true; }
    return false;
}

void App::draw_file_dialog() {
    namespace fs = std::filesystem;
    FileDlg& d = fdlg_;
    ui.begin_modal(d.mode == 2 ? "Choose folder" : d.mode == 1 ? "Save as" : "Open file", ui.S(760), ui.S(560));
    ui.input("fd_dir", &d.dir, ui.avail_w());
    std::error_code ec;
    fs::path cur = fs::path(d.dir);
    if (ui.button("Up", Btn::Secondary) && cur.has_parent_path()) { d.dir = cur.parent_path().string(); d.sel.clear(); }
    ui.same_line();
    if (ui.button("Home", Btn::Secondary)) { d.dir = home_of(user); d.sel.clear(); }
    ui.same_line();
    ui.checkbox("show hidden", &d.hidden);
    std::vector<std::pair<std::string, bool>> ents;   // name, is_dir
    if (fs::is_directory(cur, ec)) {
        for (const auto& e : fs::directory_iterator(cur, fs::directory_options::skip_permission_denied, ec)) {
            std::string n = e.path().filename().string();
            if (!d.hidden && !n.empty() && n[0] == '.') continue;
            bool dir = e.is_directory(ec);
            if (!dir && d.mode == 2) continue;
            if (!dir && !d.ext.empty() && e.path().extension() != d.ext) continue;
            ents.push_back({n, dir});
        }
    }
    std::sort(ents.begin(), ents.end(), [](auto& x, auto& y) { return x.second != y.second ? x.second > y.second : x.first < y.first; });
    ui.begin_scroll("fd_list", 0, ui.S(330));
    for (const auto& [n, dir] : ents) {
        int h = ui.line_h() + ui.S(8);
        Rect r = ui.item_rect(ui.avail_w(), h);
        bool hot = ui.hovered(), sel = d.sel == n;
        if (sel) ui.fill(r, alpha(ui.th.accent, 70), ui.S(4));
        else if (hot) ui.fill(r, ui.th.panel_hi, ui.S(4));
        ui.text_at(r.x + ui.S(8), r.y + ui.S(4), (dir ? "[ ] " : "    ") + n, dir ? ui.th.accent : ui.th.fg);
        if (hot && ui.clicked_in(r)) {
            if (dir) { d.dir = (cur / n).string(); d.sel.clear(); }
            else { d.sel = n; if (d.mode == 1) d.name = n; }
        }
    }
    ui.end_scroll();
    if (d.mode == 1) { ui.label("File name"); ui.same_line(); ui.input("fd_name", &d.name, ui.avail_w()); }
    bool ok_enabled = d.mode == 2 || (d.mode == 1 ? !d.name.empty() : !d.sel.empty());
    if (ui.button(d.mode == 2 ? "Choose this folder" : d.mode == 1 ? "Save" : "Open", Btn::Primary, 0, ok_enabled)) {
        d.picked = d.mode == 2 ? d.dir : (cur / (d.mode == 1 ? d.name : d.sel)).string();
        d.picked_key = d.key;
        d.open = false;
        wake();
    }
    ui.same_line();
    if (ui.button("Cancel", Btn::Secondary) || ui.key_pressed(SDLK_ESCAPE)) d.open = false;
    ui.end_modal();
}

void App::set_csv_logging(bool on) {
    std::lock_guard<std::mutex> l(sm_);
    if (csv_) { std::fclose(csv_); csv_ = nullptr; }
    csv_on_ = false;
    if (!on) return;
    std::string dir = home_of(user) + "/.local/share/tuxthrottle/sessions";
    mkdir_p(dir);
    char ts[32];
    std::time_t t = std::time(nullptr);
    std::tm tm;
    localtime_r(&t, &tm);
    std::strftime(ts, sizeof ts, "%Y%m%d-%H%M%S", &tm);
    csv_path_ = dir + "/session-" + ts + ".csv";
    csv_ = std::fopen(csv_path_.c_str(), "w");
    if (!csv_) return;
    std::fputs("timestamp,cpu_temp_c,cpu_freq_ghz,cpu_power_w,igpu_clock_mhz,igpu_temp_c,dgpu_clock_mhz,dgpu_temp_c,dgpu_util_pct,dgpu_power_w\n", csv_);
    chown_to_user(csv_path_, user);
    csv_on_ = true;
}

// ---------------------------------------------------------------- chrome
void App::page_title(const std::string& title, const std::string& subtitle) {
    ui.heading(title, ui.S(22));
    if (!subtitle.empty()) {
        ui.new_line(ui.S(0));
        ui.wrapped(subtitle, Tone::Muted);
    }
    ui.spacing(ui.S(10));
}

void App::job_output(const std::string& key, int height) {
    Job& j = job(key);
    if (j.busy()) {
        static const char* spin[] = {"|", "/", "-", "\\"};
        ui.label(std::string(spin[(SDL_GetTicks() / 120) % 4]) + "  Running...", Tone::Accent);
        ui.wake_after(120);
    } else if (!j.snapshot().empty()) {
        ui.label(j.rc == 0 ? "Finished OK" : "Finished with exit code " + std::to_string(j.rc.load()), j.rc == 0 ? Tone::Success : Tone::Danger);
    }
    ui.push_id(key);
    ui.log_view("out", j.snapshot(), height);
    ui.pop_id();
}

bool App::confirm(const std::string& key, const std::string& title, const std::string& body, const std::string& ok_label) {
    std::string pk = "confirm:" + key;
    if (!ui.popup_open(pk)) return false;
    bool accepted = false;
    ui.begin_modal(title, ui.S(460), ui.S(230));
    ui.wrapped(body, Tone::Normal, ui.avail_w());
    ui.spacing(ui.S(14));
    if (ui.button(ok_label, Btn::Primary)) { accepted = true; ui.close_popup(); }
    ui.same_line();
    if (ui.button("Cancel", Btn::Secondary) || ui.key_pressed(SDLK_ESCAPE)) ui.close_popup();
    ui.end_modal();
    return accepted;
}

void App::draw_footer(Rect r) {
    ui.fill(r, ui.th.panel);
    ui.fill({r.x, r.y, r.w, 1}, ui.th.border);
    ui.begin_region(r.inset(ui.S(12)));
    ui.regions_back_space(ui.S(10));
    bool en = !busy_;
    if (ui.button("Refresh Status", Btn::OutlineInfo, 0, en)) refresh_status();
    ui.same_line();
    int n = pending_ticks();
    if (ui.button(n ? "Apply Selected (" + std::to_string(n) + ")" : "Apply Selected", Btn::Success, 0, en)) apply_selected();
    ui.same_line();
    if (ui.button("Status report", Btn::Outline, 0, en)) report_open_ = true;
    ui.same_line();
    std::string st = busy_ ? busy_title_ + "..." : status_text;
    int tw = ui.text_w(st);
    ui.new_line(0);
    ui.end_region();
    ui.text_at(r.right() - tw - ui.S(16), r.y + (r.h - ui.line_h()) / 2, st, ui.tone_color(Tone::Muted));
}

void App::toggle_popout() {
    if (pgfx_) { pui_.reset(); pgfx_.reset(); log_open_ = true; return; }
    pgfx_ = std::make_unique<Gfx>();
    if (!pgfx_->init("TuxThrottle - Log", 900, 480)) { pgfx_.reset(); return; }
    pui_ = std::make_unique<Ui>(*pgfx_);
    pui_->set_theme(ui.th);
    log_open_ = false;
}

void App::draw_popout() {
    if (!pui_) return;
    Ui& u = *pui_;
    u.begin_frame();
    u.fill({0, 0, u.win_w(), u.win_h()}, u.th.sunken);
    u.begin_region({u.S(8), u.S(8), u.win_w() - u.S(16), u.win_h() - u.S(16)});
    if (u.button("dock", Btn::Secondary)) { u.end_region(); u.end_frame(); toggle_popout(); return; }
    u.same_line();
    if (u.button("copy", Btn::Ghost)) copy_text(join(log_snapshot(), "\n"), "log");
    u.same_line();
    if (u.button("clear", Btn::Ghost)) { std::lock_guard<std::mutex> l(log_m_); log_lines_.clear(); }
    u.log_view("log", log_snapshot(), u.win_h() - u.S(70));
    u.end_region();
    u.end_frame();
}

void App::draw_log(Rect r) {
    ui.fill(r, ui.th.sunken);
    ui.fill({r.x, r.y, r.w, 1}, ui.th.border);
    ui.begin_region(r.inset(ui.S(8)));
    ui.label("LOG", Tone::Muted, ui.S(10), Font::Bold);
    ui.same_line();
    if (ui.button(log_open_ ? "hide" : "show", Btn::Ghost)) log_open_ = !log_open_;
    ui.same_line();
    if (ui.button(pgfx_ ? "dock" : "pop out", Btn::Ghost)) toggle_popout();
    if (log_open_) {
        ui.same_line();
        if (ui.button("copy", Btn::Ghost)) copy_text(join(log_snapshot(), "\n"), "log");
        ui.same_line();
        if (ui.button("clear", Btn::Ghost)) { std::lock_guard<std::mutex> l(log_m_); log_lines_.clear(); }
    }
    if (log_open_ && !pgfx_) ui.log_view("log", log_snapshot(), r.h - ui.S(52));
    ui.end_region();
}

void App::draw_busy() {
    ui.begin_modal(busy_title_, ui.S(560), ui.S(300));
    static const char* spin[] = {"|", "/", "-", "\\"};
    std::string step, phase;
    int done, steps = busy_steps_;
    { std::lock_guard<std::mutex> l(log_m_); step = busy_step_; phase = busy_phase_; done = busy_done_; }
    auto lines = log_snapshot();
    ui.label(std::string(spin[(SDL_GetTicks() / 120) % 4]) + "  working...   Elapsed: " + fmt_dur(now_seconds() - busy_t0_), Tone::Accent);
    if (steps > 0) {
        ui.label("OVERALL", Tone::Muted, ui.S(10), Font::Bold);
        ui.progress(std::min(1.0f, static_cast<float>(done) / static_cast<float>(steps)), ui.avail_w(), ui.th.success);
        ui.label(std::to_string(std::min(done, steps)) + " / " + std::to_string(steps), Tone::Muted, ui.S(12));
    }
    ui.label("CURRENT TASK", Tone::Muted, ui.S(10), Font::Bold);
    ui.progress(static_cast<float>(std::fmod((now_seconds() - busy_t0_) * 0.6, 1.0)), ui.avail_w(), ui.th.info);
    std::string cur = step.empty() ? (lines.empty() ? "Preparing..." : lines.back()) : step;
    ui.wrapped(phase.empty() ? cur : cur + "   -   " + phase, Tone::Muted);
    ui.end_modal();
    ui.wake_after(120);
}

void App::draw_watchdog() {
    double left = wd_until_ - now_seconds();
    if (left <= 0) { wd_unit_.clear(); return; }
    ui.begin_modal("Confirm risky change", ui.S(480), ui.S(260));
    ui.wrapped("A risky (ADVANCED) tweak was just applied. If the system looks fine, click Keep. If anything is wrong, click Revert Now - otherwise it reverts automatically when the countdown ends.", Tone::Normal);
    ui.heading("Auto-revert in " + std::to_string(static_cast<int>(left) + 1) + "s", ui.S(18));
    if (ui.button("Keep", Btn::Success)) { watchdog::disarm(wd_unit_); log("[watchdog] user confirmed - keeping the change"); wd_unit_.clear(); }
    ui.same_line();
    if (ui.button("Revert Now", Btn::Danger)) {
        profiles::rollback("last", user);
        log("[watchdog] user chose Revert Now - rolled back");
        watchdog::disarm(wd_unit_);
        wd_unit_.clear();
        refresh_status();
    }
    ui.end_modal();
    ui.wake_after(250);
}

void App::draw_header(Rect r) {
    int pad = ui.S(16);
    ui.fill({r.x, r.y, r.w, r.h}, ui.th.panel);
    int isz = ui.S(28);
    if (!header_icon_loaded_) { int w, h; app_icon_png(48, &w, &h, &header_icon_); header_icon_w_ = w; header_icon_loaded_ = true; }
    Rect ir = {pad, r.y + (r.h - isz) / 2, isz, isz};
    ui.draw([this, ir](Gfx& g) { g.image("hdr", header_icon_.data(), header_icon_w_, header_icon_w_, ir); });
    ui.text_at(pad + isz + ui.S(10), r.y + (r.h - ui.g.line_height(ui.S(17), Font::Display)) / 2, "TuxThrottle", ui.th.fg, ui.S(17), Font::Display);
    std::string tag = std::string(elevated_ ? "elevated" : "user") + " \xC2\xB7 " + user;
    int tw = ui.g.text_width(tag, ui.S(11), Font::Regular);
    {   // which backends this machine is using (daemon or direct, OpenRGB, NVML, ryzenadj ...)
        std::string bs = "backends: " + backend_text_;
        int bw = ui.g.text_width(bs, ui.S(11), Font::Regular);
        ui.text_at(r.right() - pad - tw - ui.S(34) - bw, r.y + (r.h - ui.g.line_height(ui.S(11), Font::Regular)) / 2, bs, ui.tone_color(Tone::Muted), ui.S(11), Font::Regular);
    }
    ui.text_at(r.right() - pad - tw, r.y + (r.h - ui.g.line_height(ui.S(11), Font::Regular)) / 2, tag, ui.tone_color(Tone::Muted), ui.S(11), Font::Regular);
    // the elevated state is the one thing worth a colour: a dot, not a pill
    ui.fill({r.right() - pad - tw - ui.S(14), r.y + r.h / 2 - ui.S(3), ui.S(6), ui.S(6)}, elevated_ ? ui.th.success : ui.th.warning, ui.S(3));
    ui.fill({r.x, r.bottom() - 1, r.w, 1}, ui.th.border);
}

// Navigation rail: icons only (52 px). Pinned = docked and labelled; unpinned = labelled panel floats over the page on hover.
void App::draw_nav(Rect r, bool expanded, bool overlay) {
    if (overlay) ui.begin_over(r);
    ui.fill(r, ui.th.sunken);
    ui.fill({r.right() - 1, r.y, 1, r.h}, ui.th.border);
    ui.begin_region(r.inset(ui.S(6)));
    ui.regions_back_space(ui.S(2));
    // pin toggle
    {
        int h = ui.line_h() + ui.S(12);
        Rect it = ui.item_rect(ui.avail_w(), h);
        bool hot = ui.hovered();
        if (hot) ui.fill(it, ui.th.panel_hi, ui.S(6));
        int cx = expanded ? it.right() - ui.S(18) : it.x + it.w / 2, cy = it.y + it.h / 2;
        Color pc = nav_pinned_ ? ui.th.accent : ui.tone_color(Tone::Muted);
        ui.draw([=](Gfx& g) {
            g.disc(static_cast<float>(cx), static_cast<float>(cy) - 2, 4.0f, pc);
            g.line(static_cast<float>(cx), static_cast<float>(cy), static_cast<float>(cx), static_cast<float>(cy) + 7, pc, 2);
        });
        if (expanded) ui.text_at(it.x + ui.S(12), it.y + (it.h - ui.line_h(ui.S(11))) / 2, nav_pinned_ ? "PINNED" : "PIN MENU", ui.tone_color(Tone::Muted), ui.S(11), Font::Bold);
        if (hot && ui.clicked_in(it)) { nav_pinned_ = !nav_pinned_; save_config(); }
    }
    ui.begin_scroll("nav", 0, 0);
    ui.regions_back_space(ui.S(2));
    std::string last_group = "\x01";
    for (size_t i = 0; i < pages_.size(); ++i) {
        Page& p = *pages_[i];
        if (p.group != last_group) {
            last_group = p.group;
            if (!p.group.empty()) {
                if (expanded) {
                    ui.new_line(ui.S(8));
                    std::string up = p.group;
                    for (auto& c : up) if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 32);
                    ui.label(up, Tone::Muted, ui.S(10), Font::Bold);
                    ui.new_line(ui.S(0));
                } else {
                    Rect d = ui.item_rect(ui.avail_w(), ui.S(9));
                    ui.fill({d.x + ui.S(8), d.y + d.h / 2, d.w - ui.S(16), 1}, ui.th.border);
                }
            }
        }
        int h = ui.line_h() + ui.S(12);
        Rect it = ui.item_rect(ui.avail_w(), h);
        bool active = static_cast<int>(i) == cur_;
        bool hot = ui.hovered();
        if (active) {
            ui.fill(it, alpha(ui.th.accent, 45), ui.S(6));
        } else if (hot) ui.fill(it, ui.th.panel_hi, ui.S(6));
        int isz = ui.S(18);
        int ix = expanded ? it.x + ui.S(12) : it.x + (it.w - isz) / 2;
        draw_nav_icon(ui, p.title, {ix, it.y + (it.h - isz) / 2, isz, isz}, active || hot);
        if (expanded) {
            Color tc = active ? mix(ui.th.accent, Color(255, 255, 255), 0.55f) : (hot ? ui.th.fg : ui.tone_color(Tone::Muted));
            ui.text_at(it.x + ui.S(40), it.y + (it.h - ui.line_h()) / 2, p.title, tc, 0, active ? Font::Bold : Font::Regular);
        }
        if (hot && ui.clicked_in(it)) { select_page(p.title); if (!nav_pinned_) nav_open_ = false; }
    }
    ui.end_scroll();
    ui.end_region();
    if (overlay) ui.end_over();
}

void App::frame() {
    ui.begin_frame();
    int W = ui.win_w(), H = ui.win_h();
    int hdr = ui.S(44), rail = ui.S(52), wide = ui.S(220);
    int side = nav_pinned_ ? wide : rail;
    draw_header({0, 0, W, hdr});
    int top = hdr;
    if (!model_.is_target) {
        std::string txt = model_.is_close ? "Detected " + model_.vendor + " " + model_.product + " - a G15 5515 variant, not the exact unit this was built against; some sysfs paths may differ."
                                          : "Detected " + model_.vendor + " " + model_.product + ". The hardware tabs (keyboard, fans, power limits, the G-key) were written for the Dell G15 5515 and may not work here; the game, Steam, Proton, MangoHud, drives, apps and updates tabs do not depend on the hardware.";
        auto lines = ui.wrap_lines(txt, W - ui.S(32), ui.base_size(), Font::Regular);
        int bh = static_cast<int>(lines.size()) * ui.line_h() + ui.S(14);
        ui.fill({0, hdr, W, bh}, alpha(ui.th.warning, 30));
        for (size_t i = 0; i < lines.size(); ++i) ui.text_at(ui.S(16), hdr + ui.S(7) + static_cast<int>(i) * ui.line_h(), lines[i], ui.tone_color(Tone::Warning));
        top += bh;
    }
    int foot = ui.S(48), logh = log_open_ ? ui.S(190) : ui.S(40);
    {   // hover-open / delayed close for the floating (unpinned) panel
        bool inside_rail = ui.mouse_x() < rail && ui.mouse_y() >= top && ui.mouse_y() < H - foot - logh;
        bool inside_panel = nav_open_ && ui.mouse_x() < wide && ui.mouse_y() >= top && ui.mouse_y() < H - foot - logh;
        uint32_t now = SDL_GetTicks();
        if (!nav_pinned_ && (inside_rail || inside_panel)) { nav_open_ = true; nav_leave_ = 0; }
        else if (nav_open_ && !nav_pinned_) { if (!nav_leave_) nav_leave_ = now; if (now - nav_leave_ > 350) nav_open_ = false; else ui.wake_after(100); }
        if (nav_pinned_) nav_open_ = false;
    }
    draw_log({0, H - logh, W, logh});
    draw_footer({0, H - logh - foot, W, foot});
    Rect content = {side, top, W - side, H - top - foot - logh};
    ui.begin_region(content.inset(ui.S(18)));
    Page& pg = *pages_[static_cast<size_t>(cur_)];
    ui.push_id(pg.title);
    if (shot_scroll_ > 0) ui.scroll_to("page", static_cast<float>(shot_scroll_));
    ui.begin_scroll("page", 0, 0);
    pg.draw(*this);
    ui.end_scroll();
    ui.pop_id();
    ui.end_region();
    {
        Rect nav_area = {0, top, nav_pinned_ ? wide : (nav_open_ ? wide : rail), H - top - foot - logh};
        draw_nav(nav_area, nav_pinned_ || nav_open_, !nav_pinned_ && nav_open_);
    }

    if (fdlg_.open) draw_file_dialog();
    if (!msg_title_.empty()) {
        ui.begin_modal(msg_title_, ui.S(520), ui.S(240));
        ui.wrapped(msg_body_, Tone::Normal, ui.avail_w());
        ui.spacing(ui.S(10));
        if (ui.button("OK", Btn::Primary) || ui.key_pressed(SDLK_RETURN)) msg_title_.clear();
        ui.end_modal();
    }
    if (!out_title_.empty()) {
        ui.begin_modal(out_title_, ui.S(900), ui.S(560));
        ui.label("Full output is in the log console at the bottom of the main window.", Tone::Muted);
        ui.log_view("outdlg", out_lines_, ui.S(400));
        if (ui.button("Close", Btn::Secondary) || ui.key_pressed(SDLK_ESCAPE)) out_title_.clear();
        ui.end_modal();
    }
    if (reboot_offer_) { ui.open_popup("confirm:reboot"); reboot_offer_ = false; }
    if (confirm("reboot", "Reboot recommended", "A reboot is recommended to finish this update. Reboot now?", "Reboot now")) run_shell_stream("systemctl reboot", [](const std::string&) {});
    if (wd_unit_.size()) draw_watchdog();
    if (busy_ && busy_modal_) draw_busy();
    if (busy_ && task_done_) finish_task();
    if (report_open_) {
        ui.begin_modal("Status report", ui.S(980), ui.S(640));
        ui.log_view("report", [&] { std::vector<std::string> v; std::string r = engine::format_status_report(items), l; for (char c : r) { if (c == '\n') { v.push_back(l); l.clear(); } else l += c; } if (!l.empty()) v.push_back(l); return v; }(), ui.S(500));
        if (ui.button("Close", Btn::Primary) || ui.key_pressed(SDLK_ESCAPE)) report_open_ = false;
        ui.end_modal();
    }

    // toasts
    uint32_t now = SDL_GetTicks();
    toasts_.erase(std::remove_if(toasts_.begin(), toasts_.end(), [&](const Toast& t) { return now > t.until; }), toasts_.end());
    ui.overlay([&] {
        int y = H - ui.S(20);
        for (auto it = toasts_.rbegin(); it != toasts_.rend(); ++it) {
            int tw = std::min(ui.S(520), ui.g.text_width(it->msg, ui.base_size()) + ui.S(32));
            int th = ui.line_h() + ui.S(18);
            Rect b = {W - tw - ui.S(20), y - th, tw, th};
            y -= th + ui.S(8);
            ui.fill(b, ui.th.card, ui.S(8));
            ui.stroke(b, ui.tone_color(it->tone == Tone::Normal ? Tone::Accent : it->tone), 1, ui.S(8));
            ui.text_at(b.x + ui.S(16), b.y + ui.S(9), ui.g.ellipsize(it->msg, tw - ui.S(32), ui.base_size(), Font::Regular), ui.th.fg);
            ui.wake_after(static_cast<int>(it->until - now) + 20);
        }
    });
    ui.end_frame();
}

// ---------------------------------------------------------------- run
static void elevate(int argc, char** argv) {
    std::vector<std::string> a{"pkexec", "env"};
    for (const char* v : {"DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS", "TT_SCALE", "HOME", "XDG_CURRENT_DESKTOP", "SDL_VIDEODRIVER"})
        if (const char* e = std::getenv(v)) a.push_back(std::string(v) + "=" + e);
    a.push_back(self_exe());
    for (int i = 1; i < argc; ++i) a.push_back(argv[i]);
    if (!which("pkexec").empty()) {
        std::vector<char*> v;
        for (auto& s : a) v.push_back(s.data());
        v.push_back(nullptr);
        execvp("pkexec", v.data());
    }
    if (!which("sudo").empty()) {
        std::vector<std::string> b{"sudo", "--preserve-env=DISPLAY,XAUTHORITY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,HOME", self_exe()};
        for (int i = 1; i < argc; ++i) b.push_back(argv[i]);
        std::vector<char*> v;
        for (auto& s : b) v.push_back(s.data());
        v.push_back(nullptr);
        execvp("sudo", v.data());
    }
    std::fprintf(stderr, "Need root. Run: sudo tuxthrottle\n");
    std::exit(1);
}

// Test automation: --script FILE with lines  click X Y [right] | move X Y | text STR | key NAME | wheel N | page NAME | shot FILE | wait N | quit
bool App::run_script_step() {
    if (script_wait_ > 0) { --script_wait_; return true; }
    if (script_i_ >= script_.size()) return false;
    std::string ln = script_[script_i_++];
    auto sp = split_ws(ln);
    if (sp.empty() || sp[0][0] == '#') return true;
    SDL_Event e;
    SDL_zero(e);
    const std::string& c = sp[0];
    if (c == "click" && sp.size() >= 3) {
        bool right = sp.size() > 3 && sp[3] == "right";
        e.type = SDL_MOUSEMOTION; e.motion.x = std::atoi(sp[1].c_str()); e.motion.y = std::atoi(sp[2].c_str()); ui.handle(e);
        e.type = SDL_MOUSEBUTTONDOWN; e.button.button = right ? SDL_BUTTON_RIGHT : SDL_BUTTON_LEFT; e.button.x = std::atoi(sp[1].c_str()); e.button.y = std::atoi(sp[2].c_str()); ui.handle(e);
        frame();
        e.type = SDL_MOUSEBUTTONUP; ui.handle(e);
    } else if (c == "move" && sp.size() >= 3) { e.type = SDL_MOUSEMOTION; e.motion.x = std::atoi(sp[1].c_str()); e.motion.y = std::atoi(sp[2].c_str()); ui.handle(e); }
    else if (c == "text") { e.type = SDL_TEXTINPUT; std::string t = ln.substr(ln.find(' ') + 1); std::snprintf(e.text.text, sizeof e.text.text, "%s", t.c_str()); ui.handle(e); }
    else if (c == "key" && sp.size() >= 2) { e.type = SDL_KEYDOWN; e.key.keysym.sym = SDL_GetKeyFromName(sp[1].c_str()); ui.handle(e); }
    else if (c == "wheel" && sp.size() >= 2) { e.type = SDL_MOUSEWHEEL; e.wheel.y = std::atoi(sp[1].c_str()); ui.handle(e); }
    else if (c == "page" && ln.size() > 5) select_page(ln.substr(5));
    else if (c == "shot" && sp.size() >= 2) { std::string path = sp[1]; ui.before_present = [this, path] { gfx.screenshot(path); ui.before_present = nullptr; }; }
    else if (c == "wait" && sp.size() >= 2) script_wait_ = std::atoi(sp[1].c_str());
    else if (c == "quit") return false;
    ui.request_redraw();
    script_wait_ = std::max(script_wait_, 2);
    return true;
}

int App::run(int argc, char** argv) {
    double t_start = now_seconds();
    bool timing = std::getenv("TT_TIMING") != nullptr;
    bool no_elev = std::getenv("TT_NO_ELEVATE") != nullptr, list_pages = false;
    std::string page, shot;
    int bench_n = 0;
    int win_w = 1280, win_h = 820;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--no-elevate") no_elev = true;
        else if (a == "--list-pages") { list_pages = true; no_elev = true; }
        else if (a == "--page" && i + 1 < argc) page = argv[++i];
        else if (a == "--bench" && i + 1 < argc) bench_n = std::atoi(argv[++i]);
        else if (a == "--shot" && i + 1 < argc) shot = argv[++i];
        else if (a == "--script" && i + 1 < argc) { auto t = read_file(argv[++i]); if (t) script_ = split_lines(*t); }
        else if (a == "--scroll" && i + 1 < argc) shot_scroll_ = std::atoi(argv[++i]);
        else if (a == "--size" && i + 1 < argc) std::sscanf(argv[++i], "%dx%d", &win_w, &win_h);
    }
    elevated_ = is_root();
    backend_text_ = "...";
    run_bg([this] { backend_text_ = sensors::backend_summary(); wake(); });
    model_ = sensors::detect_model();
    if (!elevated_ && !no_elev) elevate(argc, argv);
    user = resolve_real_user();
    if (user.empty()) user = real_user();

    kWakeEvent = SDL_RegisterEvents(1);
    int sc = 1;
    if (const char* s = std::getenv("TT_SCALE")) sc = std::max(1, std::atoi(s));
    (void)sc;
    auto items_future = std::async(std::launch::async, [this] { return engine::load_items(user); });   // overlaps window creation
    if (!gfx.init("TuxThrottle", win_w, win_h)) return 1;
    if (timing) std::fprintf(stderr, "gfx_init_s %.3f\n", now_seconds() - t_start);
    SDL_SetWindowMinimumSize(gfx.window(), gfx.px(1024), gfx.px(768));
    { int iw, ih; std::vector<uint8_t> px; app_icon_png(128, &iw, &ih, &px); gfx.set_icon_rgba(px.data(), iw, ih); }
    load_config();
    {   // an autostart entry from the Python era launches a file that is gone: point it at the tray binary
        std::string af = home_of(user) + "/.config/autostart/tuxthrottle-tray.desktop";
        auto cur = read_file(af);
        std::string tray = which("tuxthrottle-tray");
        if (cur && !tray.empty() && (cur->find(".py") != std::string::npos || cur->find("python") != std::string::npos)) {
            if (write_file(af, "[Desktop Entry]\nType=Application\nName=TuxThrottle Tray\nComment=Tray icon + quick launcher for TuxThrottle\nExec=" + tray + "\nIcon=tuxthrottle\nTerminal=false\nX-GNOME-Autostart-enabled=true\n") && is_root()) chown_to_user(af, user);
        }
    }
    items = items_future.get();
    presets = engine::load_presets();
    if (timing) std::fprintf(stderr, "items_loaded_s %.3f\n", now_seconds() - t_start);
    register_pages();
    if (list_pages) { for (const auto& pg : pages_) std::puts(pg->title.c_str()); return 0; }
    refresh_status();
    sampler_ = std::thread([this] { sampler_loop(); });
    for (size_t i = 0; i < pages_.size(); ++i) if (pages_[i]->title == page) cur_ = static_cast<int>(i);
    pages_[static_cast<size_t>(cur_)]->enter(*this);

    if (bench_n > 0) {   // CPU cost of building + submitting a page frame (use TT_NOVSYNC=1)
        for (int i = 0; i < 30; ++i) frame();
        gfx.prof_on = true;
        double t0 = now_seconds();
        for (int i = 0; i < bench_n; ++i) frame();
        gfx.prof_on = false;
        std::printf("  per-frame ms: text %.2f (%d) fill %.2f (%d) stroke %.2f (%d) polyline %.2f (%d) arc %.2f (%d) clip %.2f (%d)\n", gfx.prof_ms[0] / bench_n, gfx.prof_n[0] / bench_n, gfx.prof_ms[1] / bench_n, gfx.prof_n[1] / bench_n, gfx.prof_ms[2] / bench_n, gfx.prof_n[2] / bench_n, gfx.prof_ms[3] / bench_n, gfx.prof_n[3] / bench_n, gfx.prof_ms[4] / bench_n, gfx.prof_n[4] / bench_n, gfx.prof_ms[5] / bench_n, gfx.prof_n[5] / bench_n);
        std::printf("frame_avg_ms %.3f over %d frames (%s)  last: layout %.2f cmds %.2f present %.2f  (%d commands)\n", (now_seconds() - t0) * 1000 / bench_n, bench_n, pages_[static_cast<size_t>(cur_)]->title.c_str(), ui.layout_ms, ui.cmds_ms, ui.present_ms, ui.n_cmds);
        sampler_active = false;
        return 0;
    }
    int frames = 0;
    if (!shot.empty()) {
        ui.before_present = [&] {
            if (frames >= 12) {
                gfx.screenshot(shot);
                quit_ = true;
            }
        };
    }
    bool done = false;
    while (!done && !quit_) {
        SDL_Event e;
        bool redraw = false;
        if (SDL_WaitEventTimeout(&e, ui.wake_timeout(shot.empty() && script_.empty() ? 4000 : 60))) {
            do {
                if (e.type == SDL_QUIT) done = true;
                else if (e.type == kWakeEvent) redraw = true;
                else if (pgfx_ && ((e.type == SDL_WINDOWEVENT && e.window.windowID == pgfx_->window_id()) || (e.type == SDL_MOUSEMOTION && e.motion.windowID == pgfx_->window_id()) ||
                                   ((e.type == SDL_MOUSEBUTTONDOWN || e.type == SDL_MOUSEBUTTONUP) && e.button.windowID == pgfx_->window_id()) || (e.type == SDL_MOUSEWHEEL && e.wheel.windowID == pgfx_->window_id()) ||
                                   (e.type == SDL_KEYDOWN && e.key.windowID == pgfx_->window_id()) || (e.type == SDL_TEXTINPUT && e.text.windowID == pgfx_->window_id()))) {
                    if (e.type == SDL_WINDOWEVENT && e.window.event == SDL_WINDOWEVENT_CLOSE) toggle_popout();
                    else if (pui_) pui_->handle(e);
                    redraw = true;
                } else if (e.type == SDL_WINDOWEVENT && e.window.event == SDL_WINDOWEVENT_CLOSE && e.window.windowID == gfx.window_id()) done = true;
                else redraw |= ui.handle(e);
            } while (SDL_PollEvent(&e));
        } else redraw = true;
        { Uint32 fl = SDL_GetWindowFlags(gfx.window()); win_state_ = (fl & (SDL_WINDOW_MINIMIZED | SDL_WINDOW_HIDDEN)) ? 2 : (fl & SDL_WINDOW_INPUT_FOCUS) ? 0 : 1; }
        if (!script_.empty()) { if (!run_script_step() && script_wait_ <= 0) { quit_ = true; } redraw = true; }
        if (redraw || ui.take_redraw() || !shot.empty()) { double tf = now_seconds(); frame(); ++frames; draw_popout(); if (timing && frames > 3) std::fprintf(stderr, "frame_ms %.2f\n", (now_seconds() - tf) * 1000); if (timing && frames == 1) { std::fprintf(stderr, "first_frame_s %.3f\n", now_seconds() - t_start); } }
    }
    sampler_active = false;
    for (auto& h : close_hooks_) h();
    if (csv_) { std::fclose(csv_); csv_ = nullptr; }
    return 0;
}

}  // namespace tt::app
