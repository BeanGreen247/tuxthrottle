// SDL2 + FreeType 2D renderer: window, glyph cache, rounded rects, arcs, clipping.
#pragma once
#include <SDL.h>

#include <cstdint>
#include <map>
#include <unordered_map>
#include <memory>
#include <string>
#include <vector>

namespace tt::ui {

struct Color {
    uint8_t r = 0, g = 0, b = 0, a = 255;
    constexpr Color() = default;
    constexpr Color(uint8_t R, uint8_t G, uint8_t B, uint8_t A = 255) : r(R), g(G), b(B), a(A) {}
    bool operator==(const Color& o) const { return r == o.r && g == o.g && b == o.b && a == o.a; }
};
Color hex(uint32_t rgb, uint8_t a = 255);
Color mix(Color a, Color b, float t);          // t=0 -> a, t=1 -> b
Color alpha(Color c, uint8_t a);
double contrast_ratio(Color a, Color b);
Color readable_on(Color fg, Color bg, double min_ratio);   // nudges fg toward white/black until the ratio is met

struct Rect {
    int x = 0, y = 0, w = 0, h = 0;
    bool contains(int px, int py) const { return px >= x && py >= y && px < x + w && py < y + h; }
    Rect inset(int d) const { return {x + d, y + d, w - 2 * d, h - 2 * d}; }
    int right() const { return x + w; }
    int bottom() const { return y + h; }
    Rect intersect(const Rect& o) const;
};

enum class Font { Regular, Bold, Mono, MonoBold, Display };

class Gfx {
public:
    Gfx();
    ~Gfx();
    bool init(const std::string& title, int w, int h, bool borderless = false, bool always_on_top = false, bool hidden = false);
    SDL_Window* window() const { return win_; }
    uint32_t window_id() const { return win_ ? SDL_GetWindowID(win_) : 0; }
    SDL_Renderer* renderer() const { return ren_; }
    double scale() const { return scale_; }                 // UI scale (TT_SCALE or 1.0)
    int px(double v) const { return static_cast<int>(v * scale_ + 0.5); }
    void size(int* w, int* h) const;                         // drawable size in pixels

    void begin(Color clear);
    void present();

    // text (single line); y is the top of the line box
    int text_width(const std::string& s, int size, Font f = Font::Regular);
    int line_height(int size, Font f = Font::Regular);
    int ascent(int size, Font f = Font::Regular);
    int text(int x, int y, const std::string& s, int size, Color c, Font f = Font::Regular);   // returns advance
    int fit_chars(const std::string& s, int max_w, int size, Font f);   // byte length of the longest prefix that fits
    std::string ellipsize(const std::string& s, int max_w, int size, Font f);

    void fill(Rect r, Color c, int radius = 0);
    void stroke(Rect r, Color c, int thickness = 1, int radius = 0);
    void line(float x0, float y0, float x1, float y1, Color c, float thickness = 1);
    void polyline(const std::vector<SDL_FPoint>& pts, Color c, float thickness = 1);
    void arc(float cx, float cy, float radius, float thickness, float a0_deg, float a1_deg, Color c);   // degrees, 0 = east, clockwise (screen)
    void disc(float cx, float cy, float radius, Color c);
    void triangle(SDL_FPoint a, SDL_FPoint b, SDL_FPoint c, Color col);
    void gradient_v(Rect r, Color top, Color bottom);

    void push_clip(Rect r);
    void pop_clip();
    double prof_ms[6] = {0}; int prof_n[6] = {0};   // text, fill, stroke, line, arc/disc, clip (bench only)
    bool prof_on = false;
    Rect clip() const { return clips_.empty() ? Rect{0, 0, 1 << 20, 1 << 20} : clips_.back(); }

    // RGBA image drawn scaled into r (texture cached per key)
    void image(const std::string& key, const uint8_t* rgba, int w, int h, Rect r);
    void set_icon_rgba(const uint8_t* px, int w, int h);
    void screenshot(const std::string& ppm_path);

private:
    struct Glyph { SDL_Texture* tex = nullptr; int sx = 0, sy = 0, w = 0, h = 0, bx = 0, by = 0, adv = 0; };
    struct AtlasPage { SDL_Texture* tex = nullptr; int x = 0, y = 0, row_h = 0; };
    std::vector<AtlasPage> atlas_;   // glyph atlas: every glyph lives in one texture so SDL can batch a whole line of text into one draw call
    struct Metrics { int height, ascent; };
    std::unordered_map<uint64_t, Metrics> metrics_;
    Metrics& metrics(int size, Font f);
    int line_height_uncached(int size, Font f);
    int ascent_uncached(int size, Font f);
    std::vector<SDL_Vertex> verts_;
    std::vector<int> idx_;
    bool atlas_put(const uint32_t* px, int w, int h, Glyph* g);
    struct FaceSet;
    Glyph& glyph(Font f, int size, uint32_t cp);
    void apply_clip();
    bool inited_ = false;
    SDL_Window* win_ = nullptr;
    SDL_Renderer* ren_ = nullptr;
    double scale_ = 1.0;
    std::vector<Rect> clips_;
    std::unordered_map<uint64_t, Glyph> glyphs_;
    std::map<std::string, SDL_Texture*> images_;
    std::unique_ptr<FaceSet> faces_;
};

}  // namespace tt::ui
