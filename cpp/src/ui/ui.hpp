// Immediate-mode UI toolkit (cursor layout, ImGui style) on top of Gfx. No dependencies beyond SDL2/FreeType.
#pragma once
#include <functional>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "gfx.hpp"

namespace tt::ui {

struct Theme {
    std::string name;
    Color panel, sunken, panel_hi, fg, muted, border, border_hi, card, chart_axis;
    Color success, danger, warning, info, secondary, help_amber, help_banner_bg;
    Color accent;
    bool follow_desktop_accent = false;
};
const std::vector<Theme>& themes();
const Theme& find_theme(const std::string& name);
extern const char* kDefaultTheme;       // "Steam Navy"
Color read_desktop_accent();            // KDE accent colour (kdeglobals), else Breeze blue

enum class Btn { Primary, Secondary, Outline, OutlineInfo, OutlineWarn, OutlineDanger, OutlineSuccess, Success, Danger, Warning, Info, Ghost };
enum class Tone { Normal, Muted, Accent, Success, Danger, Warning, Info, Amber };

class Ui {
public:
    explicit Ui(Gfx& gfx);
    Gfx& g;
    double layout_ms = 0, cmds_ms = 0, present_ms = 0;   // last frame: widget layout, command execution, buffer swap
    int n_cmds = 0;
    std::function<void()> before_present;
    static double now_ms();
    double frame_t0_ = 0;   // called after drawing, before the buffer swap (screenshots)
    Theme th;

    // ---- frame / input ----
    void set_theme(const Theme& t) { th = t; }
    void begin_frame();
    void end_frame();
    bool handle(const SDL_Event& e);          // true when the event needs a redraw
    void request_redraw() { dirty_ = true; }
    bool take_redraw() { bool d = dirty_; dirty_ = false; return d; }
    void wake_after(int ms);                  // ask for a redraw in ms
    int wake_timeout(int default_ms) const;   // for SDL_WaitEventTimeout
    int win_w() const { return win_w_; }
    int win_h() const { return win_h_; }
    int S(double v) const { return g.px(v); } // scaled pixels
    uint32_t ticks() const { return SDL_GetTicks(); }

    // ---- layout ----
    void begin_region(Rect r);                // absolute region; cursor to its top-left
    void end_region();
    void regions_back_space(int px) { reg().space = px; }
    Rect region() const { return reg().bounds; }
    int avail_w() const;
    int avail_h() const;
    int cursor_x() const { return reg().cx; }
    int cursor_y() const { return reg().cy; }
    void same_line(int spacing = -1);
    void flow_reset() { flow_started_ = false; }
    bool button_flow(const std::string& label, Btn style = Btn::Secondary, bool enabled = true);   // buttons wrap onto new rows
    void spacing(int px = -1);
    void indent(int px);
    void unindent(int px);
    void new_line(int h);                     // vertical gap
    Rect item_rect(int w, int h);             // reserve a slot and advance the cursor
    Rect last_item() const { return last_item_; }
    bool hovered() const { return item_hovered_; }   // last item
    void columns(int n, int gap = -1);
    void next_column();
    void end_columns();
    void push_id(const std::string& s);
    void push_id(int i);
    void pop_id();
    uint64_t id(const std::string& s) const;

    // ---- scroll areas ----
    void begin_scroll(const std::string& key, int w, int h);   // w/h 0 = fill the remaining region
    void end_scroll();
    float& scroll_of(const std::string& key);
    void scroll_to(const std::string& key, float y);

    // ---- widgets ----
    void label(const std::string& s, Tone tone = Tone::Normal, int size = 0, Font f = Font::Regular);
    void label_color(const std::string& s, Color c, int size = 0, Font f = Font::Regular);
    void set_no_collapse(bool b) { no_collapse_ = b; }   // modals show their text in full
    void begin_over(Rect r);                              // draw following widgets into the overlay layer, blocking hover underneath
    void end_over();
    void wrapped(const std::string& s, Tone tone = Tone::Muted, int width = 0, int size = 0);
    void label_c(const std::string& s, Tone tone = Tone::Normal, Font f = Font::Regular, int min_w = 0);   // label sized/centered like a button, for use in control rows
    void heading(const std::string& s, int size = 0);
    void separator();
    bool button(const std::string& label, Btn style = Btn::Secondary, int w = 0, bool enabled = true);
    bool checkbox(const std::string& label, bool* v, bool enabled = true);
    bool toggle(const std::string& label, bool* v, bool enabled = true);
    bool radio(const std::string& label, int* v, int value);
    bool released() const { return released_; }   // the most recent slider was just let go
    bool segmented(const std::string& key, int* sel, const std::vector<std::string>& items);
    bool slider(const std::string& key, int* v, int lo, int hi, int w = 0, const std::string& suffix = "");
    bool slider_f(const std::string& key, float* v, float lo, float hi, int w = 0, int decimals = 1);
    bool spinbox(const std::string& key, int* v, int lo, int hi, int step = 1, int w = 0);
    bool input(const std::string& key, std::string* s, int w = 0, const std::string& hint = "", bool password = false);
    bool combo(const std::string& key, int* sel, const std::vector<std::string>& items, int w = 0);
    void progress(float frac, int w = 0, Color c = {0, 0, 0, 0});
    void pill(const std::string& text, Tone tone);
    void tooltip(const std::string& text);    // for the last item
    void log_view(const std::string& key, const std::vector<std::string>& lines, int h);

