#include "gfx.hpp"

#include <fontconfig/fontconfig.h>
#include <ft2build.h>
#include FT_FREETYPE_H

#include <algorithm>
#include <future>
#include <string>
#include <cmath>
#include <ctime>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace tt::ui {

// ---------------------------------------------------------------- colors
Color hex(uint32_t rgb, uint8_t a) { return Color(static_cast<uint8_t>((rgb >> 16) & 255), static_cast<uint8_t>((rgb >> 8) & 255), static_cast<uint8_t>(rgb & 255), a); }

Color mix(Color a, Color b, float t) {
    auto m = [&](uint8_t x, uint8_t y) { return static_cast<uint8_t>(std::lround(x + (y - x) * t)); };
    return Color(m(a.r, b.r), m(a.g, b.g), m(a.b, b.b), m(a.a, b.a));
}

Color alpha(Color c, uint8_t a) { c.a = a; return c; }

static double lin(uint8_t v) {
    double c = v / 255.0;
    return c <= 0.03928 ? c / 12.92 : std::pow((c + 0.055) / 1.055, 2.4);
}
static double luminance(Color c) { return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b); }

double contrast_ratio(Color a, Color b) {
    double la = luminance(a), lb = luminance(b);
    if (la < lb) std::swap(la, lb);
    return (la + 0.05) / (lb + 0.05);
}

Color readable_on(Color fg, Color bg, double min_ratio) {
    if (contrast_ratio(fg, bg) >= min_ratio) return fg;
    Color target = luminance(bg) < 0.5 ? Color(255, 255, 255) : Color(0, 0, 0);
    for (int i = 1; i <= 20; ++i) {
        Color c = mix(fg, target, i / 20.0f);
        if (contrast_ratio(c, bg) >= min_ratio) return c;
    }
    return target;
}

Rect Rect::intersect(const Rect& o) const {
    int x0 = std::max(x, o.x), y0 = std::max(y, o.y), x1 = std::min(right(), o.right()), y1 = std::min(bottom(), o.bottom());
    return {x0, y0, std::max(0, x1 - x0), std::max(0, y1 - y0)};
}

// ---------------------------------------------------------------- fonts
struct Gfx::FaceSet {
    FT_Library lib = nullptr;
    std::vector<FT_Face> faces[5];   // per Font: primary first, then fallbacks
    ~FaceSet() {
        for (auto& v : faces) for (FT_Face f : v) FT_Done_Face(f);
        if (lib) FT_Done_FreeType(lib);
    }
};

static std::vector<std::string> fonts_for(const char* family, bool bold) {
    std::vector<std::string> files;
    FcPattern* pat = FcPatternCreate();
    FcPatternAddString(pat, FC_FAMILY, reinterpret_cast<const FcChar8*>(family));
    FcPatternAddInteger(pat, FC_WEIGHT, bold ? FC_WEIGHT_BOLD : FC_WEIGHT_REGULAR);
    FcPatternAddInteger(pat, FC_SLANT, FC_SLANT_ROMAN);
    FcConfigSubstitute(nullptr, pat, FcMatchPattern);
    FcDefaultSubstitute(pat);
    FcResult res;
    FcFontSet* set = FcFontSort(nullptr, pat, FcTrue, nullptr, &res);
    if (set) {
        for (int i = 0; i < set->nfont && files.size() < 14; ++i) {
            FcChar8* file = nullptr;
            if (FcPatternGetString(set->fonts[i], FC_FILE, 0, &file) == FcResultMatch) {
                std::string f = reinterpret_cast<char*>(file);
                if (std::find(files.begin(), files.end(), f) == files.end()) files.push_back(f);
            }
        }
        FcFontSetDestroy(set);
    }
    FcPatternDestroy(pat);
    return files;
}

static int g_sdl_refs = 0;

Gfx::Gfx() = default;

Gfx::~Gfx() {
    for (auto& kv : images_) if (kv.second) SDL_DestroyTexture(kv.second);
    for (auto& pg : atlas_) if (pg.tex) SDL_DestroyTexture(pg.tex);
    faces_.reset();
    if (ren_) SDL_DestroyRenderer(ren_);
    if (win_) SDL_DestroyWindow(win_);
    if (inited_ && --g_sdl_refs == 0) SDL_Quit();
}

