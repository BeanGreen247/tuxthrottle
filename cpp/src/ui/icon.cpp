#include "icon.hpp"

#include "../sensors.hpp"
#include "appicon_data.inc"
#include "png.hpp"

#include <algorithm>
#include <cmath>

namespace tt::ui {

std::vector<uint8_t> app_icon_rgba(int n) {
    std::vector<uint8_t> px(static_cast<size_t>(n) * static_cast<size_t>(n) * 4);
    double rad = n * 0.22, cx = n / 2.0, cy = n / 2.0, half = n / 2.0;
    for (int y = 0; y < n; ++y)
        for (int x = 0; x < n; ++x) {
            double dx = std::max(std::fabs(x + 0.5 - cx) - (half - rad), 0.0), dy = std::max(std::fabs(y + 0.5 - cy) - (half - rad), 0.0);
            double d = std::sqrt(dx * dx + dy * dy) - rad;
            double a = std::clamp(0.5 - d, 0.0, 1.0);
            double u = x / double(n), v = y / double(n);
            bool tee = (v > 0.2 && v < 0.34 && u > 0.22 && u < 0.78) || (v >= 0.34 && v < 0.8 && u > 0.43 && u < 0.57);
            size_t i = (static_cast<size_t>(y) * static_cast<size_t>(n) + static_cast<size_t>(x)) * 4;
            px[i] = tee ? 255 : 0x1a; px[i + 1] = tee ? 255 : 0x9f; px[i + 2] = tee ? 255 : 0xff; px[i + 3] = static_cast<uint8_t>(a * 255);
        }
    return px;
}

bool app_icon_png(int want, int* w, int* h, std::vector<uint8_t>* rgba) {
    // the icon is compiled in (like hootedit / lestrix), so the window and tray never depend on files being installed
    if (want <= 128) {
        int n = want <= 32 ? 32 : want <= 64 ? 64 : 128;
        const unsigned char* src = n == 32 ? kAppIcon32 : n == 64 ? kAppIcon64 : kAppIcon128;
        rgba->assign(src, src + static_cast<size_t>(n) * static_cast<size_t>(n) * 4);
        *w = *h = n;
        return true;
    }
    static const int sizes[] = {16, 24, 32, 48, 64, 128, 256, 512};
    int pick = sizes[7];
    for (int s : sizes) if (s >= want) { pick = s; break; }
    std::string path = sensors::app_dir() + "/assets/icon-" + std::to_string(pick) + ".png";
    if (load_png(path, w, h, rgba)) return true;
    *w = *h = want;
    *rgba = app_icon_rgba(want);
    return false;
}

}  // namespace tt::ui