    // cards: draw a titled bordered panel around whatever is placed between begin and end
    void card_begin(const std::string& title, Tone border = Tone::Normal);
    void card_end();

    // ---- overlays ----
    bool begin_modal(const std::string& title, int w, int h);     // returns true while open; draw widgets, then end_modal
    void end_modal();
    bool popup_open(const std::string& key) const;
    void open_popup(const std::string& key);
    void close_popup();
    void overlay(const std::function<void()>& fn);               // draw into the overlay layer after the frame

    // ---- input queries ----
    int mouse_x() const { return mx_; }
    int mouse_y() const { return my_; }
    bool mouse_down() const { return mdown_; }
    bool clicked_in(Rect r) const;            // left click released inside r this frame
    bool right_clicked_in(Rect r) const;
    bool hover_in(Rect r) const;
    bool dragging() const { return active_ != 0; }
    int wheel() const { return wheel_; }
    void consume_wheel() { wheel_ = 0; }
    uint64_t focus() const { return focus_; }
    void set_focus(uint64_t id) { focus_ = id; }
    bool key_pressed(SDL_Keycode k) const;    // pressed this frame (not consumed by a text field)
    bool ctrl() const { return ctrl_; }
    void blocked(bool b) { blocked_ = b; }

    // ---- draw (deferred, so cards can insert a background behind content) ----
    using Cmd = std::function<void(Gfx&)>;
    size_t reserve();
    void set_cmd(size_t slot, Cmd c);
    void draw(Cmd c);
    void fill(Rect r, Color c, int radius = 0);
    void stroke(Rect r, Color c, int t = 1, int radius = 0);
    void text_at(int x, int y, const std::string& s, Color c, int size = 0, Font f = Font::Regular);
    int text_w(const std::string& s, int size = 0, Font f = Font::Regular) { return g.text_width(s, size ? size : base_size(), f); }
    int base_size() const { return S(13); }
    int line_h(int size = 0, Font f = Font::Regular) { return g.line_height(size ? size : base_size(), f); }
    Color tone_color(Tone t) const;
    std::vector<std::string> wrap_lines(const std::string& s, int width, int size, Font f);
    std::vector<std::string> wrap_lines_uncached(const std::string& s, int width, int size, Font f);
    std::unordered_map<std::string, std::vector<std::string>> wrap_cache_;

    struct Region {
        Rect bounds;
        int cx = 0, cy = 0;          // cursor (next item top-left unless same_line)
        int row_h = 0;               // height of the current row
        int row_y = 0;               // top of the current row
        int last_right = 0;          // right edge of the previous item
        int max_y = 0;               // content extent
        int indent = 0;
        int origin_y = 0;
        bool same = false;
        int space = 0;
    };

private:
    friend struct UiAccess;
    Region& reg() { return regions_.back(); }
    const Region& reg() const { return regions_.back(); }
    void finish_item(Rect r);
    bool item_hot(Rect r);
    void push_layer(std::vector<Cmd>* l) { layers_.push_back(l); }

    std::vector<Region> regions_;
    std::vector<Cmd> main_, over_;
    std::vector<std::vector<Cmd>*> layers_;
    std::vector<uint64_t> id_stack_;
    Rect last_item_;
    bool item_hovered_ = false;
    int win_w_ = 0, win_h_ = 0;
    int mx_ = 0, my_ = 0;
    bool mdown_ = false, mclick_ = false, mrelease_ = false, mright_ = false;
    int wheel_ = 0;
    bool ctrl_ = false, shift_ = false;
    uint64_t active_ = 0, focus_ = 0;
    std::set<uint64_t> collapsed_open_;
    bool no_collapse_ = false;
    bool released_ = false;
    bool flow_started_ = false;
    bool saved_in_overlay_ = false;
    bool in_overlay_ = false, dirty_ = true, blocked_ = false, modal_prev_ = false, modal_now_ = false;
    uint32_t wake_at_ = 0;
    std::vector<SDL_Keycode> keys_;
    std::string text_in_;
    std::vector<Rect> overlay_rects_prev_, overlay_rects_;
    std::string open_popup_;
    std::unordered_map<uint64_t, float> scroll_;
    struct TextState { int caret = 0, sel = -1; float scroll = 0; };
    std::unordered_map<uint64_t, TextState> text_;
    std::map<std::string, float> scroll_by_key_;
    uint64_t hover_id_ = 0;
    uint32_t hover_since_ = 0;
    struct ColState { int n = 1, i = 0, gap = 0, x0 = 0, w = 0, y0 = 0, max_y = 0; };
    std::vector<ColState> cols_;
    struct ScrollCtx { std::string key; Rect view; int origin_y; int bar_w; Rect saved_bounds; };
    std::vector<ScrollCtx> scrolls_;
    std::unordered_map<uint64_t, float> content_h_;
    struct CardCtx { size_t slot; int top; int left; int right; Tone tone; std::string title; };
    std::vector<CardCtx> cards_;
    struct ModalCtx { Rect box; };
    std::vector<ModalCtx> modals_;
    uint64_t drag_scroll_ = 0;
    int drag_off_ = 0;
};

}  // namespace tt::ui