static double tnow() { timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + ts.tv_nsec / 1e9; }

bool Gfx::init(const std::string& title, int w, int h, bool borderless, bool always_on_top, bool hidden) {
    // WM_CLASS / Wayland app id must match StartupWMClass in the .desktop file, or the taskbar shows no icon (hootedit and lestrix do the same)
    setenv("mesa_glthread", "false", 0);   // a 2D UI gains nothing from Mesa's GL command thread: 4 threads and ~2 MB less
    setenv("SDL_VIDEO_X11_WMCLASS", "tuxthrottle", 0);
    setenv("SDL_VIDEO_WAYLAND_WMCLASS", "tuxthrottle", 0);
    double t0 = tnow();
    bool tm = std::getenv("TT_TIMING") != nullptr;
    auto mark = [&](const char* what) { if (tm) std::fprintf(stderr, "  gfx.%s %.3f\n", what, tnow() - t0); };
    if (const char* s = std::getenv("TT_SCALE")) scale_ = std::max(0.5, std::atof(s));
    SDL_SetHint(SDL_HINT_VIDEODRIVER, std::getenv("SDL_VIDEODRIVER") ? std::getenv("SDL_VIDEODRIVER") : "x11,wayland");
    if (g_sdl_refs == 0 && SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) != 0) {
        std::fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return false;
    }
    ++g_sdl_refs;
    inited_ = true;
    mark("sdl_init");
    // font discovery (fontconfig) runs while the window and GL context are being created
    struct FontSet { std::vector<std::string> files[5]; };
    auto fonts_future = std::async(std::launch::deferred, [] {
        FcInit();
        FontSet fs;
        fs.files[0] = fonts_for("Inter", false); fs.files[1] = fonts_for("Inter", true);
        fs.files[2] = fonts_for("DejaVu Sans Mono", false); fs.files[3] = fonts_for("DejaVu Sans Mono", true);
        fs.files[4] = fonts_for("Inter Display", true);
        return fs;
    });
    SDL_SetHint(SDL_HINT_RENDER_SCALE_QUALITY, "1");
    SDL_SetHint(SDL_HINT_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR, "0");
    const char* rmode = std::getenv("TT_RENDER");
    bool soft = rmode && std::string(rmode) == "software";
    const char* ms = std::getenv("TT_MSAA");
    if (!soft && !(ms && std::string(ms) == "0")) { SDL_GL_SetAttribute(SDL_GL_MULTISAMPLEBUFFERS, 1); SDL_GL_SetAttribute(SDL_GL_MULTISAMPLESAMPLES, 4); }
    Uint32 flags = SDL_WINDOW_RESIZABLE | SDL_WINDOW_ALLOW_HIGHDPI | (soft ? 0 : SDL_WINDOW_OPENGL);
    if (borderless) flags |= SDL_WINDOW_BORDERLESS;
    if (always_on_top) flags |= SDL_WINDOW_ALWAYS_ON_TOP | SDL_WINDOW_SKIP_TASKBAR;
    if (hidden) flags |= SDL_WINDOW_HIDDEN;
    mark("pre_window");
    SDL_SetHint(SDL_HINT_RENDER_DRIVER, soft ? "software" : "opengl");
    win_ = SDL_CreateWindow(title.c_str(), SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h, flags);
    if (!win_) {   // no GL/MSAA: plain window
        SDL_SetHint(SDL_HINT_RENDER_DRIVER, "");
        win_ = SDL_CreateWindow(title.c_str(), SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h, flags & ~static_cast<Uint32>(SDL_WINDOW_OPENGL));
    }
    if (!win_) { std::fprintf(stderr, "SDL_CreateWindow failed: %s\n", SDL_GetError()); return false; }
    mark("window");
    ren_ = SDL_CreateRenderer(win_, -1, soft ? SDL_RENDERER_SOFTWARE : (SDL_RENDERER_ACCELERATED | (std::getenv("TT_NOVSYNC") ? 0 : SDL_RENDERER_PRESENTVSYNC)));
    if (!ren_) ren_ = SDL_CreateRenderer(win_, -1, SDL_RENDERER_SOFTWARE);
    if (!ren_) { std::fprintf(stderr, "SDL_CreateRenderer failed: %s\n", SDL_GetError()); return false; }
    SDL_SetRenderDrawBlendMode(ren_, SDL_BLENDMODE_BLEND);
    mark("renderer");

    faces_ = std::make_unique<FaceSet>();
    if (FT_Init_FreeType(&faces_->lib) != 0) { std::fprintf(stderr, "FreeType init failed\n"); return false; }
    FontSet fonts = fonts_future.get();
    mark("fontconfig");
    for (int i = 0; i < 5; ++i)
        for (const auto& file : fonts.files[i]) {
            FT_Face face;
            if (FT_New_Face(faces_->lib, file.c_str(), 0, &face) == 0) faces_->faces[i].push_back(face);
        }
    mark("faces");
    if (faces_->faces[4].empty())   // no "Inter Display" installed: use the bold body face
        for (const auto& file : fonts_for("Inter", true)) { FT_Face face; if (FT_New_Face(faces_->lib, file.c_str(), 0, &face) == 0) faces_->faces[4].push_back(face); }
    if (faces_->faces[0].empty()) { std::fprintf(stderr, "no usable fonts found (install Inter or DejaVu Sans)\n"); return false; }
    return true;
}

