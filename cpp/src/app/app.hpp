// TuxThrottle GUI application shell: window, sidebar nav, background jobs, sensor sampler, toasts.
#pragma once
#include <atomic>
#include <cstdio>
#include <map>
#include <optional>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "../engine.hpp"
#include "../sensors.hpp"
#include "../ui/ui.hpp"

namespace tt::app {

class App;

// A shell command (or function) run on a worker thread; output lines are collected for display.
struct Job {
    std::mutex m;
    std::vector<std::string> lines;
    std::atomic<bool> running{false};
    std::atomic<int> rc{0};
    std::thread th;
    ~Job() { if (th.joinable()) th.join(); }
    void start_shell(const std::string& cmd);
    void start_fn(std::function<int(const std::function<void(const std::string&)>&)> fn);
    std::vector<std::string> snapshot();
    void clear();
    bool busy() const { return running; }
};

struct Sample {
    bool valid = false;
    std::optional<double> cpu_temp, cpu_freq, cpu_power, igpu_temp, dgpu_power;
    std::optional<int> igpu_clock, dgpu_clock, dgpu_temp, dgpu_util;
    bool rapl_ok = true, gamemode = false;
    std::optional<double> stapm_limit;
};

struct Page {
    std::string title;
    std::string group;     // nav section ("" = main)
    virtual ~Page() = default;
    virtual void draw(App& app) = 0;
    virtual void enter(App&) {}
    virtual void leave(App&) {}
};

class App {
public:
    App();
    ~App();
    int run(int argc, char** argv);

    ui::Gfx gfx;
    ui::Ui ui{gfx};
    std::string user;

    // services
    Sample sample() { std::lock_guard<std::mutex> l(sm_); return sample_; }
    const std::deque<double>& hist(const std::string& key) { return hist_[key]; }
    void wake();                                       // wake the UI thread from any thread
    void toast(const std::string& msg, ui::Tone tone = ui::Tone::Normal);
    void select_page(const std::string& title);
    void set_theme_by_name(const std::string& name);
    Job& job(const std::string& key);
    bool confirm(const std::string& key, const std::string& title, const std::string& body, const std::string& ok_label = "Confirm");   // modal; true on the frame it is accepted
    std::string cfg_dir() const;

    // standard page chrome
    void page_title(const std::string& title, const std::string& subtitle = "");
    void job_output(const std::string& key, int height);   // log view for a job + spinner line

    bool csv_logging() const { return csv_on_; }
    const std::string& csv_path() const { return csv_path_; }
    void set_csv_logging(bool on);

    // ---- tweak engine state (main-thread owned; workers operate on a copy) ----
    std::vector<engine::Item> items;
    std::map<std::string, bool> ticks;               // user's checkbox state per item id
    Json presets;
    engine::Item* find_item(const std::string& id);
    void log(const std::string& line);               // thread-safe
    std::vector<std::string> log_snapshot();
    bool busy() const { return busy_; }
    // Runs fn on a worker with a private copy of the items, then merges the copy back and re-evaluates.
    void run_items_task(const std::string& title, std::function<engine::BatchResult(std::vector<engine::Item>&, const engine::LogFn&)> fn);
    void run_task(const std::string& title, std::function<void(const engine::LogFn&)> fn, std::function<void()> on_done = {}, bool modal = true);
    void refresh_status();
    std::string user_ctl(const std::string& args);                              // `ctl <args>` run as the real user (su - user -c ...) when elevated
    void run_user_ctl_task(const std::string& tag, const std::string& title, const std::string& args);
    void run_bg(std::function<void()> fn);   // fire-and-forget worker thread (joined at exit)
    // Streams a shell command to the log under the busy overlay; reboot=true offers a reboot afterwards.
    void run_shell_task(const std::string& title, const std::string& cmd, bool reboot = false);
    void apply_selected();
    void apply_ids(const std::string& title, const std::vector<std::string>& ids, const std::string& label);
    int pending_ticks() const;
    // persisted daemon/user state under ~/.config/tuxthrottle
    Json read_state(const std::string& name);
    void write_state(const std::string& name, const Json& data);
    // in-app file chooser (mode 0 = open file, 1 = save file, 2 = choose folder); result arrives via file_result()
    void open_file_dialog(const std::string& key, int mode, const std::string& start_dir, const std::string& default_name = "", const std::string& ext = "");
    bool file_result(const std::string& key, std::string* path);   // true once when the user confirmed
    void message(const std::string& title, const std::string& body);
    void open_url(const std::string& url);
    void copy_text(const std::string& text, const std::string& what = "text");
    std::string status_text = "Ready.";

