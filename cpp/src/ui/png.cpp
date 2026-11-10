#include "png.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace tt::ui {

namespace {
struct Bits {
    const uint8_t* d;
    size_t n, pos = 0;
    uint32_t buf = 0;
    int cnt = 0;
    bool err = false;
    uint32_t get(int k) {
        while (cnt < k) {
            if (pos >= n) { err = true; return 0; }
            buf |= static_cast<uint32_t>(d[pos++]) << cnt;
            cnt += 8;
        }
        uint32_t v = buf & ((1u << k) - 1);
        buf >>= k;
        cnt -= k;
        return v;
    }
};

struct Huff {
    uint16_t count[16] = {0};
    uint16_t symbol[288] = {0};
    void build(const uint8_t* lens, int n) {
        std::memset(count, 0, sizeof count);
        for (int i = 0; i < n; ++i) ++count[lens[i]];
        count[0] = 0;
        uint16_t offs[16];
        offs[1] = 0;
        for (int i = 1; i < 15; ++i) offs[i + 1] = static_cast<uint16_t>(offs[i] + count[i]);
        for (int i = 0; i < n; ++i) if (lens[i]) symbol[offs[lens[i]]++] = static_cast<uint16_t>(i);
    }
    int decode(Bits& b) const {   // canonical Huffman, bit by bit (small inputs only)
        int code = 0, first = 0, index = 0;
        for (int len = 1; len <= 15; ++len) {
            code |= static_cast<int>(b.get(1));
            if (b.err) return -1;
            int c = count[len];
            if (code - c < first) return symbol[index + (code - first)];
            index += c;
            first += c;
            first <<= 1;
            code <<= 1;
        }
        return -1;
    }
};

const uint16_t kLenBase[] = {3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258};
const uint16_t kLenExtra[] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
const uint16_t kDistBase[] = {1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577};
const uint16_t kDistExtra[] = {0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13};

bool inflate_block(Bits& b, const Huff& lit, const Huff& dist, std::vector<uint8_t>* out) {
    while (true) {
        int sym = lit.decode(b);
        if (sym < 0) return false;
        if (sym < 256) out->push_back(static_cast<uint8_t>(sym));
        else if (sym == 256) return true;
        else {
            sym -= 257;
            if (sym >= 29) return false;
            int len = kLenBase[sym] + static_cast<int>(b.get(kLenExtra[sym]));
            int ds = dist.decode(b);
            if (ds < 0 || ds >= 30) return false;
            size_t d = kDistBase[ds] + b.get(kDistExtra[ds]);
            if (b.err || d > out->size()) return false;
            size_t from = out->size() - d;
            for (int i = 0; i < len; ++i) out->push_back((*out)[from + static_cast<size_t>(i)]);
        }
    }
}
}  // namespace

bool inflate_zlib(const uint8_t* data, size_t n, std::vector<uint8_t>* out) {
    if (n < 6 || (data[0] & 0x0F) != 8) return false;
    Bits b{data + 2, n - 2};
    out->clear();
    bool last = false;
    while (!last) {
        last = b.get(1) != 0;
        uint32_t type = b.get(2);
        if (b.err) return false;
        if (type == 0) {
            b.buf = 0; b.cnt = 0;
            if (b.pos + 4 > b.n) return false;
            uint16_t len = static_cast<uint16_t>(b.d[b.pos] | (b.d[b.pos + 1] << 8));
            b.pos += 4;
            if (b.pos + len > b.n) return false;
            out->insert(out->end(), b.d + b.pos, b.d + b.pos + len);
            b.pos += len;
        } else if (type == 1) {
            uint8_t l[288];
            for (int i = 0; i < 144; ++i) l[i] = 8;
            for (int i = 144; i < 256; ++i) l[i] = 9;
            for (int i = 256; i < 280; ++i) l[i] = 7;
            for (int i = 280; i < 288; ++i) l[i] = 8;
            Huff lit, dist;
            lit.build(l, 288);
            uint8_t dl[30];
            for (auto& x : dl) x = 5;
            dist.build(dl, 30);
            if (!inflate_block(b, lit, dist, out)) return false;
        } else if (type == 2) {
            int nlen = static_cast<int>(b.get(5)) + 257, ndist = static_cast<int>(b.get(5)) + 1, ncode = static_cast<int>(b.get(4)) + 4;
            if (b.err || nlen > 286 || ndist > 30) return false;
            static const uint8_t order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
            uint8_t lens[320] = {0};
            for (int i = 0; i < ncode; ++i) lens[order[i]] = static_cast<uint8_t>(b.get(3));
            Huff cl;
            cl.build(lens, 19);
            uint8_t ll[320] = {0};
            int idx = 0;
            while (idx < nlen + ndist) {
                int sym = cl.decode(b);
                if (sym < 0) return false;
                if (sym < 16) ll[idx++] = static_cast<uint8_t>(sym);
                else {
                    int rep, val = 0;
                    if (sym == 16) { if (idx == 0) return false; val = ll[idx - 1]; rep = 3 + static_cast<int>(b.get(2)); }
                    else if (sym == 17) rep = 3 + static_cast<int>(b.get(3));
                    else rep = 11 + static_cast<int>(b.get(7));
                    if (idx + rep > nlen + ndist) return false;
                    while (rep--) ll[idx++] = static_cast<uint8_t>(val);
                }
            }
            Huff lit, dist;
            lit.build(ll, nlen);
            dist.build(ll + nlen, ndist);
            if (!inflate_block(b, lit, dist, out)) return false;
        } else return false;
        if (b.err) return false;
    }
    return true;
}