void Gfx::size(int* w, int* h) const { SDL_GetRendererOutputSize(ren_, w, h); }

void Gfx::begin(Color clear) {
    SDL_SetRenderDrawColor(ren_, clear.r, clear.g, clear.b, 255);
    SDL_RenderSetClipRect(ren_, nullptr);
    SDL_RenderClear(ren_);
    clips_.clear();
}

void Gfx::present() { SDL_RenderPresent(ren_); }

// ---------------------------------------------------------------- glyphs
static uint32_t decode_utf8(const std::string& s, size_t& i) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    if (c < 0x80) { ++i; return c; }
    int n = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : c >= 0xC0 ? 1 : 0;
    uint32_t cp = n == 3 ? (c & 0x07) : n == 2 ? (c & 0x0F) : n == 1 ? (c & 0x1F) : c;
    ++i;
    for (int k = 0; k < n && i < s.size(); ++k, ++i) cp = (cp << 6) | (static_cast<unsigned char>(s[i]) & 0x3F);
    return cp;
}

bool Gfx::atlas_put(const uint32_t* px, int w, int h, Glyph* g) {
    constexpr int kPage = 1024;
    if (w + 2 > kPage || h + 2 > kPage) return false;
    for (int attempt = 0; attempt < 2; ++attempt) {
        if (atlas_.empty() || attempt == 1) {
            AtlasPage pg;
            pg.tex = SDL_CreateTexture(ren_, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STATIC, kPage, kPage);
            if (!pg.tex) return false;
            SDL_SetTextureBlendMode(pg.tex, SDL_BLENDMODE_BLEND);
            std::vector<uint32_t> blank(static_cast<size_t>(kPage) * kPage, 0x00FFFFFFu);
            SDL_UpdateTexture(pg.tex, nullptr, blank.data(), kPage * 4);
            atlas_.push_back(pg);
        }
        AtlasPage& p = atlas_.back();
        if (p.x + w + 1 > kPage) { p.x = 0; p.y += p.row_h + 1; p.row_h = 0; }
        if (p.y + h + 1 > kPage) continue;
        SDL_Rect r = {p.x, p.y, w, h};
        SDL_UpdateTexture(p.tex, &r, px, w * 4);
        g->tex = p.tex; g->sx = p.x; g->sy = p.y;
        p.x += w + 1;
        p.row_h = std::max(p.row_h, h);
        return true;
    }
    return false;
}

