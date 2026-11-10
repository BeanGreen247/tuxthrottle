#include "ui.hpp"

#include <algorithm>
#include <cmath>
#include <ctime>
#include <cstdlib>
#include <fstream>
#include <sstream>

namespace tt::ui {

const char* kDefaultTheme = "Steam Navy";

static Theme make_theme(const char* name, uint32_t panel, uint32_t sunken, uint32_t hi, uint32_t fg, uint32_t muted, uint32_t border, uint32_t accent, bool follow = false) {
    Theme t;
    t.name = name;
    t.panel = hex(panel); t.sunken = hex(sunken); t.panel_hi = hex(hi); t.fg = hex(fg); t.muted = hex(muted);
    t.border = hex(border); t.border_hi = mix(hex(border), hex(fg), 0.35f); t.card = mix(hex(panel), hex(hi), 0.35f);
    t.chart_axis = mix(hex(muted), hex(panel), 0.4f);
    t.success = hex(0x3fb950); t.danger = hex(0xf85149); t.warning = hex(0xd29922); t.info = hex(0x58a6ff);
    t.secondary = hex(hi); t.help_amber = hex(0xe3b341); t.help_banner_bg = mix(hex(panel), hex(0xe3b341), 0.12f);
    t.accent = hex(accent);
    t.follow_desktop_accent = follow;
    return t;
}

const std::vector<Theme>& themes() {
    static const std::vector<Theme> v = {
        make_theme("Steam Navy", 0x171d25, 0x0e141b, 0x2a3441, 0xdfe3e8, 0x9aa7b5, 0x2f3a48, 0x1a9fff),
        make_theme("NVIDIA Green", 0x121212, 0x0a0a0a, 0x262626, 0xe8e8e8, 0xa0a0a0, 0x333333, 0x76b900),
        make_theme("BIOS Dark", 0x1b1b1f, 0x111114, 0x2c2c33, 0xe6e6e6, 0xa0a0aa, 0x3a3a44, 0xe0262e),
        make_theme("Desktop Accent", 0x1e2127, 0x14171b, 0x2d323b, 0xe4e6ea, 0x9ca3ae, 0x373d48, 0x3daee9, true),
    };
    return v;
}

const Theme& find_theme(const std::string& name) {
    for (const auto& t : themes()) if (t.name == name) return t;
    return themes()[0];
}

Color read_desktop_accent() {
    const char* home = std::getenv("HOME");
    if (!home) return hex(0x3daee9);
    std::ifstream f(std::string(home) + "/.config/kdeglobals");
    std::string line, sect;
    while (std::getline(f, line)) {
        if (!line.empty() && line[0] == '[') { sect = line; continue; }
        if (sect == "[General]" && line.rfind("AccentColor=", 0) == 0) {
            int r, g, b;
            if (std::sscanf(line.c_str() + 12, "%d,%d,%d", &r, &g, &b) == 3) return Color(static_cast<uint8_t>(r), static_cast<uint8_t>(g), static_cast<uint8_t>(b));
        }
    }
    return hex(0x3daee9);
}

// ---------------------------------------------------------------- helpers
static size_t utf8_prev(const std::string& s, size_t i) {
    if (i == 0) return 0;
    --i;
    while (i > 0 && (static_cast<unsigned char>(s[i]) & 0xC0) == 0x80) --i;
    return i;
}
static size_t utf8_next(const std::string& s, size_t i) {
    if (i >= s.size()) return s.size();
    ++i;
    while (i < s.size() && (static_cast<unsigned char>(s[i]) & 0xC0) == 0x80) ++i;
    return i;
}

static double now_ms_impl() { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6; }
double Ui::now_ms() { return now_ms_impl(); }

Ui::Ui(Gfx& gfx) : g(gfx), th(find_theme(kDefaultTheme)) { SDL_StartTextInput(); }

uint64_t Ui::id(const std::string& s) const {
    uint64_t h = 1469598103934665603ULL;
    for (uint64_t p : id_stack_) { h ^= p; h *= 1099511628211ULL; }
    for (unsigned char c : s) { h ^= c; h *= 1099511628211ULL; }
    return h ? h : 1;
}
void Ui::push_id(const std::string& s) { id_stack_.push_back(id(s)); }
void Ui::push_id(int i) { push_id(std::to_string(i)); }
void Ui::pop_id() { if (!id_stack_.empty()) id_stack_.pop_back(); }

Color Ui::tone_color(Tone t) const {
    Color c = th.fg;
    switch (t) {
        case Tone::Normal: c = th.fg; break;
        case Tone::Muted: c = th.muted; break;
        case Tone::Accent: c = th.accent; break;
        case Tone::Success: c = th.success; break;
        case Tone::Danger: c = th.danger; break;
        case Tone::Warning: c = th.warning; break;
        case Tone::Info: c = th.info; break;
        case Tone::Amber: c = th.help_amber; break;
    }
    return readable_on(c, th.panel, 4.5);
}

// ---------------------------------------------------------------- input
bool Ui::handle(const SDL_Event& e) {
    int ww = 1, wh = 1, ow = 1, oh = 1;
    SDL_GetWindowSize(g.window(), &ww, &wh);
    g.size(&ow, &oh);
    double fx = ww ? static_cast<double>(ow) / ww : 1, fy = wh ? static_cast<double>(oh) / wh : 1;
    switch (e.type) {
        case SDL_MOUSEMOTION: mx_ = static_cast<int>(e.motion.x * fx); my_ = static_cast<int>(e.motion.y * fy); return true;
        case SDL_MOUSEBUTTONDOWN:
            mx_ = static_cast<int>(e.button.x * fx); my_ = static_cast<int>(e.button.y * fy);
            if (e.button.button == SDL_BUTTON_LEFT) { mdown_ = true; mclick_ = true; }
            else if (e.button.button == SDL_BUTTON_RIGHT) mright_ = true;
            return true;
        case SDL_MOUSEBUTTONUP:
            mx_ = static_cast<int>(e.button.x * fx); my_ = static_cast<int>(e.button.y * fy);
            if (e.button.button == SDL_BUTTON_LEFT) { mdown_ = false; mrelease_ = true; }
            return true;
        case SDL_MOUSEWHEEL: wheel_ += e.wheel.y; return true;
        case SDL_KEYDOWN:
            keys_.push_back(e.key.keysym.sym);
            ctrl_ = (e.key.keysym.mod & KMOD_CTRL) != 0;
            shift_ = (e.key.keysym.mod & KMOD_SHIFT) != 0;
            return true;
        case SDL_TEXTINPUT: text_in_ += e.text.text; return true;
        case SDL_WINDOWEVENT: return true;
        default: return false;
    }
}

bool Ui::key_pressed(SDL_Keycode k) const { return std::find(keys_.begin(), keys_.end(), k) != keys_.end(); }
bool Ui::hover_in(Rect r) const { return r.contains(mx_, my_); }
bool Ui::clicked_in(Rect r) const { return mrelease_ && r.contains(mx_, my_) && modals_.empty() == (!modal_prev_ || !modals_.empty()); }
bool Ui::right_clicked_in(Rect r) const { return mright_ && r.contains(mx_, my_); }

void Ui::wake_after(int ms) {
    uint32_t t = SDL_GetTicks() + static_cast<uint32_t>(std::max(1, ms));
    if (!wake_at_ || t < wake_at_) wake_at_ = t;
}
int Ui::wake_timeout(int def) const {
    if (!wake_at_) return def;
    int64_t d = static_cast<int64_t>(wake_at_) - static_cast<int64_t>(SDL_GetTicks());
    return static_cast<int>(std::clamp<int64_t>(d, 0, def));
}

void Ui::begin_frame() {
    frame_t0_ = now_ms();
    g.size(&win_w_, &win_h_);
    main_.clear();
    over_.clear();
    layers_.assign(1, &main_);
    regions_.clear();
    cols_.clear();
    scrolls_.clear();
    cards_.clear();
    modals_.clear();
    overlay_rects_.clear();
    id_stack_.clear();
    wake_at_ = 0;
    modal_now_ = false;
    in_overlay_ = false;
    begin_region({0, 0, win_w_, win_h_});
}

void Ui::end_frame() {
    end_region();
    double t0 = now_ms();
    layout_ms = t0 - frame_t0_;
    g.begin(th.panel);
    for (auto& c : main_) c(g);
    for (auto& c : over_) c(g);
    if (before_present) before_present();
    double t1 = now_ms();
    g.present();
    cmds_ms = t1 - t0;
    present_ms = now_ms() - t1;
    n_cmds = static_cast<int>(main_.size() + over_.size());
    mclick_ = mrelease_ = mright_ = false;
    if (!mdown_ && active_ && mrelease_ == false) { /* keep active until a release is seen */ }
    wheel_ = 0;
    keys_.clear();
    text_in_.clear();
    overlay_rects_prev_ = overlay_rects_;
    modal_prev_ = modal_now_;
}

// ---------------------------------------------------------------- layout
void Ui::begin_region(Rect r) {
    Region R;
    R.bounds = r;
    R.cy = r.y;
    R.row_y = r.y;
    R.max_y = r.y;
    R.origin_y = r.y;
    R.space = S(8);
    regions_.push_back(R);
}
void Ui::end_region() { if (!regions_.empty()) regions_.pop_back(); }

int Ui::avail_w() const {
    const Region& R = reg();
    int left = R.same ? R.last_right + R.space : R.bounds.x + R.indent;
    return std::max(0, R.bounds.right() - left);
}
int Ui::avail_h() const { return std::max(0, reg().bounds.bottom() - reg().cy); }

void Ui::same_line(int sp) {
    Region& R = reg();
    R.same = true;
    if (sp >= 0) R.last_right += sp - R.space;
}
bool Ui::button_flow(const std::string& lab, Btn style, bool enabled) {
    int w = g.text_width(lab, base_size(), Font::Regular) + S(28);
    if (flow_started_) {
        Region& R = reg();
        R.same = R.last_right + R.space + w <= R.bounds.right();
    }
    flow_started_ = true;
    return button(lab, style, 0, enabled);
}

void Ui::spacing(int px) { reg().cy += px < 0 ? reg().space : px; reg().row_h = 0; reg().row_y = reg().cy; reg().same = false; }
void Ui::new_line(int h) { reg().cy += h; reg().row_h = 0; reg().row_y = reg().cy; reg().same = false; }
void Ui::indent(int px) { reg().indent += px; }
void Ui::unindent(int px) { reg().indent -= px; }

Rect Ui::item_rect(int w, int h) {
    Region& R = reg();
    Rect r;
    if (R.same) {
        r = {R.last_right + R.space, R.row_y, w, h};
        R.row_h = std::max(R.row_h, h);
    } else {
        r = {R.bounds.x + R.indent, R.cy, w, h};
        R.row_y = r.y;
        R.row_h = h;
    }
    R.same = false;
    R.last_right = r.right();
    R.cy = R.row_y + R.row_h + R.space;
    R.max_y = std::max(R.max_y, R.row_y + R.row_h);
    last_item_ = r;
    item_hovered_ = item_hot(r);
    return r;
}

void Ui::columns(int n, int gap) {
    Region& R = reg();
    ColState c;
    c.n = std::max(1, n);
    c.gap = gap < 0 ? S(16) : gap;
    c.x0 = R.bounds.x + R.indent;
    c.w = R.bounds.right() - c.x0;
    c.y0 = R.cy;
    c.max_y = R.cy;
    cols_.push_back(c);
    R.same = false;
    int cw = (c.w - c.gap * (c.n - 1)) / c.n;
    R.bounds.x = c.x0 - R.indent;
    R.bounds.w = cw + R.indent;
    cols_.back().w = c.w;
}

void Ui::next_column() {
    if (cols_.empty()) return;
    ColState& c = cols_.back();
    Region& R = reg();
    c.max_y = std::max(c.max_y, R.cy);
    ++c.i;
    int cw = (c.w - c.gap * (c.n - 1)) / c.n;
    R.bounds.x = c.x0 + c.i * (cw + c.gap) - R.indent;
    R.bounds.w = cw + R.indent;
    R.cy = c.y0;
    R.row_y = c.y0;
    R.row_h = 0;
    R.same = false;
}

void Ui::end_columns() {
    if (cols_.empty()) return;
    ColState c = cols_.back();
    cols_.pop_back();
    Region& R = reg();
    R.bounds.x = c.x0 - R.indent;
    R.bounds.w = c.w + R.indent;
    R.cy = std::max(c.max_y, R.cy);
    R.row_y = R.cy;
    R.row_h = 0;
    R.same = false;
}

bool Ui::item_hot(Rect r) {
    if (blocked_) return false;
    if (modals_.empty() && modal_prev_) return false;
    if (!in_overlay_) {
        for (const auto& o : overlay_rects_prev_) if (o.contains(mx_, my_)) return false;
        for (const auto& o : overlay_rects_) if (o.contains(mx_, my_)) return false;
    }
    if (!scrolls_.empty() && !scrolls_.back().view.contains(mx_, my_)) return false;
    return r.contains(mx_, my_);
}

// ---------------------------------------------------------------- drawing
size_t Ui::reserve() { layers_.back()->push_back(nullptr); return layers_.back()->size() - 1; }
void Ui::set_cmd(size_t slot, Cmd c) { (*layers_.back())[slot] = std::move(c); }
void Ui::draw(Cmd c) { layers_.back()->push_back(std::move(c)); }
void Ui::fill(Rect r, Color c, int radius) { draw([=](Gfx& gg) { gg.fill(r, c, radius); }); }
void Ui::stroke(Rect r, Color c, int t, int radius) { draw([=](Gfx& gg) { gg.stroke(r, c, t, radius); }); }
void Ui::text_at(int x, int y, const std::string& s, Color c, int size, Font f) {
    int sz = size ? size : base_size();
    draw([=](Gfx& gg) { gg.text(x, y, s, sz, c, f); });
}

std::vector<std::string> Ui::wrap_lines(const std::string& s, int width, int size, Font f) {
    // the same paragraphs are re-wrapped on every frame; keep the result per (text, width, size, font)
    std::string key = std::to_string(width) + '|' + std::to_string(size) + '|' + std::to_string(static_cast<int>(f)) + '|' + s;
    auto hit = wrap_cache_.find(key);
    if (hit != wrap_cache_.end()) return hit->second;
    if (wrap_cache_.size() > 4000) wrap_cache_.clear();
    auto res = wrap_lines_uncached(s, width, size, f);
    wrap_cache_.emplace(std::move(key), res);
    return res;
}

std::vector<std::string> Ui::wrap_lines_uncached(const std::string& s, int width, int size, Font f) {
    std::vector<std::string> out;
    std::istringstream paras(s);
    std::string para;
    bool any = false;
    while (std::getline(paras, para)) {
        any = true;
        std::string cur;
        std::istringstream words(para);
        std::string w;
        while (words >> w) {
            std::string trial = cur.empty() ? w : cur + " " + w;
            if (!cur.empty() && g.text_width(trial, size, f) > width) { out.push_back(cur); cur = w; }
            else cur = trial;
            while (g.text_width(cur, size, f) > width && width > 20) {   // very long token
                int n = g.fit_chars(cur, width, size, f);
                if (n <= 0) break;
                out.push_back(cur.substr(0, static_cast<size_t>(n)));
                cur = cur.substr(static_cast<size_t>(n));
            }
        }
        out.push_back(cur);
    }
    if (!any) out.push_back("");
    return out;
}

// ---------------------------------------------------------------- labels
void Ui::label_color(const std::string& s, Color c, int size, Font f) {
    int sz = size ? size : base_size();
    Rect r = item_rect(g.text_width(s, sz, f), g.line_height(sz, f));
    text_at(r.x, r.y, s, c, sz, f);
}
void Ui::label(const std::string& s, Tone tone, int size, Font f) {
    int sz = size ? size : base_size();
    int lh = g.line_height(sz, f);
    bool same = reg().same;
    int row_h = reg().row_h;
    Rect r = item_rect(g.text_width(s, sz, f), lh);
    int y = same && row_h > lh ? r.y + (row_h - lh) / 2 : r.y;
    text_at(r.x, y, s, tone_color(tone), sz, f);
}

void Ui::label_c(const std::string& s, Tone tone, Font f, int min_w) {
    int lh = g.line_height(base_size(), f), h = g.line_height(base_size(), Font::Regular) + S(14);
    Rect r = item_rect(std::max(min_w, g.text_width(s, base_size(), f)), h);
    text_at(r.x, r.y + (h - lh) / 2, s, tone_color(tone), base_size(), f);
}
void Ui::heading(const std::string& s, int size) { label_color(s, th.fg, size ? size : S(20), Font::Display); }

void Ui::wrapped(const std::string& s, Tone tone, int width, int size) {
    int sz = size ? size : base_size();
    int w = width ? width : avail_w();
    auto lines = wrap_lines(s, w, sz, Font::Regular);
    int lh = g.line_height(sz, Font::Regular);
    Color c = tone_color(tone);
    // long explanatory text collapses to two lines with a "more" link
    bool collapsible = tone == Tone::Muted && lines.size() > 2 && !no_collapse_;
    uint64_t hid = id(s);
    bool open = collapsed_open_.count(hid) > 0;
    if (!collapsible || open) {
        int n = static_cast<int>(lines.size()) + (collapsible ? 1 : 0);
        Rect r = item_rect(w, lh * n);
        for (size_t i = 0; i < lines.size(); ++i) text_at(r.x, r.y + static_cast<int>(i) * lh, lines[i], c, sz);
        if (collapsible) {
            Rect lr = {r.x, r.y + static_cast<int>(lines.size()) * lh, g.text_width("less", sz, Font::Bold) + S(8), lh};
            text_at(lr.x, lr.y, "less", th.accent, sz, Font::Bold);
            if (item_hovered_ && clicked_in(lr)) collapsed_open_.erase(hid);
        }
        return;
    }
    Rect r = item_rect(w, lh * 2);
    text_at(r.x, r.y, lines[0], c, sz);
    std::string more = "  more";
    int mw = g.text_width(more, sz, Font::Bold);
    std::string l2 = g.ellipsize(lines[1] + " ...", w - mw, sz, Font::Regular);
    int x2 = r.x + g.text_width(l2, sz, Font::Regular);
    text_at(r.x, r.y + lh, l2, c, sz);
    text_at(x2, r.y + lh, more, th.accent, sz, Font::Bold);
    if (item_hovered_ && clicked_in(r)) collapsed_open_.insert(hid);
}

void Ui::separator() {
    Rect r = item_rect(avail_w(), 1);
    fill(r, th.border);
}

void Ui::pill(const std::string& text, Tone tone) {
    int sz = S(11);
    int tw = g.text_width(text, sz, Font::Bold);
    Rect r = item_rect(tw + S(14), g.line_height(sz, Font::Bold) + S(4));
    Color c = tone_color(tone);
    fill(r, alpha(c, 40), r.h / 2);
    stroke(r, alpha(c, 120), 1, r.h / 2);
    text_at(r.x + S(7), r.y + S(2), text, c, sz, Font::Bold);
}

// ---------------------------------------------------------------- buttons & toggles
bool Ui::button(const std::string& lab, Btn style, int w, bool enabled) {
    int tw = g.text_width(lab, base_size(), Font::Regular);
    int h = g.line_height(base_size(), Font::Regular) + S(14);
    Rect r = item_rect(w ? w : tw + S(28), h);
    uint64_t bid = id(lab) ^ (static_cast<uint64_t>(r.x) * 31 + static_cast<uint64_t>(r.y));
    bool hot = enabled && item_hovered_;
    bool clicked = false;
    if (hot && mclick_) active_ = bid;
    if (active_ == bid && mrelease_) { clicked = hot; active_ = 0; }
    bool down = active_ == bid && hot;

    Color base = th.panel_hi, border = th.border, text = th.fg;
    bool outline = false;
    auto solid = [&](Color c) { base = c; border = c; text = readable_on(Color(255, 255, 255), c, 4.5); if (contrast_ratio(Color(0, 0, 0), c) > contrast_ratio(Color(255, 255, 255), c)) text = readable_on(Color(0, 0, 0), c, 4.5); };
    auto out = [&](Color c) { outline = true; base = Color(0, 0, 0, 0); border = c; text = readable_on(c, th.panel, 4.5); };
    switch (style) {
        case Btn::Primary: solid(th.accent); break;
        case Btn::Secondary: break;
        case Btn::Success: solid(th.success); break;
        case Btn::Danger: solid(th.danger); break;
        case Btn::Warning: solid(th.warning); break;
        case Btn::Info: solid(th.info); break;
        case Btn::Outline: out(th.accent); break;
        case Btn::OutlineInfo: out(th.info); break;
        case Btn::OutlineWarn: out(th.warning); break;
        case Btn::OutlineDanger: out(th.danger); break;
        case Btn::OutlineSuccess: out(th.success); break;
        case Btn::Ghost: base = Color(0, 0, 0, 0); border = Color(0, 0, 0, 0); text = th.muted; outline = true; break;
    }
    Color fillc = base;
    if (hot) fillc = outline ? alpha(border.a ? border : th.accent, down ? 70 : 40) : mix(base, Color(255, 255, 255), down ? 0.0f : 0.12f);
    if (style == Btn::Ghost && hot) { fillc = th.panel_hi; text = th.fg; }
    if (!enabled) { fillc = outline ? Color(0, 0, 0, 0) : mix(base, th.panel, 0.6f); border = mix(border, th.panel, 0.6f); text = mix(text, th.panel, 0.55f); }
    fill(r, fillc, S(5));
    if (border.a) stroke(r, border, 1, S(5));
    text_at(r.x + (r.w - tw) / 2, r.y + (r.h - g.line_height(base_size(), Font::Regular)) / 2, lab, text);
    return clicked && enabled;
}

bool Ui::checkbox(const std::string& lab, bool* v, bool enabled) {
    int box = S(18), tw = g.text_width(lab, base_size(), Font::Regular), lh = g.line_height(base_size(), Font::Regular);
    Rect r = item_rect(box + S(8) + tw, std::max(box, lh));
    uint64_t cid = id(lab) ^ static_cast<uint64_t>(r.x * 131 + r.y);
    bool hot = enabled && item_hovered_, changed = false;
    if (hot && mclick_) active_ = cid;
    if (active_ == cid && mrelease_) { if (hot) { *v = !*v; changed = true; } active_ = 0; }
    Rect b = {r.x, r.y + (r.h - box) / 2, box, box};
    Color edge = *v ? th.accent : (hot ? th.border_hi : th.border);
    fill(b, *v ? th.accent : th.sunken, S(4));
    stroke(b, enabled ? edge : th.border, 1, S(4));
    if (*v) {
        Color tick = readable_on(Color(255, 255, 255), th.accent, 3.0);
        float x = static_cast<float>(b.x), y = static_cast<float>(b.y), s = static_cast<float>(box);
        draw([=](Gfx& gg) { gg.polyline({{x + s * 0.24f, y + s * 0.52f}, {x + s * 0.43f, y + s * 0.70f}, {x + s * 0.76f, y + s * 0.30f}}, tick, std::max(2.0f, s * 0.12f)); });
    }
    text_at(r.x + box + S(8), r.y + (r.h - lh) / 2, lab, enabled ? th.fg : th.muted);
    return changed;
}

bool Ui::toggle(const std::string& lab, bool* v, bool enabled) {
    int tw_ = S(38), th_ = S(20), lh = g.line_height(base_size(), Font::Regular), tw = g.text_width(lab, base_size(), Font::Regular);
    Rect r = item_rect(tw_ + S(10) + tw, std::max(th_, lh));
    uint64_t tid = id(lab) ^ static_cast<uint64_t>(r.x * 131 + r.y);
    bool hot = enabled && item_hovered_, changed = false;
    if (hot && mclick_) active_ = tid;
    if (active_ == tid && mrelease_) { if (hot) { *v = !*v; changed = true; } active_ = 0; }
    Rect t = {r.x, r.y + (r.h - th_) / 2, tw_, th_};
    fill(t, *v ? th.accent : th.panel_hi, th_ / 2);
    stroke(t, hot ? th.border_hi : th.border, 1, th_ / 2);
    float kx = static_cast<float>(*v ? t.right() - th_ / 2 : t.x + th_ / 2);
    float ky = static_cast<float>(t.y + th_ / 2), kr = static_cast<float>(th_) / 2 - 3;
    Color knob = *v ? readable_on(Color(255, 255, 255), th.accent, 3.0) : th.muted;
    draw([=](Gfx& gg) { gg.disc(kx, ky, kr, knob); });
    text_at(r.x + tw_ + S(10), r.y + (r.h - lh) / 2, lab, enabled ? th.fg : th.muted);
    return changed;
}

bool Ui::radio(const std::string& lab, int* v, int value) {
    int d = S(18), lh = g.line_height(base_size(), Font::Regular), tw = g.text_width(lab, base_size(), Font::Regular);
    Rect r = item_rect(d + S(8) + tw, std::max(d, lh));
    uint64_t rid = id(lab) ^ static_cast<uint64_t>(r.x * 131 + r.y);
    bool hot = item_hovered_, changed = false;
    if (hot && mclick_) active_ = rid;
    if (active_ == rid && mrelease_) { if (hot && *v != value) { *v = value; changed = true; } active_ = 0; }
    float cx = static_cast<float>(r.x + d / 2), cy = static_cast<float>(r.y + r.h / 2), rad = static_cast<float>(d) / 2;
    bool on = *v == value;
    Color edge = on ? th.accent : (hot ? th.border_hi : th.border), inner = th.accent, bg = th.sunken;
    draw([=](Gfx& gg) {
        gg.disc(cx, cy, rad, edge);
        gg.disc(cx, cy, rad - 1.5f, bg);
        if (on) gg.disc(cx, cy, rad - 5, inner);
    });
    text_at(r.x + d + S(8), r.y + (r.h - lh) / 2, lab, th.fg);
    return changed;
}

bool Ui::slider_f(const std::string& key, float* v, float lo, float hi, int w, int decimals) {
    released_ = false;
    int total = w ? w : S(260), valw = S(64), lh = g.line_height(base_size(), Font::Regular);
    Rect r = item_rect(total, lh + S(14));
    uint64_t sid = id(key);
    Rect track = {r.x + S(8), r.y + r.h / 2 - S(3), r.w - valw - S(16), S(6)};
    Rect hit = {r.x, r.y, r.w - valw, r.h};
    bool hot = item_hovered_ && hit.contains(mx_, my_), changed = false;
    if (hot && mclick_) active_ = sid;
    if (active_ == sid) {
        if (mdown_) {
            float t = std::clamp(static_cast<float>(mx_ - track.x) / static_cast<float>(std::max(1, track.w)), 0.0f, 1.0f);
            float nv = lo + (hi - lo) * t;
            if (decimals == 0) nv = std::round(nv);
            if (nv != *v) { *v = nv; changed = true; }
        }
        if (mrelease_) { active_ = 0; released_ = true; }
    }
    float frac = hi > lo ? std::clamp((*v - lo) / (hi - lo), 0.0f, 1.0f) : 0;
    fill(track, th.sunken, track.h / 2);
    fill({track.x, track.y, static_cast<int>(track.w * frac), track.h}, th.accent, track.h / 2);
    float kx = static_cast<float>(track.x) + static_cast<float>(track.w) * frac, ky = static_cast<float>(track.y + track.h / 2);
    Color kc = active_ == sid || hot ? mix(th.accent, Color(255, 255, 255), 0.35f) : th.fg;
    float kr = static_cast<float>(S(8));
    draw([=](Gfx& gg) { gg.disc(kx, ky, kr, kc); });
    char b[32];
    std::snprintf(b, sizeof b, "%.*f", decimals, static_cast<double>(*v));
    text_at(r.right() - valw + S(10), r.y + (r.h - lh) / 2, b, th.fg);
    return changed;
}

bool Ui::slider(const std::string& key, int* v, int lo, int hi, int w, const std::string& suffix) {
    released_ = false;
    float f = static_cast<float>(*v);
    int total = w ? w : S(260), valw = S(64), lh = g.line_height(base_size(), Font::Regular);
    // draw via slider_f but replace its value text with the suffix-aware one
    Rect r = item_rect(total, lh + S(14));
    uint64_t sid = id(key);
    Rect track = {r.x + S(8), r.y + r.h / 2 - S(3), r.w - valw - S(16), S(6)};
    bool hot = item_hovered_ && Rect{r.x, r.y, r.w - valw, r.h}.contains(mx_, my_), changed = false;
    if (hot && mclick_) active_ = sid;
    if (active_ == sid) {
        if (mdown_) {
            float t = std::clamp(static_cast<float>(mx_ - track.x) / static_cast<float>(std::max(1, track.w)), 0.0f, 1.0f);
            int nv = static_cast<int>(std::lround(lo + (hi - lo) * t));
            if (nv != *v) { *v = nv; changed = true; }
        }
        if (mrelease_) { active_ = 0; released_ = true; }
    }
    f = static_cast<float>(*v);
    float frac = hi > lo ? std::clamp((f - static_cast<float>(lo)) / static_cast<float>(hi - lo), 0.0f, 1.0f) : 0;
    fill(track, th.sunken, track.h / 2);
    fill({track.x, track.y, static_cast<int>(track.w * frac), track.h}, th.accent, track.h / 2);
    float kx = static_cast<float>(track.x) + static_cast<float>(track.w) * frac, ky = static_cast<float>(track.y + track.h / 2), kr = static_cast<float>(S(8));
    Color kc = active_ == sid || hot ? mix(th.accent, Color(255, 255, 255), 0.35f) : th.fg;
    draw([=](Gfx& gg) { gg.disc(kx, ky, kr, kc); });
    text_at(r.right() - valw + S(10), r.y + (r.h - lh) / 2, std::to_string(*v) + suffix, th.fg);
    return changed;
}

bool Ui::segmented(const std::string& key, int* sel, const std::vector<std::string>& items) {
    bool changed = false;
    push_id(key);
    for (size_t i = 0; i < items.size(); ++i) {
        if (i) same_line(S(4));
        bool on = static_cast<int>(i) == *sel;
        if (button(items[i], on ? Btn::Primary : Btn::Secondary)) { if (!on) { *sel = static_cast<int>(i); changed = true; } }
    }
    pop_id();
    return changed;
}

bool Ui::spinbox(const std::string& key, int* v, int lo, int hi, int step, int w) {
    int bw = S(26), h = g.line_height(base_size(), Font::Regular) + S(12);
    Rect r = item_rect(w ? w : S(120), h);
    uint64_t sid = id(key);
    Rect minus = {r.x, r.y, bw, r.h}, plus = {r.right() - bw, r.y, bw, r.h}, mid = {r.x + bw, r.y, r.w - 2 * bw, r.h};
    bool changed = false;
    auto press = [&](Rect b, int dir, uint64_t k) {
        bool hot = item_hovered_ && b.contains(mx_, my_);
        if (hot && mclick_) active_ = sid ^ k;
        if (active_ == (sid ^ k) && mrelease_) { if (hot) { int nv = std::clamp(*v + dir * step, lo, hi); if (nv != *v) { *v = nv; changed = true; } } active_ = 0; }
        fill(b, hot ? th.panel_hi : th.card, S(4));
    };
    fill(r, th.sunken, S(4));
    stroke(r, th.border, 1, S(4));
    press(minus, -1, 1);
    press(plus, 1, 2);
    if (item_hovered_ && wheel_ != 0 && mid.contains(mx_, my_)) { int nv = std::clamp(*v + wheel_ * step, lo, hi); if (nv != *v) { *v = nv; changed = true; } wheel_ = 0; }
    std::string s = std::to_string(*v);
    int lh = g.line_height(base_size(), Font::Regular);
    text_at(minus.x + (bw - g.text_width("-", base_size())) / 2, r.y + (r.h - lh) / 2, "-", th.fg);
    text_at(plus.x + (bw - g.text_width("+", base_size())) / 2, r.y + (r.h - lh) / 2, "+", th.fg);
    text_at(mid.x + (mid.w - g.text_width(s, base_size())) / 2, r.y + (r.h - lh) / 2, s, th.fg);
    return changed;
}

// ---------------------------------------------------------------- text input
bool Ui::input(const std::string& key, std::string* s, int w, const std::string& hint, bool password) {
    int lh = g.line_height(base_size(), Font::Regular), pad = S(8);
    Rect r = item_rect(w ? w : S(240), lh + S(12));
    uint64_t iid = id(key);
    TextState& st = text_[iid];
    bool hot = item_hovered_, changed = false;
    if (mclick_) {
        if (hot) { focus_ = iid; active_ = iid; }
        else if (focus_ == iid && modals_.empty() == (!modal_prev_ || !modals_.empty())) focus_ = 0;
    }
    std::string shown = *s;
    if (password) { shown.clear(); for (size_t i = 0; i < s->size();) { i = utf8_next(*s, i); shown += "\xE2\x80\xA2"; } }
    auto shown_pos = [&](size_t byte) {   // map caret byte offset in *s to byte offset in shown
        if (!password) return byte;
        size_t n = 0;
        for (size_t i = 0; i < byte && i < s->size(); i = utf8_next(*s, i)) ++n;
        return n * 3;
    };
    if (focus_ == iid) {
        st.caret = std::clamp(st.caret, 0, static_cast<int>(s->size()));
        size_t c = static_cast<size_t>(st.caret);
        bool all = st.sel == 0;
        if (!text_in_.empty()) {
            if (all) { s->clear(); c = 0; st.sel = -1; }
            s->insert(c, text_in_);
            c += text_in_.size();
            changed = true;
        }
        for (SDL_Keycode k : keys_) {
            if (ctrl_ && k == SDLK_a) { st.sel = 0; continue; }
            if (ctrl_ && k == SDLK_v) {
                char* clip = SDL_GetClipboardText();
                if (clip) { std::string t = clip; SDL_free(clip); t.erase(std::remove(t.begin(), t.end(), '\n'), t.end()); if (st.sel == 0) { s->clear(); c = 0; st.sel = -1; } s->insert(c, t); c += t.size(); changed = true; }
                continue;
            }
            if (ctrl_ && (k == SDLK_c || k == SDLK_x)) { SDL_SetClipboardText(s->c_str()); if (k == SDLK_x) { s->clear(); c = 0; changed = true; } continue; }
            if (k == SDLK_BACKSPACE) {
                if (all) { s->clear(); c = 0; changed = true; st.sel = -1; }
                else if (c > 0) { size_t p = utf8_prev(*s, c); s->erase(p, c - p); c = p; changed = true; }
            } else if (k == SDLK_DELETE) {
                if (all) { s->clear(); c = 0; changed = true; st.sel = -1; }
                else if (c < s->size()) { size_t n = utf8_next(*s, c); s->erase(c, n - c); changed = true; }
            } else if (k == SDLK_LEFT) { c = utf8_prev(*s, c); st.sel = -1; }
            else if (k == SDLK_RIGHT) { c = utf8_next(*s, c); st.sel = -1; }
            else if (k == SDLK_HOME) { c = 0; st.sel = -1; }
            else if (k == SDLK_END) { c = s->size(); st.sel = -1; }
        }
        st.caret = static_cast<int>(c);
        wake_after(500);
    }
    // click positions the caret
    if (active_ == iid && mclick_ && hot) {
        int rel = mx_ - (r.x + pad) + static_cast<int>(st.scroll);
        size_t best = 0;
        for (size_t i = 0; i <= s->size(); i = utf8_next(*s, i)) {
            if (g.text_width(shown.substr(0, shown_pos(i)), base_size()) <= rel) best = i;
            if (i >= s->size()) break;
        }
        st.caret = static_cast<int>(best);
        st.sel = -1;
    }
    if (active_ == iid && mrelease_) active_ = 0;

    bool focused = focus_ == iid;
    fill(r, th.sunken, S(4));
    stroke(r, focused ? th.accent : (hot ? th.border_hi : th.border), focused ? 2 : 1, S(4));
    int inner_w = r.w - 2 * pad;
    int caret_x = g.text_width(shown.substr(0, shown_pos(static_cast<size_t>(st.caret))), base_size());
    if (caret_x - st.scroll > inner_w - 2) st.scroll = static_cast<float>(caret_x - inner_w + 2);
    if (caret_x - st.scroll < 0) st.scroll = static_cast<float>(caret_x);
    if (g.text_width(shown, base_size()) <= inner_w) st.scroll = 0;
    Rect clip = {r.x + pad, r.y, inner_w, r.h};
    int tx = r.x + pad - static_cast<int>(st.scroll), ty = r.y + (r.h - lh) / 2;
    draw([=](Gfx& gg) { gg.push_clip(clip); });
    if (s->empty() && !focused && !hint.empty()) text_at(r.x + pad, ty, hint, th.muted);
    else {
        if (st.sel == 0 && focused && !s->empty()) fill({tx, ty, g.text_width(shown, base_size()), lh}, alpha(th.accent, 90));
        text_at(tx, ty, shown, th.fg);
    }
    if (focused && (SDL_GetTicks() / 500) % 2 == 0) fill({tx + caret_x, ty, S(2) > 1 ? S(2) : 1, lh}, th.fg);
    draw([=](Gfx& gg) { gg.pop_clip(); });
    return changed;
}

// ---------------------------------------------------------------- combo / popups
bool Ui::popup_open(const std::string& key) const { return open_popup_ == key; }
void Ui::open_popup(const std::string& key) { open_popup_ = key; }
void Ui::close_popup() { open_popup_.clear(); }

bool Ui::combo(const std::string& key, int* sel, const std::vector<std::string>& items, int w) {
    int lh = g.line_height(base_size(), Font::Regular);
    int widest = 0;
    for (const auto& it : items) widest = std::max(widest, g.text_width(it, base_size()));
    Rect r = item_rect(w ? w : std::max(S(140), widest + S(44)), lh + S(14));
    std::string pk = std::to_string(id(key));
    bool open = open_popup_ == pk, hot = item_hovered_, changed = false;
    if (hot && mclick_ && !open) { open_popup_ = pk; open = true; mclick_ = false; }
    else if (hot && mclick_ && open) { close_popup(); open = false; mclick_ = false; }
    fill(r, hot || open ? th.panel_hi : th.card, S(5));
    stroke(r, open ? th.accent : th.border, 1, S(5));
    std::string cur = (*sel >= 0 && *sel < static_cast<int>(items.size())) ? items[static_cast<size_t>(*sel)] : "";
    text_at(r.x + S(10), r.y + (r.h - lh) / 2, g.ellipsize(cur, r.w - S(34), base_size(), Font::Regular), th.fg);
    float ax = static_cast<float>(r.right() - S(16)), ay = static_cast<float>(r.y + r.h / 2), as = static_cast<float>(S(4));
    Color ac = th.muted;
    draw([=](Gfx& gg) { gg.triangle({ax - as, ay - as / 2}, {ax + as, ay - as / 2}, {ax, ay + as}, ac); });
    if (!open) return false;

    int rows = std::min<int>(static_cast<int>(items.size()), 12), rh = lh + S(10);
    Rect pop = {r.x, r.bottom() + 2, std::max(r.w, widest + S(24)), rows * rh + 4};
    if (pop.bottom() > win_h_ && r.y - pop.h - 2 > 0) pop.y = r.y - pop.h - 2;
    overlay_rects_.push_back(pop);
    float& off = scroll_[id(key) ^ 0x9e3779b9];
    int maxoff = std::max(0, static_cast<int>(items.size()) - rows);
    if (pop.contains(mx_, my_) && wheel_ != 0) { off = std::clamp(off - static_cast<float>(wheel_), 0.0f, static_cast<float>(maxoff)); wheel_ = 0; }
    int first = static_cast<int>(off);
    bool picked = false;
    layers_.push_back(&over_);
    fill(pop, th.card, S(5));
    stroke(pop, th.border_hi, 1, S(5));
    for (int i = 0; i < rows; ++i) {
        int idx = first + i;
        Rect row = {pop.x + 2, pop.y + 2 + i * rh, pop.w - 4, rh};
        bool rhot = row.contains(mx_, my_);
        if (rhot) fill(row, alpha(th.accent, 70), S(4));
        else if (idx == *sel) fill(row, alpha(th.accent, 35), S(4));
        text_at(row.x + S(8), row.y + (rh - lh) / 2, g.ellipsize(items[static_cast<size_t>(idx)], row.w - S(16), base_size(), Font::Regular), th.fg);
        if (rhot && mrelease_) { *sel = idx; changed = true; picked = true; }
    }
    layers_.pop_back();
    if (picked) close_popup();
    else if (mclick_ && !pop.contains(mx_, my_)) close_popup();
    return changed;
}

void Ui::tooltip(const std::string& text) {
    Rect r = last_item_;
    if (!r.contains(mx_, my_) || !item_hovered_) return;
    uint64_t hid = static_cast<uint64_t>(r.x) * 73856093ULL ^ static_cast<uint64_t>(r.y) * 19349663ULL;
    if (hover_id_ != hid) { hover_id_ = hid; hover_since_ = SDL_GetTicks(); }
    uint32_t el = SDL_GetTicks() - hover_since_;
    if (el < 450) { wake_after(static_cast<int>(450 - el)); return; }
    int sz = S(12), maxw = S(380);
    auto lines = wrap_lines(text, maxw, sz, Font::Regular);
    int lh = g.line_height(sz, Font::Regular), tw = 0;
    for (const auto& l : lines) tw = std::max(tw, g.text_width(l, sz, Font::Regular));
    Rect box = {mx_ + S(14), my_ + S(18), tw + S(20), lh * static_cast<int>(lines.size()) + S(14)};
    if (box.right() > win_w_) box.x = win_w_ - box.w - 4;
    if (box.bottom() > win_h_) box.y = my_ - box.h - 6;
    layers_.push_back(&over_);
    fill(box, th.card, S(5));
    stroke(box, th.border_hi, 1, S(5));
    for (size_t i = 0; i < lines.size(); ++i) text_at(box.x + S(10), box.y + S(7) + static_cast<int>(i) * lh, lines[i], th.fg, sz);
    layers_.pop_back();
}

void Ui::progress(float frac, int w, Color c) {
    Rect r = item_rect(w ? w : S(220), S(8));
    fill(r, th.sunken, r.h / 2);
    Color col = c.a ? c : th.accent;
    int fw = static_cast<int>(static_cast<float>(r.w) * std::clamp(frac, 0.0f, 1.0f));
    if (fw > 0) fill({r.x, r.y, std::max(fw, r.h), r.h}, col, r.h / 2);
}

// ---------------------------------------------------------------- scroll areas
float& Ui::scroll_of(const std::string& key) { return scroll_[id(key)]; }
void Ui::scroll_to(const std::string& key, float y) { scroll_[id(key)] = y; }

void Ui::begin_scroll(const std::string& key, int w, int h) {
    int ww = w ? w : avail_w(), hh = h ? h : std::max(S(40), avail_h());
    Rect view = item_rect(ww, hh);
    uint64_t sid = id(key);
    float& sc = scroll_[sid];
    int bar_w = S(10);
    bool bar = content_h_.count(sid) && content_h_[sid] > static_cast<float>(view.h);
    Region R;
    R.bounds = {view.x, view.y, view.w - (bar ? bar_w + S(4) : 0), view.h};
    R.cy = view.y - static_cast<int>(sc);
    R.row_y = R.cy;
    R.max_y = R.cy;
    R.origin_y = R.cy;
    R.space = reg().space;
    scrolls_.push_back({key, view, R.origin_y, bar_w, view});
    regions_.push_back(R);
    draw([=](Gfx& gg) { gg.push_clip(view); });
}

void Ui::end_scroll() {
    if (scrolls_.empty()) return;
    ScrollCtx sc = scrolls_.back();
    Region R = reg();
    regions_.pop_back();
    scrolls_.pop_back();
    draw([](Gfx& gg) { gg.pop_clip(); });
    uint64_t sid = id(sc.key);
    float ch = static_cast<float>(R.max_y - R.origin_y + S(4));
    content_h_[sid] = ch;
    float maxs = std::max(0.0f, ch - static_cast<float>(sc.view.h));
    float& s = scroll_[sid];
    bool over = sc.view.contains(mx_, my_) && modals_.empty() == (!modal_prev_ || !modals_.empty());
    for (const auto& o : overlay_rects_prev_) if (o.contains(mx_, my_) && !in_overlay_) over = false;
    if (over && wheel_ != 0 && maxs > 0) { s -= static_cast<float>(wheel_ * S(54)); wheel_ = 0; }
    s = std::clamp(s, 0.0f, maxs);
    if (maxs <= 0) return;
    Rect track = {sc.view.right() - sc.bar_w, sc.view.y, sc.bar_w, sc.view.h};
    int thumb_h = std::max(S(30), static_cast<int>(static_cast<float>(track.h) * static_cast<float>(sc.view.h) / ch));
    int ty = track.y + static_cast<int>((static_cast<float>(track.h - thumb_h)) * (s / maxs));
    Rect thumb = {track.x + 2, ty, track.w - 4, thumb_h};
    uint64_t bid = sid ^ 0xB0B;
    bool on_thumb = thumb.contains(mx_, my_) && item_hot(thumb);
    if (on_thumb && mclick_) { active_ = bid; drag_off_ = my_ - ty; }
    if (active_ == bid) {
        if (mdown_) {
            float t = static_cast<float>(my_ - drag_off_ - track.y) / static_cast<float>(std::max(1, track.h - thumb_h));
            s = std::clamp(t, 0.0f, 1.0f) * maxs;
        }
        if (mrelease_) active_ = 0;
    }
    fill(track, alpha(th.sunken, 160), track.w / 2);
    fill(thumb, active_ == bid || on_thumb ? th.border_hi : th.border, thumb.w / 2);
}

void Ui::log_view(const std::string& key, const std::vector<std::string>& lines, int h) {
    Rect r = item_rect(avail_w(), h);
    fill(r, th.sunken, S(5));
    stroke(r, th.border, 1, S(5));
    // draw inside via a nested region occupying the same rect
    begin_region(r.inset(S(6)));
    size_t prev = content_h_.count(id(key + "#n")) ? static_cast<size_t>(content_h_[id(key + "#n")]) : 0;
    begin_scroll(key, r.w - S(12), r.h - S(12));
    int lh = g.line_height(S(12), Font::Mono);
    for (const auto& l : lines) {
        Rect lr = item_rect(avail_w(), lh);
        reg().cy = lr.y + lh;
        reg().space = 0;
        text_at(lr.x, lr.y, l, th.fg, S(12), Font::Mono);
    }
    end_scroll();
    if (lines.size() != prev) { scroll_[id(key)] = 1e9f; content_h_[id(key + "#n")] = static_cast<float>(lines.size()); request_redraw(); }
    end_region();
}

// ---------------------------------------------------------------- cards
static std::string upper_ascii(std::string s) { for (auto& c : s) if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 32); return s; }

