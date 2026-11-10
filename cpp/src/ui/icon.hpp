// Procedural TuxThrottle icon (rounded accent square with a T) - used for the window and tray icons.
#pragma once
#include <cstdint>
#include <vector>

namespace tt::ui {
// n x n pixels, RGBA byte order.
std::vector<uint8_t> app_icon_rgba(int n);
// The shipped icon (assets/icon-N.png, nearest size >= want, else the largest). Falls back to the procedural icon above.
// Returns RGBA bytes and the actual size.
bool app_icon_png(int want, int* w, int* h, std::vector<uint8_t>* rgba);
}  // namespace tt::ui