Gfx::Glyph& Gfx::glyph(Font f, int size, uint32_t cp) {
    uint64_t key = (static_cast<uint64_t>(f) << 56) | (static_cast<uint64_t>(size) << 32) | cp;
    auto it = glyphs_.find(key);
    if (it != glyphs_.end()) return it->second;
    Glyph g;
    auto& faces = faces_->faces[static_cast<int>(f)];
    FT_Face use = nullptr;
    FT_UInt idx = 0;
    for (FT_Face face : faces) {
        idx = FT_Get_Char_Index(face, cp);
        if (idx) { use = face; break; }
    }
    if (!use && !faces.empty()) { use = faces[0]; idx = 0; }
    if (use) {
        FT_Set_Pixel_Sizes(use, 0, static_cast<FT_UInt>(size));
        if (FT_Load_Glyph(use, idx, FT_LOAD_DEFAULT | FT_LOAD_TARGET_LIGHT) == 0 && FT_Render_Glyph(use->glyph, FT_RENDER_MODE_NORMAL) == 0) {
            FT_GlyphSlot sl = use->glyph;
            g.adv = static_cast<int>((sl->advance.x + 32) >> 6);
            g.w = static_cast<int>(sl->bitmap.width);
            g.h = static_cast<int>(sl->bitmap.rows);
            g.bx = sl->bitmap_left;
            g.by = sl->bitmap_top;
            if (g.w > 0 && g.h > 0) {
                std::vector<uint32_t> px(static_cast<size_t>(g.w) * static_cast<size_t>(g.h));
                for (int y = 0; y < g.h; ++y)
                    for (int x = 0; x < g.w; ++x) {
                        uint8_t a = sl->bitmap.buffer[y * sl->bitmap.pitch + x];
                        if (sl->bitmap.pixel_mode == FT_PIXEL_MODE_MONO) a = (sl->bitmap.buffer[y * sl->bitmap.pitch + x / 8] & (0x80 >> (x % 8))) ? 255 : 0;
                        px[static_cast<size_t>(y) * static_cast<size_t>(g.w) + static_cast<size_t>(x)] = (static_cast<uint32_t>(a) << 24) | 0x00FFFFFF;
                    }
                atlas_put(px.data(), g.w, g.h, &g);
            }
        }
    }
    return glyphs_.emplace(key, g).first->second;
}

Gfx::Metrics& Gfx::metrics(int size, Font f) {
    uint64_t key = (static_cast<uint64_t>(f) << 32) | static_cast<uint32_t>(size);
    auto it = metrics_.find(key);
    if (it != metrics_.end()) return it->second;
    Metrics m{size + 4, size};
    auto& faces = faces_->faces[static_cast<int>(f)];
    if (!faces.empty()) {
        FT_Set_Pixel_Sizes(faces[0], 0, static_cast<FT_UInt>(size));
        m.height = static_cast<int>(faces[0]->size->metrics.height >> 6) + 1;
        m.ascent = static_cast<int>(faces[0]->size->metrics.ascender >> 6);
    }
    return metrics_.emplace(key, m).first->second;
}

int Gfx::line_height(int size, Font f) {
    return metrics(size, f).height;
}

int Gfx::line_height_uncached(int size, Font f) {
    auto& faces = faces_->faces[static_cast<int>(f)];
    if (faces.empty()) return size + 4;
    FT_Set_Pixel_Sizes(faces[0], 0, static_cast<FT_UInt>(size));
    return static_cast<int>(faces[0]->size->metrics.height >> 6) + 1;
}

int Gfx::ascent(int size, Font f) {
    return metrics(size, f).ascent;
}

int Gfx::ascent_uncached(int size, Font f) {
    auto& faces = faces_->faces[static_cast<int>(f)];
    if (faces.empty()) return size;
    FT_Set_Pixel_Sizes(faces[0], 0, static_cast<FT_UInt>(size));
    return static_cast<int>(faces[0]->size->metrics.ascender >> 6);
}

int Gfx::text_width(const std::string& s, int size, Font f) {
    int w = 0;
    for (size_t i = 0; i < s.size();) w += glyph(f, size, decode_utf8(s, i)).adv;
    return w;
}