// Flat section: an uppercase header over a hairline, no box. Untitled sections are list rows separated by a hairline.
// A non-normal tone adds a coloured bar on the left edge.
void Ui::card_begin(const std::string& title, Tone tone) {
    Region& R = reg();
    bool toned = tone != Tone::Normal;
    int padx = toned ? S(12) : 0;
    size_t slot = reserve();
    CardCtx c;
    c.slot = slot;
    c.top = R.cy;
    c.left = R.bounds.x + R.indent;
    c.right = R.bounds.right();
    c.tone = tone;
    c.title = title;
    cards_.push_back(c);
    R.bounds.x += padx;
    R.bounds.w -= 2 * padx;
    R.cy += title.empty() ? S(6) : S(4);
    R.row_y = R.cy;
    R.row_h = 0;
    R.same = false;
    if (!title.empty()) {
        label_color(upper_ascii(title), toned ? tone_color(tone) : tone_color(Tone::Muted), S(11), Font::Bold);
        R.cy += S(2);
        Rect h = item_rect(R.bounds.w, 1);
        fill(h, th.border);
        R.cy = h.bottom() + S(8);
        R.row_y = R.cy;
        R.row_h = 0;
        R.same = false;
    }
}

void Ui::card_end() {
    if (cards_.empty()) return;
    CardCtx c = cards_.back();
    cards_.pop_back();
    Region& R = reg();
    bool toned = c.tone != Tone::Normal;
    int padx = toned ? S(12) : 0;
    int bottom = std::max(R.cy - R.space, R.max_y) + S(8);
    R.bounds.x -= padx;
    R.bounds.w += 2 * padx;
    Rect box = {c.left, c.top, c.right - c.left, bottom - c.top};
    Color bar = toned ? alpha(tone_color(c.tone), 200) : Color(0, 0, 0, 0);
    Color line = th.border;
    bool row = c.title.empty();
    int bw = S(3);
    set_cmd(c.slot, [=](Gfx& gg) {
        if (bar.a) gg.fill({box.x, box.y, bw, box.h}, bar, 1);
        if (row) gg.fill({box.x, box.bottom(), box.w, 1}, alpha(line, 150));
    });
    R.cy = bottom + (row ? S(2) : S(14));
    R.row_y = R.cy;
    R.row_h = 0;
    R.same = false;
    R.last_right = box.right();
    R.max_y = std::max(R.max_y, bottom);
    last_item_ = box;
}