static uint32_t be32(const uint8_t* p) { return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) | (static_cast<uint32_t>(p[2]) << 8) | p[3]; }

bool decode_png(const std::vector<uint8_t>& f, int* w, int* h, std::vector<uint8_t>* rgba) {
    static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
    if (f.size() < 33 || std::memcmp(f.data(), sig, 8) != 0) return false;
    uint32_t W = 0, H = 0;
    int ctype = -1;
    std::vector<uint8_t> idat;
    size_t pos = 8;
    while (pos + 12 <= f.size()) {
        uint32_t len = be32(&f[pos]);
        if (pos + 12 + len > f.size()) return false;
        const uint8_t* body = &f[pos + 8];
        if (!std::memcmp(&f[pos + 4], "IHDR", 4) && len >= 13) {
            W = be32(body); H = be32(body + 4);
            if (body[8] != 8 || body[12] != 0) return false;   // 8-bit, non-interlaced only
            ctype = body[9];
        } else if (!std::memcmp(&f[pos + 4], "IDAT", 4)) idat.insert(idat.end(), body, body + len);
        else if (!std::memcmp(&f[pos + 4], "IEND", 4)) break;
        pos += 12 + len;
    }
    if (ctype != 6 && ctype != 2) return false;
    if (W == 0 || H == 0 || W > 4096 || H > 4096) return false;
    int bpp = ctype == 6 ? 4 : 3;
    std::vector<uint8_t> raw;
    if (!inflate_zlib(idat.data(), idat.size(), &raw)) return false;
    size_t stride = static_cast<size_t>(W) * static_cast<size_t>(bpp);
    if (raw.size() < (stride + 1) * H) return false;
    std::vector<uint8_t> px(stride * H);
    for (uint32_t y = 0; y < H; ++y) {
        uint8_t ft = raw[y * (stride + 1)];
        const uint8_t* src = &raw[y * (stride + 1) + 1];
        uint8_t* dst = &px[y * stride];
        const uint8_t* up = y ? &px[(y - 1) * stride] : nullptr;
        for (size_t i = 0; i < stride; ++i) {
            int a = i >= static_cast<size_t>(bpp) ? dst[i - static_cast<size_t>(bpp)] : 0, bb = up ? up[i] : 0, c = (up && i >= static_cast<size_t>(bpp)) ? up[i - static_cast<size_t>(bpp)] : 0;
            int v = src[i];
            switch (ft) {
                case 0: break;
                case 1: v += a; break;
                case 2: v += bb; break;
                case 3: v += (a + bb) / 2; break;
                case 4: { int p = a + bb - c, pa = std::abs(p - a), pb = std::abs(p - bb), pc = std::abs(p - c); v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? bb : c); break; }
                default: return false;
            }
            dst[i] = static_cast<uint8_t>(v);
        }
    }
    rgba->assign(static_cast<size_t>(W) * H * 4, 255);
    for (size_t i = 0; i < static_cast<size_t>(W) * H; ++i) {
        (*rgba)[i * 4] = px[i * static_cast<size_t>(bpp)];
        (*rgba)[i * 4 + 1] = px[i * static_cast<size_t>(bpp) + 1];
        (*rgba)[i * 4 + 2] = px[i * static_cast<size_t>(bpp) + 2];
        if (bpp == 4) (*rgba)[i * 4 + 3] = px[i * 4 + 3];
    }
    *w = static_cast<int>(W);
    *h = static_cast<int>(H);
    return true;
}

bool load_png(const std::string& path, int* w, int* h, std::vector<uint8_t>* rgba) {
    std::FILE* fp = std::fopen(path.c_str(), "rb");
    if (!fp) return false;
    std::vector<uint8_t> f;
    uint8_t buf[8192];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, fp)) > 0) f.insert(f.end(), buf, buf + n);
    std::fclose(fp);
    return decode_png(f, w, h, rgba);
}

}  // namespace tt::ui