int Gfx::text(int x, int y, const std::string& s, int size, Color c, Font f) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[0] += tnow() * 1000 - t; ++g->prof_n[0]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    int base = y + ascent(size, f);
    int pen = x;
    // one SDL_RenderGeometry call per atlas page instead of one SDL_RenderCopy per glyph
    SDL_Color col = {c.r, c.g, c.b, c.a};
    verts_.clear();
    idx_.clear();
    SDL_Texture* page = nullptr;
    auto flush = [&] {
        if (!verts_.empty() && page) SDL_RenderGeometry(ren_, page, verts_.data(), static_cast<int>(verts_.size()), idx_.data(), static_cast<int>(idx_.size()));
        verts_.clear();
        idx_.clear();
    };
    for (size_t i = 0; i < s.size();) {
        Glyph& g = glyph(f, size, decode_utf8(s, i));
        if (g.tex) {
            if (g.tex != page) { flush(); page = g.tex; }
            float x0 = static_cast<float>(pen + g.bx), y0 = static_cast<float>(base - g.by), x1 = x0 + static_cast<float>(g.w), y1 = y0 + static_cast<float>(g.h);
            float u0 = static_cast<float>(g.sx) / 1024.0f, v0 = static_cast<float>(g.sy) / 1024.0f, u1 = static_cast<float>(g.sx + g.w) / 1024.0f, v1 = static_cast<float>(g.sy + g.h) / 1024.0f;
            int base_i = static_cast<int>(verts_.size());
            verts_.push_back({{x0, y0}, col, {u0, v0}});
            verts_.push_back({{x1, y0}, col, {u1, v0}});
            verts_.push_back({{x1, y1}, col, {u1, v1}});
            verts_.push_back({{x0, y1}, col, {u0, v1}});
            for (int k : {0, 1, 2, 0, 2, 3}) idx_.push_back(base_i + k);
        }
        pen += g.adv;
    }
    flush();
    return pen - x;
}

int Gfx::fit_chars(const std::string& s, int max_w, int size, Font f) {
    int w = 0;
    size_t i = 0, last = 0;
    while (i < s.size()) {
        size_t start = i;
        int adv = glyph(f, size, decode_utf8(s, i)).adv;
        if (w + adv > max_w) return static_cast<int>(start);
        w += adv;
        last = i;
    }
    return static_cast<int>(last);
}

std::string Gfx::ellipsize(const std::string& s, int max_w, int size, Font f) {
    if (text_width(s, size, f) <= max_w) return s;
    int dots = text_width("...", size, f);
    int n = fit_chars(s, max_w - dots, size, f);
    return s.substr(0, static_cast<size_t>(n)) + "...";
}

// ---------------------------------------------------------------- shapes
static SDL_Vertex vert(float x, float y, Color c) { SDL_Vertex v; v.position = {x, y}; v.color = {c.r, c.g, c.b, c.a}; v.tex_coord = {0, 0}; return v; }

static void rounded_outline(std::vector<SDL_FPoint>& pts, Rect r, int radius) {
    int rad = std::min({radius, r.w / 2, r.h / 2});
    if (rad <= 0) {
        pts = {{static_cast<float>(r.x), static_cast<float>(r.y)}, {static_cast<float>(r.right()), static_cast<float>(r.y)},
               {static_cast<float>(r.right()), static_cast<float>(r.bottom())}, {static_cast<float>(r.x), static_cast<float>(r.bottom())}};
        return;
    }
    const int seg = std::max(3, rad);
    auto corner = [&](float cx, float cy, float a0) {
        for (int i = 0; i <= seg; ++i) {
            float a = (a0 + 90.0f * static_cast<float>(i) / static_cast<float>(seg)) * 3.14159265f / 180.0f;
            pts.push_back({cx + static_cast<float>(rad) * std::cos(a), cy + static_cast<float>(rad) * std::sin(a)});
        }
    };
    corner(static_cast<float>(r.right() - rad), static_cast<float>(r.y + rad), 270);
    corner(static_cast<float>(r.right() - rad), static_cast<float>(r.bottom() - rad), 0);
    corner(static_cast<float>(r.x + rad), static_cast<float>(r.bottom() - rad), 90);
    corner(static_cast<float>(r.x + rad), static_cast<float>(r.y + rad), 180);
}

void Gfx::fill(Rect r, Color c, int radius) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[1] += tnow() * 1000 - t; ++g->prof_n[1]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    if (r.w <= 0 || r.h <= 0 || c.a == 0) return;
    if (radius <= 0) {
        SDL_SetRenderDrawColor(ren_, c.r, c.g, c.b, c.a);
        SDL_Rect sr = {r.x, r.y, r.w, r.h};
        SDL_RenderFillRect(ren_, &sr);
        return;
    }
    std::vector<SDL_FPoint> pts;
    rounded_outline(pts, r, radius);
    std::vector<SDL_Vertex> v;
    v.push_back(vert(static_cast<float>(r.x) + static_cast<float>(r.w) / 2, static_cast<float>(r.y) + static_cast<float>(r.h) / 2, c));
    for (auto& p : pts) v.push_back(vert(p.x, p.y, c));
    std::vector<int> idx;
    int n = static_cast<int>(pts.size());
    for (int i = 0; i < n; ++i) { idx.push_back(0); idx.push_back(1 + i); idx.push_back(1 + (i + 1) % n); }
    SDL_RenderGeometry(ren_, nullptr, v.data(), static_cast<int>(v.size()), idx.data(), static_cast<int>(idx.size()));
}

