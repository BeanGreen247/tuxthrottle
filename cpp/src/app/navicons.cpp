#include "navicons.hpp"

#include <cmath>
#include <map>

namespace tt::app {
using namespace ui;

namespace {
struct Spec { uint32_t color; int shape; };
enum Shape { Chart, Keys, Pad, Fan, Batt, Chip, Bolt, Monitor, Stack, Star, Gamepad, Gear, Line, Atom, Drive, Down, Info, Warn, Up, Diamond, Sun, Shield, Joy, Window, Box, Rings, Sliders, Cyl, Eye, Cam, Rgb, Disc };

const std::map<std::string, Spec>& table() {
    static const std::map<std::string, Spec> t = {
        {"Dashboard", {0x3b82f6, Chart}}, {"Keyboard", {0x8b5cf6, Keys}}, {"Touchpad", {0x94a3b8, Pad}}, {"Fans", {0x06b6d4, Fan}},
        {"Battery", {0x22c55e, Batt}}, {"VRAM", {0xa855f7, Chip}}, {"Power & Limits", {0xf59e0b, Bolt}}, {"Display", {0x0ea5e9, Monitor}},
        {"Profiles", {0x14b8a6, Stack}}, {"Presets", {0xeab308, Star}}, {"Setup Games", {0xef4444, Gamepad}}, {"Game Tools", {0xf97316, Gear}},
        {"MangoHud", {0xff8a1f, Line}}, {"Proton & Runtimes", {0x38bdf8, Atom}}, {"Drives", {0xa3e635, Drive}}, {"Monitoring", {0x22d3ee, Eye}},
        {"Streaming", {0xc084fc, Cam}}, {"RGB", {0xffffff, Rgb}}, {"Updates", {0x10b981, Down}}, {"About", {0x9ca3af, Info}},
        {"Report a Bug", {0xdc2626, Warn}}, {"Performance", {0xe11d48, Up}}, {"GPU", {0x76b900, Diamond}}, {"Power", {0xfacc15, Sun}},
        {"Stability", {0x3b82f6, Shield}}, {"Gaming", {0xdb2777, Joy}}, {"KDE (Desktop GUI Tweaks)", {0x1d99f3, Window}}, {"Software", {0xf59e0b, Box}},
        {"Emulation", {0xa78bfa, Rings}}, {"Input & Controllers", {0x2dd4bf, Sliders}}, {"Repos", {0x94a3b8, Cyl}},
    };
    return t;
}
}  // namespace

void draw_nav_icon(Ui& ui, const std::string& title, Rect r, bool active) {
    auto it = table().find(title);
    Spec sp = it == table().end() ? Spec{0x94a3b8, Disc} : it->second;
    Color c = hex(sp.color);
    if (!active) c = mix(c, ui.th.sunken, 0.25f);
    Color dim = alpha(c, 110);
    float x = static_cast<float>(r.x), y = static_cast<float>(r.y), w = static_cast<float>(r.w), h = static_cast<float>(r.h);
    float cx = x + w / 2, cy = y + h / 2, u = w / 20.0f;   // 20 units across
    int rad = std::max(1, static_cast<int>(u * 2));
    Color pn = ui.th.panel;
    ui.draw([=](Gfx& g) {
        auto R = [&](float a, float b, float cw, float ch) { return Rect{static_cast<int>(x + a * u), static_cast<int>(y + b * u), static_cast<int>(cw * u), static_cast<int>(ch * u)}; };
        switch (sp.shape) {
            case Chart: g.fill(R(2, 11, 4, 7), c, 1); g.fill(R(8, 5, 4, 13), c, 1); g.fill(R(14, 8, 4, 10), dim, 1); break;
            case Keys: g.stroke(R(1, 5, 18, 11), c, 2, rad); for (int i = 0; i < 3; ++i) g.fill(R(4.0f + i * 4.5f, 8, 2, 2), c, 0); g.fill(R(5, 12, 10, 2), c, 0); break;
            case Pad: g.stroke(R(2, 3, 16, 14), c, 2, rad); g.line(x + 3 * u, y + 13 * u, x + 17 * u, y + 13 * u, c, 1.5f); break;
            case Fan: g.stroke(R(1, 1, 18, 18), c, 2, 100); for (int k = 0; k < 3; ++k) g.arc(cx, cy, 4 * u, 3 * u, k * 120.0f, k * 120.0f + 80, c); g.disc(cx, cy, 1.6f * u, c); break;
            case Batt: g.stroke(R(1, 5, 15, 10), c, 2, rad); g.fill(R(16.5f, 8, 2.5f, 4), c, 0); g.fill(R(3, 7, 7, 6), c, 0); break;
            case Chip: g.stroke(R(5, 5, 10, 10), c, 2, 2); for (int i = 0; i < 3; ++i) { float p = 6.5f + i * 3.5f; g.line(x + p * u, y + 1.5f * u, x + p * u, y + 5 * u, c, 1.5f); g.line(x + p * u, y + 15 * u, x + p * u, y + 18.5f * u, c, 1.5f); } break;
            case Bolt: g.triangle({x + 11 * u, y + 1 * u}, {x + 4 * u, y + 11 * u}, {x + 10 * u, y + 11 * u}, c); g.triangle({x + 9 * u, y + 19 * u}, {x + 16 * u, y + 9 * u}, {x + 10 * u, y + 9 * u}, c); break;
            case Monitor: g.stroke(R(1, 3, 18, 11), c, 2, rad); g.line(cx, y + 14 * u, cx, y + 17 * u, c, 2); g.line(x + 6 * u, y + 17.5f * u, x + 14 * u, y + 17.5f * u, c, 2); break;
            case Stack: g.fill(R(3, 3, 14, 4), c, 2); g.fill(R(3, 8, 14, 4), dim, 2); g.fill(R(3, 13, 14, 4), c, 2); break;
            case Star: { std::vector<SDL_FPoint> p; for (int k = 0; k < 10; ++k) { float a = -1.5708f + k * 0.62832f, rr = (k % 2 ? 4.2f : 9.0f) * u; p.push_back({cx + rr * std::cos(a), cy + rr * std::sin(a)}); } for (size_t k = 0; k < p.size(); ++k) g.triangle({cx, cy}, p[k], p[(k + 1) % p.size()], c); break; }
            case Gamepad: g.fill(R(1, 6, 18, 9), c, rad); g.fill(R(5, 8, 1.5f, 5), pn, 0); g.fill(R(3.2f, 9.8f, 5.2f, 1.5f), pn, 0); g.disc(x + 13.5f * u, y + 9 * u, 1.2f * u, pn); g.disc(x + 16 * u, y + 11.5f * u, 1.2f * u, pn); break;
            case Gear: g.arc(cx, cy, 5.5f * u, 3 * u, 0, 360, c); for (int k = 0; k < 8; ++k) { float a = k * 0.7854f; g.line(cx + 6 * u * std::cos(a), cy + 6 * u * std::sin(a), cx + 9 * u * std::cos(a), cy + 9 * u * std::sin(a), c, 2.5f); } break;
            case Line: g.polyline({{x + 2 * u, y + 15 * u}, {x + 7 * u, y + 8 * u}, {x + 11 * u, y + 12 * u}, {x + 18 * u, y + 4 * u}}, c, 2.2f); g.line(x + 2 * u, y + 18 * u, x + 18 * u, y + 18 * u, dim, 1.5f); break;
            case Atom: g.disc(cx, cy, 2 * u, c); g.arc(cx, cy, 7 * u, 1.5f, 0, 360, dim); g.arc(cx, cy - 0.5f * u, 8 * u, 2, 200, 340, c); g.arc(cx, cy + 0.5f * u, 8 * u, 2, 20, 160, c); break;
            case Drive: g.stroke(R(2, 5, 16, 10), c, 2, rad); g.disc(x + 14.5f * u, y + 10 * u, 1.4f * u, c); g.line(x + 5 * u, y + 10 * u, x + 10 * u, y + 10 * u, dim, 2); break;
            case Down: g.fill(R(8.5f, 2, 3, 8), c, 0); g.triangle({x + 3 * u, y + 9 * u}, {x + 17 * u, y + 9 * u}, {cx, y + 16 * u}, c); g.fill(R(3, 17, 14, 2), dim, 0); break;
            case Info: g.disc(cx, cy, 9 * u, dim); g.disc(cx, y + 5.5f * u, 1.3f * u, c); g.fill(R(8.7f, 8.5f, 2.6f, 7), c, 1); break;
            case Warn: g.triangle({cx, y + 2 * u}, {x + 1.5f * u, y + 18 * u}, {x + 18.5f * u, y + 18 * u}, c); g.fill(R(9, 8, 2, 5), pn, 0); g.disc(cx, y + 15.2f * u, 1.1f * u, pn); break;
            case Up: g.triangle({cx, y + 3 * u}, {x + 2 * u, y + 17 * u}, {x + 18 * u, y + 17 * u}, c); break;
            case Diamond: g.triangle({cx, y + 1 * u}, {x + 1 * u, cy}, {x + 19 * u, cy}, c); g.triangle({cx, y + 19 * u}, {x + 1 * u, cy}, {x + 19 * u, cy}, dim); break;
            case Sun: g.disc(cx, cy, 4 * u, c); for (int k = 0; k < 8; ++k) { float a = k * 0.7854f; g.line(cx + 6.5f * u * std::cos(a), cy + 6.5f * u * std::sin(a), cx + 9 * u * std::cos(a), cy + 9 * u * std::sin(a), c, 2); } break;
            case Shield: g.triangle({x + 2 * u, y + 3 * u}, {x + 18 * u, y + 3 * u}, {x + 18 * u, y + 11 * u}, c); g.triangle({x + 2 * u, y + 3 * u}, {x + 2 * u, y + 11 * u}, {x + 18 * u, y + 11 * u}, c); g.triangle({x + 2 * u, y + 11 * u}, {x + 18 * u, y + 11 * u}, {cx, y + 19 * u}, dim); break;
            case Joy: g.line(cx, y + 17 * u, cx, y + 8 * u, c, 2.5f); g.disc(cx, y + 6 * u, 4 * u, c); g.fill(R(3, 16, 14, 3), dim, 2); break;
            case Window: g.stroke(R(1, 3, 18, 14), c, 2, 3); g.fill(R(1, 3, 18, 4), c, 2); break;
            case Box: g.stroke(R(2, 5, 16, 13), c, 2, 2); g.fill(R(2, 5, 16, 4), c, 1); g.fill(R(8, 5, 4, 6), pn, 0); break;
            case Rings: g.arc(cx, cy, 8 * u, 2, 0, 360, c); g.disc(cx, cy, 3.5f * u, c); break;
            case Sliders: for (int k = 0; k < 3; ++k) { float yy = y + (5 + k * 5) * u; g.line(x + 2 * u, yy, x + 18 * u, yy, dim, 2); g.disc(x + (5 + k * 5) * u, yy, 2.2f * u, c); } break;
            case Cyl: for (int k = 0; k < 3; ++k) g.fill(R(3, 3.0f + k * 5.5f, 14, 4), k == 1 ? dim : c, 3); break;
            case Eye: g.arc(cx, cy + 4 * u, 9 * u, 2, 215, 325, c); g.arc(cx, cy - 4 * u, 9 * u, 2, 35, 145, c); g.disc(cx, cy, 3 * u, c); break;
            case Cam: g.fill(R(1, 5, 12, 10), c, 3); g.triangle({x + 14 * u, y + 8 * u}, {x + 19 * u, y + 4.5f * u}, {x + 19 * u, y + 15.5f * u}, c); g.triangle({x + 14 * u, y + 12 * u}, {x + 14 * u, y + 8 * u}, {x + 19 * u, y + 15.5f * u}, c); break;
            case Rgb: g.disc(x + 7 * u, y + 7 * u, 5 * u, Color(255, 60, 60, 170)); g.disc(x + 13 * u, y + 7 * u, 5 * u, Color(60, 255, 90, 170)); g.disc(cx, y + 13 * u, 5 * u, Color(70, 110, 255, 170)); break;
            default: g.disc(cx, cy, 6 * u, c); break;
        }
    });
}

}  // namespace tt::app
