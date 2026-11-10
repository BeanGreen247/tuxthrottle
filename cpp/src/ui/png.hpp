// Minimal PNG reader (8-bit RGBA / RGB, non-interlaced) with its own inflate: no libpng, no zlib.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace tt::ui {
bool inflate_zlib(const uint8_t* data, size_t n, std::vector<uint8_t>* out);
// Decodes to RGBA bytes. Returns false for anything it does not support.
bool load_png(const std::string& path, int* w, int* h, std::vector<uint8_t>* rgba);
bool decode_png(const std::vector<uint8_t>& file, int* w, int* h, std::vector<uint8_t>* rgba);
}  // namespace tt::ui