void Gfx::stroke(Rect r, Color c, int t, int radius) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[2] += tnow() * 1000 - t; ++g->prof_n[2]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    if (r.w <= 0 || r.h <= 0 || c.a == 0) return;
    if (radius <= 0) {
        SDL_SetRenderDrawColor(ren_, c.r, c.g, c.b, c.a);
        for (int i = 0; i < t; ++i) {
            SDL_Rect sr = {r.x + i, r.y + i, r.w - 2 * i, r.h - 2 * i};
            SDL_RenderDrawRect(ren_, &sr);
        }
        return;
    }
    std::vector<SDL_FPoint> pts;
    rounded_outline(pts, r.inset(0), radius);
    pts.push_back(pts.front());
    // inset by half the thickness so the stroke stays inside r
    Rect in = r.inset(0);
    (void)in;
    polyline(pts, c, static_cast<float>(t));
}

void Gfx::line(float x0, float y0, float x1, float y1, Color c, float t) { polyline({{x0, y0}, {x1, y1}}, c, t); }

void Gfx::polyline(const std::vector<SDL_FPoint>& pts, Color c, float t) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[3] += tnow() * 1000 - t; ++g->prof_n[3]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    if (pts.size() < 2 || c.a == 0) return;
    std::vector<SDL_Vertex> v;
    std::vector<int> idx;
    float h = t / 2;
    for (size_t i = 0; i + 1 < pts.size(); ++i) {
        float dx = pts[i + 1].x - pts[i].x, dy = pts[i + 1].y - pts[i].y, len = std::sqrt(dx * dx + dy * dy);
        if (len < 1e-4f) continue;
        float nx = -dy / len * h, ny = dx / len * h;
        int base = static_cast<int>(v.size());
        v.push_back(vert(pts[i].x + nx, pts[i].y + ny, c));
        v.push_back(vert(pts[i].x - nx, pts[i].y - ny, c));
        v.push_back(vert(pts[i + 1].x - nx, pts[i + 1].y - ny, c));
        v.push_back(vert(pts[i + 1].x + nx, pts[i + 1].y + ny, c));
        for (int k : {0, 1, 2, 0, 2, 3}) idx.push_back(base + k);
    }
    if (!v.empty()) SDL_RenderGeometry(ren_, nullptr, v.data(), static_cast<int>(v.size()), idx.data(), static_cast<int>(idx.size()));
}

void Gfx::arc(float cx, float cy, float radius, float thickness, float a0, float a1, Color c) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[4] += tnow() * 1000 - t; ++g->prof_n[4]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    if (c.a == 0 || a1 == a0) return;
    float r_out = radius + thickness / 2, r_in = radius - thickness / 2;
    int steps = std::max(4, static_cast<int>(std::fabs(a1 - a0) / 3.0f));
    std::vector<SDL_Vertex> v;
    std::vector<int> idx;
    for (int i = 0; i <= steps; ++i) {
        float a = (a0 + (a1 - a0) * static_cast<float>(i) / static_cast<float>(steps)) * 3.14159265f / 180.0f;
        v.push_back(vert(cx + r_out * std::cos(a), cy + r_out * std::sin(a), c));
        v.push_back(vert(cx + r_in * std::cos(a), cy + r_in * std::sin(a), c));
        if (i > 0) {
            int b = (i - 1) * 2;
            for (int k : {0, 1, 3, 0, 3, 2}) idx.push_back(b + k);
        }
    }
    SDL_RenderGeometry(ren_, nullptr, v.data(), static_cast<int>(v.size()), idx.data(), static_cast<int>(idx.size()));
}