// ---------------------------------------------------------------- modal
bool Ui::begin_modal(const std::string& title, int w, int h) {
    modal_now_ = true;
    no_collapse_ = true;
    int ww = std::min(w, win_w_ - S(40)), hh = std::min(h, win_h_ - S(40));
    Rect box = {(win_w_ - ww) / 2, (win_h_ - hh) / 2, ww, hh};
    layers_.push_back(&over_);
    Rect full = {0, 0, win_w_, win_h_};
    fill(full, Color(0, 0, 0, 150));
    fill(box, th.panel, S(10));
    stroke(box, th.border_hi, 1, S(10));
    modals_.push_back({box});
    in_overlay_ = true;
    Rect inner = box.inset(S(18));
    begin_region(inner);
    regions_.back().space = S(8);
    heading(title, S(17));
    spacing(S(4));
    return true;
}

void Ui::end_modal() {
    if (modals_.empty()) return;
    modals_.pop_back();
    if (modals_.empty()) no_collapse_ = false;
    end_region();
    layers_.pop_back();
    in_overlay_ = !modals_.empty();
}

void Ui::begin_over(Rect r) {
    layers_.push_back(&over_);
    overlay_rects_.push_back(r);
    saved_in_overlay_ = in_overlay_;
    in_overlay_ = true;
}

void Ui::end_over() {
    if (layers_.size() > 1) layers_.pop_back();
    in_overlay_ = saved_in_overlay_;
}

void Ui::overlay(const std::function<void()>& fn) {
    layers_.push_back(&over_);
    fn();
    layers_.pop_back();
}

}  // namespace tt::ui