    // task follow-ups
    void show_output(const std::string& title, const std::vector<std::string>& lines);   // modal dump of a task's output (failures)
    std::string delta_text() { std::lock_guard<std::mutex> l(log_m_); return delta_; }
    void start_delta_watch(const std::string& label);                                   // sensor change 30 s after a preset apply
    void add_close_hook(std::function<void()> fn) { close_hooks_.push_back(std::move(fn)); }
    void set_busy_steps(int n) { busy_steps_ = n; }

    bool sampler_active = false;   // pages that need live data set this while shown

private:
    void frame();
    void draw_nav(ui::Rect r, bool expanded, bool overlay);
    bool nav_pinned_ = false, nav_open_ = false;
    uint32_t nav_leave_ = 0;
    void draw_header(ui::Rect r);
    void sampler_loop();
    void load_config();
    void save_config();
    void register_pages();

    std::vector<std::unique_ptr<Page>> pages_;
    int cur_ = 0;
    std::map<std::string, std::unique_ptr<Job>> jobs_;
    std::mutex sm_;
    Sample sample_;
    std::map<std::string, std::deque<double>> hist_;
    std::thread sampler_;
    std::atomic<bool> quit_{false};
    struct Toast { std::string msg; ui::Tone tone; uint32_t until; };
    std::vector<Toast> toasts_;
    std::string theme_name_;
    std::string pending_confirm_;
    bool want_screenshot_ = false;
    std::string shot_path_;
    int shot_page_ = -1;
    int shot_frames_ = 0;
    int shot_scroll_ = 0;
    std::vector<std::string> script_;
    size_t script_i_ = 0;
    int script_wait_ = 0;
    bool run_script_step();
    float nav_scroll_ = 0;
    bool elevated_ = false;
    sensors::ModelInfo model_;
    // busy / task
    std::string backend_text_;   // written once by a worker at start-up
    std::vector<uint8_t> header_icon_;
    int header_icon_w_ = 0;
    bool header_icon_loaded_ = false;
    std::atomic<int> win_state_{0};   // 0 focused, 1 visible but unfocused, 2 hidden / minimised
    bool busy_ = false;
    bool busy_modal_ = true;
    std::atomic<bool> task_done_{false};
    std::thread task_th_;
    std::string busy_title_;
    double busy_t0_ = 0;
    std::function<void()> task_after_;
    std::vector<engine::Item> work_;
    engine::BatchResult last_batch_;
    std::mutex log_m_;
    std::vector<std::string> log_lines_;
    bool log_open_ = false;
    std::string wd_unit_;
    double wd_until_ = 0;
    bool report_open_ = false;
    bool reboot_offer_ = false;
    int busy_steps_ = 0, busy_done_ = 0;
    std::string busy_step_, busy_phase_, delta_;
    std::vector<std::string> task_tail_;
    std::atomic<int> delta_gen_{0};
    std::vector<std::function<void()>> close_hooks_;
    std::string out_title_;
    std::vector<std::string> out_lines_;
    void note_progress(const std::string& line);
    std::vector<std::thread> bg_;
    std::atomic<int> bg_active_{0};
    std::mutex bg_m_;
    struct FileDlg { bool open = false; int mode = 0; std::string key, dir, name, ext, picked_key, picked, sel; bool hidden = false; } fdlg_;
    std::string msg_title_, msg_body_;
    void draw_file_dialog();
    std::unique_ptr<ui::Gfx> pgfx_;
    std::unique_ptr<ui::Ui> pui_;
    void toggle_popout();
    void draw_popout();
    void draw_footer(ui::Rect r);
    void draw_log(ui::Rect r);
    void draw_busy();
    void draw_watchdog();
    void finish_task();
    bool csv_on_ = false;
    std::string csv_path_;
    std::FILE* csv_ = nullptr;
    friend struct AppInternals;
};

extern uint32_t kWakeEvent;

}  // namespace tt::app