void Gfx::disc(float cx, float cy, float radius, Color c) {
    std::vector<SDL_Vertex> v{vert(cx, cy, c)};
    std::vector<int> idx;
    int steps = std::max(10, static_cast<int>(radius * 2));
    for (int i = 0; i <= steps; ++i) {
        float a = 2 * 3.14159265f * static_cast<float>(i) / static_cast<float>(steps);
        v.push_back(vert(cx + radius * std::cos(a), cy + radius * std::sin(a), c));
        if (i > 0) { idx.push_back(0); idx.push_back(i); idx.push_back(i + 1); }
    }
    SDL_RenderGeometry(ren_, nullptr, v.data(), static_cast<int>(v.size()), idx.data(), static_cast<int>(idx.size()));
}

void Gfx::triangle(SDL_FPoint a, SDL_FPoint b, SDL_FPoint c, Color col) {
    SDL_Vertex v[3] = {vert(a.x, a.y, col), vert(b.x, b.y, col), vert(c.x, c.y, col)};
    SDL_RenderGeometry(ren_, nullptr, v, 3, nullptr, 0);
}

void Gfx::gradient_v(Rect r, Color top, Color bottom) {
    SDL_Vertex v[4] = {vert(static_cast<float>(r.x), static_cast<float>(r.y), top), vert(static_cast<float>(r.right()), static_cast<float>(r.y), top),
                       vert(static_cast<float>(r.right()), static_cast<float>(r.bottom()), bottom), vert(static_cast<float>(r.x), static_cast<float>(r.bottom()), bottom)};
    int idx[6] = {0, 1, 2, 0, 2, 3};
    SDL_RenderGeometry(ren_, nullptr, v, 4, idx, 6);
}

void Gfx::apply_clip() {
    if (clips_.empty()) { SDL_RenderSetClipRect(ren_, nullptr); return; }
    Rect c = clips_.back();
    SDL_Rect sr = {c.x, c.y, c.w, c.h};
    SDL_RenderSetClipRect(ren_, &sr);
}

void Gfx::push_clip(Rect r) {
    struct P { Gfx* g; double t; ~P() { if (g->prof_on) { g->prof_ms[5] += tnow() * 1000 - t; ++g->prof_n[5]; } } } prof_guard{this, prof_on ? tnow() * 1000 : 0};
    if (!clips_.empty()) r = r.intersect(clips_.back());
    clips_.push_back(r);
    apply_clip();
}

void Gfx::pop_clip() {
    if (!clips_.empty()) clips_.pop_back();
    apply_clip();
}

void Gfx::image(const std::string& key, const uint8_t* rgba, int w, int h, Rect r) {
    SDL_Texture*& t = images_[key];
    if (!t) {
        t = SDL_CreateTexture(ren_, SDL_PIXELFORMAT_RGBA32, SDL_TEXTUREACCESS_STATIC, w, h);
        if (!t) return;
        SDL_UpdateTexture(t, nullptr, rgba, w * 4);
        SDL_SetTextureBlendMode(t, SDL_BLENDMODE_BLEND);
        SDL_SetTextureScaleMode(t, SDL_ScaleModeLinear);
    }
    SDL_Rect dst = {r.x, r.y, r.w, r.h};
    SDL_RenderCopy(ren_, t, nullptr, &dst);
}

void Gfx::set_icon_rgba(const uint8_t* px, int w, int h) {
    SDL_Surface* s = SDL_CreateRGBSurfaceWithFormatFrom(const_cast<uint8_t*>(px), w, h, 32, w * 4, SDL_PIXELFORMAT_RGBA32);
    if (s) { SDL_SetWindowIcon(win_, s); SDL_FreeSurface(s); }
}

void Gfx::screenshot(const std::string& path) {
    int w, h;
    SDL_GetRendererOutputSize(ren_, &w, &h);
    std::vector<uint8_t> px(static_cast<size_t>(w) * static_cast<size_t>(h) * 4);
    if (SDL_RenderReadPixels(ren_, nullptr, SDL_PIXELFORMAT_RGBA32, px.data(), w * 4) != 0) return;
    std::FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return;
    std::fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (size_t i = 0; i < px.size(); i += 4) std::fwrite(&px[i], 1, 3, f);
    std::fclose(f);
}

}  // namespace tt::ui
