#include "widgets.hpp"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <cstdio>
#include <string>

namespace tt::app {

using namespace ui;

void ring_gauge(Ui& ui, const std::string& caption, const std::string& unit, double value, double maximum, Color color, int decimals, int size) {
    int S = ui.S(size);
    int off = std::max(0, (ui.avail_w() - S) / 2);
    ui.indent(off);
    int cap_h = ui.line_h(ui.S(12));
    Rect r = ui.item_rect(S, S + cap_h + ui.S(4));
    ui.unindent(off);
    float cx = static_cast<float>(r.x + S / 2), cy = static_cast<float>(r.y + S / 2);
    float ring = std::max(8.0f, static_cast<float>(S) / 11.0f);
    float rad = static_cast<float>(S) / 2 - ring / 2 - 3;
    double frac = maximum > 0 ? std::clamp(value / maximum, 0.0, 1.0) : 0;
    Color track = ui.th.sunken;
    ui.draw([=](Gfx& g) {
        g.arc(cx, cy, rad, ring, 135, 405, track);
        if (frac > 0.001) g.arc(cx, cy, rad, ring, 135, 135 + 270 * static_cast<float>(frac), color);
    });
    char b[32];
    std::snprintf(b, sizeof b, "%.*f", decimals, value);
    int vs = static_cast<int>(S * 0.19), us = static_cast<int>(S * 0.09);
    int vw = ui.g.text_width(b, vs, Font::Bold), vh = ui.g.line_height(vs, Font::Bold);
    ui.text_at(static_cast<int>(cx) - vw / 2, static_cast<int>(cy) - vh / 2 - 4, b, ui.th.fg, vs, Font::Bold);
    if (!unit.empty()) {
        int uw = ui.g.text_width(unit, us, Font::Regular);
        ui.text_at(static_cast<int>(cx) - uw / 2, static_cast<int>(cy) + static_cast<int>(S * 0.13), unit, ui.tone_color(Tone::Muted), us);
    }
    int sz = ui.S(12);
    int cw = ui.g.text_width(caption, sz, Font::Regular);
    ui.text_at(r.x + (S - cw) / 2, r.y + S + ui.S(2), caption, ui.tone_color(Tone::Muted), sz);
}

void history_chart(Ui& ui, const std::string& caption, const std::deque<double>& data, Color color, const std::vector<Threshold>& thr, int w, int h) {
    Rect r = ui.item_rect(w, h);
    ui.fill(r, ui.th.sunken, ui.S(6));
    ui.stroke(r, ui.th.border, 1, ui.S(6));
    int sz = ui.S(11);
    ui.text_at(r.x + ui.S(8), r.y + ui.S(5), caption, ui.tone_color(Tone::Muted), sz);
    if (data.empty()) return;
    char b[32];
    std::snprintf(b, sizeof b, "%.0f", data.back());
    int bw = ui.g.text_width(b, ui.S(13), Font::Bold);
    ui.text_at(r.right() - bw - ui.S(8), r.y + ui.S(4), b, ui.th.fg, ui.S(13), Font::Bold);
    double lo = *std::min_element(data.begin(), data.end()), hi = *std::max_element(data.begin(), data.end());
    for (const auto& t : thr) { lo = std::min(lo, t.value); hi = std::max(hi, t.value); }
    double pad = std::max(2.0, (hi - lo) * 0.15);
    lo = std::max(0.0, lo - pad);
    hi += pad;
    Rect plot = {r.x + ui.S(8), r.y + ui.S(22), r.w - ui.S(16), r.h - ui.S(30)};
    auto Y = [&](double v) { return static_cast<float>(plot.bottom()) - static_cast<float>((v - lo) / (hi - lo) * plot.h); };
    for (const auto& t : thr) {
        float y = Y(t.value);
        Color c = alpha(t.color, 150);
        ui.draw([=](Gfx& g) { g.line(static_cast<float>(plot.x), y, static_cast<float>(plot.right()), y, c, 1); });
        ui.text_at(plot.x + 2, static_cast<int>(y) - ui.S(12), t.label, alpha(t.color, 200), ui.S(9));
    }
    std::vector<SDL_FPoint> pts;
    size_t n = data.size();
    for (size_t i = 0; i < n; ++i) {
        float x = static_cast<float>(plot.right()) - static_cast<float>(n - 1 - i) * (static_cast<float>(plot.w) / 89.0f);
        pts.push_back({std::max(x, static_cast<float>(plot.x)), Y(data[i])});
    }
    ui.draw([=](Gfx& g) { g.polyline(pts, color, 2); });
}

Color parse_hex(const std::string& in, Color def) {
    std::string s = in;
    if (!s.empty() && s[0] == '#') s.erase(0, 1);
    if (s.size() != 6) return def;
    for (char c : s) if (!std::isxdigit(static_cast<unsigned char>(c))) return def;
    unsigned v = static_cast<unsigned>(std::stoul(s, nullptr, 16));
    return Color(static_cast<uint8_t>(v >> 16), static_cast<uint8_t>((v >> 8) & 255), static_cast<uint8_t>(v & 255));
}

bool color_edit(Ui& ui, const std::string& key, std::string* hex) {
    ui.push_id(key);
    Color c = parse_hex(*hex);
    int rgb[3] = {c.r, c.g, c.b};
    bool changed = false;
    const char* names[3] = {"R", "G", "B"};
    for (int i = 0; i < 3; ++i) {
        ui.push_id(i);
        ui.label_c(names[i], Tone::Muted, Font::Regular, ui.S(20));
        ui.same_line(ui.S(4));
        if (ui.slider("c", &rgb[i], 0, 255, ui.S(320))) changed = true;
        ui.pop_id();
    }
    ui.label_c("Hex", Tone::Muted, Font::Regular, ui.S(20));
    ui.same_line(ui.S(4));
    std::string h = *hex;
    bool typed = ui.input("hex", &h, ui.S(130), "#rrggbb");
    ui.same_line(ui.S(12));
    Rect sw = ui.item_rect(ui.S(80), ui.line_h() + ui.S(12));
    if (typed) { if (!h.empty() && h[0] != '#') h = "#" + h; *hex = h; c = parse_hex(h); }
    else if (changed) {
        char b[16];
        std::snprintf(b, sizeof b, "#%02x%02x%02x", rgb[0], rgb[1], rgb[2]);
        *hex = b;
        c = Color(static_cast<uint8_t>(rgb[0]), static_cast<uint8_t>(rgb[1]), static_cast<uint8_t>(rgb[2]));
    }
    ui.fill(sw, c, ui.S(6));
    ui.stroke(sw, ui.th.border_hi, 1, ui.S(6));
    ui.pop_id();
    return typed || changed;
}

}  // namespace tt::app
