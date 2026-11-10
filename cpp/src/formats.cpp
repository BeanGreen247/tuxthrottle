#include "formats.hpp"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "util.hpp"

namespace tt {

// ---------------------------------------------------------------- base64
static const char* kB64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string base64_encode(const std::string& in) {
    std::string out;
    size_t i = 0;
    while (i + 2 < in.size()) {
        unsigned v = (static_cast<unsigned char>(in[i]) << 16) | (static_cast<unsigned char>(in[i + 1]) << 8) | static_cast<unsigned char>(in[i + 2]);
        out += kB64[(v >> 18) & 63]; out += kB64[(v >> 12) & 63]; out += kB64[(v >> 6) & 63]; out += kB64[v & 63];
        i += 3;
    }
    if (i + 1 == in.size()) {
        unsigned v = static_cast<unsigned char>(in[i]) << 16;
        out += kB64[(v >> 18) & 63]; out += kB64[(v >> 12) & 63]; out += "==";
    } else if (i + 2 == in.size()) {
        unsigned v = (static_cast<unsigned char>(in[i]) << 16) | (static_cast<unsigned char>(in[i + 1]) << 8);
        out += kB64[(v >> 18) & 63]; out += kB64[(v >> 12) & 63]; out += kB64[(v >> 6) & 63]; out += '=';
    }
    return out;
}

std::string base64_decode(const std::string& in) {
    std::string out;
    unsigned acc = 0;
    int bits = 0;
    for (char c : in) {
        if (c == '=' || c == '\n' || c == '\r' || c == ' ') continue;
        const char* p = std::strchr(kB64, c);
        if (!p || c == '\0') throw std::invalid_argument("invalid base64 character");
        acc = (acc << 6) | static_cast<unsigned>(p - kB64);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out += static_cast<char>((acc >> bits) & 0xFF);
        }
    }
    return out;
}

// ---------------------------------------------------------------- shlex
std::vector<std::string> shlex_split(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    bool in_tok = false;
    size_t i = 0;
    while (i < s.size()) {
        char c = s[i];
        if (c == ' ' || c == '\t' || c == '\n') {
            if (in_tok) { out.push_back(cur); cur.clear(); in_tok = false; }
            ++i;
        } else if (c == '\'') {
            size_t j = s.find('\'', i + 1);
            if (j == std::string::npos) throw std::invalid_argument("No closing quotation");
            cur += s.substr(i + 1, j - i - 1);
            in_tok = true;
            i = j + 1;
        } else if (c == '"') {
            ++i;
            bool closed = false;
            while (i < s.size()) {
                if (s[i] == '"') { closed = true; ++i; break; }
                if (s[i] == '\\' && i + 1 < s.size() && (s[i + 1] == '"' || s[i + 1] == '\\' || s[i + 1] == '$' || s[i + 1] == '`')) { cur += s[i + 1]; i += 2; }
                else cur += s[i++];
            }
            if (!closed) throw std::invalid_argument("No closing quotation");
            in_tok = true;
        } else if (c == '\\') {
            if (i + 1 >= s.size()) throw std::invalid_argument("No escaped character");
            cur += s[i + 1];
            in_tok = true;
            i += 2;
        } else {
            cur += c;
            in_tok = true;
            ++i;
        }
    }
    if (in_tok) out.push_back(cur);
    return out;
}

// ---------------------------------------------------------------- VDF
Vdf* Vdf::find_ci(const std::string& key) {
    for (auto& kv : children_) if (kv.first == key) return &kv.second;
    std::string kl = lower(key);
    for (auto& kv : children_) if (lower(kv.first) == kl) return &kv.second;
    return nullptr;
}
const Vdf* Vdf::find_ci(const std::string& key) const { return const_cast<Vdf*>(this)->find_ci(key); }

Vdf& Vdf::set(const std::string& key, Vdf v) {
    for (auto& kv : children_) if (kv.first == key) { kv.second = std::move(v); return kv.second; }
    children_.emplace_back(key, std::move(v));
    return children_.back().second;
}

bool Vdf::erase_ci(const std::string& key) {
    std::string kl = lower(key);
    for (auto it = children_.begin(); it != children_.end(); ++it)
        if (lower(it->first) == kl) { children_.erase(it); return true; }
    return false;
}

namespace {
struct VdfParser {
    const std::string& t;
    size_t i = 0;
    void skip_ws() {
        while (i < t.size()) {
            char c = t[i];
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') ++i;
            else if (c == '/' && i + 1 < t.size() && t[i + 1] == '/') { while (i < t.size() && t[i] != '\n') ++i; }
            else break;
        }
    }
    std::string token() {
        skip_ws();
        if (i >= t.size()) throw std::runtime_error("unexpected end of VDF");
        if (t[i] == '"') {
            ++i;
            std::string buf;
            while (i < t.size() && t[i] != '"') {
                if (t[i] == '\\' && i + 1 < t.size()) {
                    char n = t[i + 1];
                    buf += n == 'n' ? '\n' : n == 't' ? '\t' : n;
                    i += 2;
                } else buf += t[i++];
            }
            ++i;
            return buf;
        }
        size_t st = i;
        while (i < t.size() && std::string(" \t\r\n\"{}").find(t[i]) == std::string::npos) ++i;
        return t.substr(st, i - st);
    }
    Vdf block() {
        Vdf out = Vdf::block();
        while (true) {
            skip_ws();
            if (i >= t.size() || t[i] == '}') { ++i; return out; }
            std::string key = token();
            skip_ws();
            if (i < t.size() && t[i] == '{') { ++i; out.children().emplace_back(key, block()); }
            else out.children().emplace_back(key, Vdf(token()));
        }
    }
};
std::string vdf_esc(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '\\') o += "\\\\";
        else if (c == '"') o += "\\\"";
        else if (c == '\n') o += "\\n";
        else if (c == '\t') o += "\\t";
        else o += c;
    }
    return o;
}
}  // namespace

Vdf Vdf::parse(const std::string& text) {
    VdfParser p{text};
    Vdf root = Vdf::block();
    p.skip_ws();
    while (p.i < text.size()) {
        std::string key = p.token();
        p.skip_ws();
        if (p.i < text.size() && text[p.i] == '{') { ++p.i; root.children().emplace_back(key, p.block()); }
        else if (!key.empty()) root.children().emplace_back(key, Vdf(p.token()));
        p.skip_ws();
    }
    return root;
}

void Vdf::dump_block(std::string& out, int depth) const {
    std::string pad(static_cast<size_t>(depth), '\t');
    for (const auto& kv : children_) {
        if (kv.second.is_block_) {
            out += pad + "\"" + vdf_esc(kv.first) + "\"\n" + pad + "{\n";
            kv.second.dump_block(out, depth + 1);
            out += pad + "}\n";
        } else {
            out += pad + "\"" + vdf_esc(kv.first) + "\"\t\t\"" + vdf_esc(kv.second.value_) + "\"\n";
        }
    }
}

std::string Vdf::dump() const {
    std::string out;
    dump_block(out, 0);
    return out;
}

// ---------------------------------------------------------------- YAML subset
bool Yaml::truthy() const {
    if (kind == Kind::Null) return false;
    if (kind == Kind::Map) return !map.empty();
    if (kind == Kind::Seq) return !seq.empty();
    if (quoted) return !scalar.empty();
    if (scalar.empty() || scalar == "false" || scalar == "False" || scalar == "no" || scalar == "null" || scalar == "~" || scalar == "0") return false;
    return true;
}

Yaml* Yaml::get(const std::string& key) {
    for (auto& kv : map) if (kv.first == key) return &kv.second;
    return nullptr;
}
const Yaml* Yaml::get(const std::string& key) const { return const_cast<Yaml*>(this)->get(key); }

Yaml& Yaml::set(const std::string& key, Yaml v) {
    kind = Kind::Map;
    for (auto& kv : map) if (kv.first == key) { kv.second = std::move(v); return kv.second; }
    map.emplace_back(key, std::move(v));
    return map.back().second;
}

bool Yaml::erase(const std::string& key) {
    for (auto it = map.begin(); it != map.end(); ++it) if (it->first == key) { map.erase(it); return true; }
    return false;
}

namespace {
struct Line { int indent; std::string text; };

std::string strip_comment(const std::string& s) {
    bool sq = false, dq = false;
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (c == '\'' && !dq) sq = !sq;
        else if (c == '"' && !sq) dq = !dq;
        else if (c == '#' && !sq && !dq && (i == 0 || s[i - 1] == ' ')) return s.substr(0, i);
    }
    return s;
}

Yaml parse_scalar_text(const std::string& raw) {
    std::string s = trim(raw);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'') {
        std::string inner = s.substr(1, s.size() - 2), out;
        for (size_t i = 0; i < inner.size(); ++i) {
            if (inner[i] == '\'' && i + 1 < inner.size() && inner[i + 1] == '\'') { out += '\''; ++i; }
            else out += inner[i];
        }
        return Yaml::make_scalar(out, true);
    }
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"') {
        std::string inner = s.substr(1, s.size() - 2), out;
        for (size_t i = 0; i < inner.size(); ++i) {
            if (inner[i] == '\\' && i + 1 < inner.size()) {
                char n = inner[++i];
                out += n == 'n' ? '\n' : n == 't' ? '\t' : n == 'r' ? '\r' : n;
            } else out += inner[i];
        }
        return Yaml::make_scalar(out, true);
    }
    if (s == "{}") return Yaml::make_map();
    if (s == "[]") return Yaml::make_seq();
    if (s.empty() || s == "~" || s == "null" || s == "Null") return Yaml();
    return Yaml::make_scalar(s, false);
}

// splits "key: value" at the first ': ' (or trailing ':') outside quotes
bool split_key(const std::string& t, std::string& key, std::string& rest) {
    bool sq = false, dq = false;
    for (size_t i = 0; i < t.size(); ++i) {
        char c = t[i];
        if (c == '\'' && !dq) sq = !sq;
        else if (c == '"' && !sq) dq = !dq;
        else if (c == ':' && !sq && !dq && (i + 1 == t.size() || t[i + 1] == ' ')) {
            key = trim(t.substr(0, i));
            rest = i + 1 < t.size() ? trim(t.substr(i + 1)) : "";
            if (key.size() >= 2 && ((key.front() == '\'' && key.back() == '\'') || (key.front() == '"' && key.back() == '"'))) key = key.substr(1, key.size() - 2);
            return true;
        }
    }
    return false;
}

struct YamlParser {
    std::vector<Line> lines;
    size_t pos = 0;

    Yaml parse_node(int indent) {
        if (pos >= lines.size()) return Yaml();
        if (starts_with(lines[pos].text, "- ") || lines[pos].text == "-") return parse_seq(lines[pos].indent);
        std::string k, r;
        if (split_key(lines[pos].text, k, r)) return parse_map(lines[pos].indent);
        Yaml s = parse_scalar_text(lines[pos].text);
        ++pos;
        (void)indent;
        return s;
    }

    Yaml parse_map(int indent) {
        Yaml m = Yaml::make_map();
        while (pos < lines.size() && lines[pos].indent == indent) {
            std::string key, rest;
            if (!split_key(lines[pos].text, key, rest)) throw std::runtime_error("yaml: expected 'key: value' at: " + lines[pos].text);
            ++pos;
            if (!rest.empty()) m.map.emplace_back(key, parse_scalar_text(rest));
            else if (pos < lines.size() && lines[pos].indent > indent) m.map.emplace_back(key, parse_node(lines[pos].indent));
            else if (pos < lines.size() && lines[pos].indent == indent && (starts_with(lines[pos].text, "- ") || lines[pos].text == "-"))
                m.map.emplace_back(key, parse_seq(indent));  // PyYAML writes sequences at the key's own indent
            else m.map.emplace_back(key, Yaml());
        }
        return m;
    }

    Yaml parse_seq(int indent) {
        Yaml s = Yaml::make_seq();
        while (pos < lines.size() && lines[pos].indent == indent && (starts_with(lines[pos].text, "- ") || lines[pos].text == "-")) {
            std::string rest = lines[pos].text == "-" ? "" : trim(lines[pos].text.substr(2));
            std::string k, r;
            if (rest.empty()) {
                ++pos;
                if (pos < lines.size() && lines[pos].indent > indent) s.seq.push_back(parse_node(lines[pos].indent));
                else s.seq.push_back(Yaml());
            } else if (split_key(rest, k, r) && !(rest.front() == '\'' || rest.front() == '"')) {
                // "- key: value" starts an inline mapping; re-indent the line so parse_map sees its siblings
                lines[pos].indent = indent + 2;
                lines[pos].text = rest;
                s.seq.push_back(parse_map(indent + 2));
            } else {
                ++pos;
                s.seq.push_back(parse_scalar_text(rest));
            }
        }
        return s;
    }
};

bool needs_quote(const std::string& s) {
    if (s.empty()) return true;
    static const char* reserved[] = {"true", "false", "yes", "no", "null", "~", "True", "False", "Null", "on", "off", "On", "Off"};
    for (const char* r : reserved) if (s == r) return true;
    char* end = nullptr;
    std::strtod(s.c_str(), &end);
    if (end && *end == '\0') return true;                       // looks like a number
    if (s.front() == ' ' || s.back() == ' ') return true;
    if (std::string("-?:,[]{}#&*!|>'\"%@`").find(s.front()) != std::string::npos) return true;
    if (s.find(": ") != std::string::npos || s.find(" #") != std::string::npos || s.back() == ':') return true;
    if (s.find('\n') != std::string::npos) return true;
    return false;
}

std::string emit_scalar(const Yaml& y) {
    if (y.kind == Yaml::Kind::Null) return "null";
    if (!y.quoted && !needs_quote(y.scalar)) return y.scalar;
    if (!y.quoted && (y.scalar == "true" || y.scalar == "false" || y.scalar == "null")) return y.scalar;
    char* end = nullptr;
    if (!y.quoted && !y.scalar.empty()) { std::strtod(y.scalar.c_str(), &end); if (end && *end == '\0') return y.scalar; }
    if (y.scalar.find('\n') != std::string::npos) {
        std::string o = "\"";
        for (char c : y.scalar) o += c == '\n' ? std::string("\\n") : c == '"' ? std::string("\\\"") : c == '\\' ? std::string("\\\\") : std::string(1, c);
        return o + "\"";
    }
    std::string o = "'";
    for (char c : y.scalar) o += c == '\'' ? std::string("''") : std::string(1, c);
    return o + "'";
}

void emit(const Yaml& y, int indent, std::string& out, bool in_seq_item) {
    std::string pad(static_cast<size_t>(indent), ' ');
    if (y.kind == Yaml::Kind::Map) {
        bool first = true;
        for (const auto& kv : y.map) {
            std::string key = needs_quote(kv.first) ? emit_scalar(Yaml::make_scalar(kv.first, true)) : kv.first;
            std::string lead = (first && in_seq_item) ? "" : pad;
            first = false;
            const Yaml& v = kv.second;
            if (v.kind == Yaml::Kind::Map && !v.map.empty()) { out += lead + key + ":\n"; emit(v, indent + 2, out, false); }
            else if (v.kind == Yaml::Kind::Seq && !v.seq.empty()) { out += lead + key + ":\n"; emit(v, indent, out, false); }
            else if (v.kind == Yaml::Kind::Map) out += lead + key + ": {}\n";
            else if (v.kind == Yaml::Kind::Seq) out += lead + key + ": []\n";
            else out += lead + key + ": " + emit_scalar(v) + "\n";
        }
    } else if (y.kind == Yaml::Kind::Seq) {
        for (const auto& item : y.seq) {
            if (item.kind == Yaml::Kind::Map && !item.map.empty()) { out += pad + "- "; emit(item, indent + 2, out, true); }
            else if (item.kind == Yaml::Kind::Seq && !item.seq.empty()) { out += pad + "-\n"; emit(item, indent + 2, out, false); }
            else if (item.kind == Yaml::Kind::Map) out += pad + "- {}\n";
            else if (item.kind == Yaml::Kind::Seq) out += pad + "- []\n";
            else out += pad + "- " + emit_scalar(item) + "\n";
        }
    } else {
        out += pad + emit_scalar(y) + "\n";
    }
}
}  // namespace

Yaml Yaml::parse(const std::string& text) {
    YamlParser p;
    for (const auto& raw : split_lines(text)) {
        std::string line = strip_comment(raw);
        // trim trailing whitespace; skip blank lines and document markers
        while (!line.empty() && (line.back() == ' ' || line.back() == '\t' || line.back() == '\r')) line.pop_back();
        size_t ind = 0;
        while (ind < line.size() && line[ind] == ' ') ++ind;
        std::string body = line.substr(ind);
        if (body.empty() || body == "---" || body == "...") continue;
        if (body[0] == '\t') throw std::runtime_error("yaml: tabs are not allowed for indentation");
        p.lines.push_back({static_cast<int>(ind), body});
    }
    if (p.lines.empty()) return Yaml();
    Yaml r = p.parse_node(p.lines[0].indent);
    if (p.pos < p.lines.size()) throw std::runtime_error("yaml: unexpected content: " + p.lines[p.pos].text);
    return r;
}

std::string Yaml::dump() const {
    std::string out;
    if (kind == Kind::Map && map.empty()) return "{}\n";
    if (kind == Kind::Seq && seq.empty()) return "[]\n";
    emit(*this, 0, out, false);
    return out;
}

// ---------------------------------------------------------------- SQLite (read only)
std::string SqlValue::str() const {
    if (std::holds_alternative<std::string>(v)) return std::get<std::string>(v);
    if (std::holds_alternative<long long>(v)) return std::to_string(std::get<long long>(v));
    if (std::holds_alternative<double>(v)) return format_double(std::get<double>(v), 6);
    return "";
}
long long SqlValue::integer(long long def) const {
    if (std::holds_alternative<long long>(v)) return std::get<long long>(v);
    if (std::holds_alternative<double>(v)) return static_cast<long long>(std::get<double>(v));
    if (std::holds_alternative<std::string>(v)) return std::atoll(std::get<std::string>(v).c_str());
    return def;
}

struct SqliteReader::Impl {
    std::string data;
    unsigned page_size = 4096;
    unsigned usable = 4096;
    bool utf8 = true;

    unsigned long long be(size_t off, int n) const {
        unsigned long long v = 0;
        for (int i = 0; i < n; ++i) v = (v << 8) | static_cast<unsigned char>(data[off + static_cast<size_t>(i)]);
        return v;
    }
    const char* page(unsigned n) const { return data.data() + static_cast<size_t>(n - 1) * page_size; }
    bool valid_page(unsigned n) const { return n >= 1 && static_cast<size_t>(n) * page_size <= data.size(); }

    static size_t varint(const unsigned char* p, size_t avail, long long* out) {
        unsigned long long v = 0;
        for (size_t i = 0; i < 9 && i < avail; ++i) {
            if (i == 8) { v = (v << 8) | p[i]; *out = static_cast<long long>(v); return 9; }
            v = (v << 7) | (p[i] & 0x7F);
            if (!(p[i] & 0x80)) { *out = static_cast<long long>(v); return i + 1; }
        }
        *out = static_cast<long long>(v);
        return avail;
    }

    // payload of a cell (follows the overflow chain)
    std::string read_payload(const unsigned char* cell, size_t cell_avail, unsigned long long total, bool leaf_table, size_t* header_len) const {
        (void)leaf_table;
        unsigned long long x = usable - 35;                       // max local for table leaf
        unsigned long long m = ((usable - 12) * 32 / 255) - 23;   // min local
        unsigned long long local;
        if (total <= x) local = total;
        else {
            local = m + ((total - m) % (usable - 4));
            if (local > x) local = m;
        }
        std::string out(reinterpret_cast<const char*>(cell + *header_len), static_cast<size_t>(std::min<unsigned long long>(local, cell_avail - *header_len)));
        if (local < total) {
            unsigned next = static_cast<unsigned>(be(static_cast<size_t>(cell - reinterpret_cast<const unsigned char*>(data.data())) + *header_len + local, 4));
            unsigned long long remaining = total - local;
            while (next && remaining && valid_page(next)) {
                const unsigned char* pg = reinterpret_cast<const unsigned char*>(page(next));
                unsigned nn = static_cast<unsigned>((pg[0] << 24) | (pg[1] << 16) | (pg[2] << 8) | pg[3]);
                size_t take = static_cast<size_t>(std::min<unsigned long long>(remaining, usable - 4));
                out.append(reinterpret_cast<const char*>(pg + 4), take);
                remaining -= take;
                next = nn;
            }
        }
        return out;
    }

    std::vector<SqlValue> decode_record(const std::string& rec) const {
        std::vector<SqlValue> row;
        const unsigned char* p = reinterpret_cast<const unsigned char*>(rec.data());
        long long hdr_size = 0;
        size_t off = varint(p, rec.size(), &hdr_size);
        std::vector<long long> types;
        while (off < static_cast<size_t>(hdr_size) && off < rec.size()) {
            long long t = 0;
            off += varint(p + off, rec.size() - off, &t);
            types.push_back(t);
        }
        size_t d = static_cast<size_t>(hdr_size);
        for (long long t : types) {
            SqlValue v;
            auto need = [&](size_t n) { return d + n <= rec.size(); };
            if (t == 0) {}
            else if (t >= 1 && t <= 6) {
                static const int sizes[] = {0, 1, 2, 3, 4, 6, 8};
                size_t n = static_cast<size_t>(sizes[t]);
                if (!need(n)) break;
                long long val = static_cast<signed char>(rec[d]);
                for (size_t i = 1; i < n; ++i) val = (val << 8) | static_cast<unsigned char>(rec[d + i]);
                v.v = val;
                d += n;
            } else if (t == 7) {
                if (!need(8)) break;
                unsigned long long bits = 0;
                for (int i = 0; i < 8; ++i) bits = (bits << 8) | static_cast<unsigned char>(rec[d + static_cast<size_t>(i)]);
                double f;
                std::memcpy(&f, &bits, 8);
                v.v = f;
                d += 8;
            } else if (t == 8) v.v = 0LL;
            else if (t == 9) v.v = 1LL;
            else if (t >= 12) {
                size_t n = static_cast<size_t>((t - (t % 2 == 0 ? 12 : 13)) / 2);
                if (!need(n)) break;
                v.v = rec.substr(d, n);
                d += n;
            }
            row.push_back(std::move(v));
        }
        return row;
    }

    // walk a table b-tree, emitting (rowid, record)
    void walk(unsigned pgno, std::vector<std::pair<long long, std::string>>& out, int depth = 0) const {
        if (depth > 40 || !valid_page(pgno)) return;
        const unsigned char* pg = reinterpret_cast<const unsigned char*>(page(pgno));
        size_t hdr_base = pgno == 1 ? 100 : 0;
        unsigned type = pg[hdr_base];
        unsigned ncells = static_cast<unsigned>((pg[hdr_base + 3] << 8) | pg[hdr_base + 4]);
        if (type == 13) {  // leaf table
            for (unsigned i = 0; i < ncells; ++i) {
                size_t ptr_off = hdr_base + 8 + 2 * i;
                unsigned cell_off = static_cast<unsigned>((pg[ptr_off] << 8) | pg[ptr_off + 1]);
                if (cell_off >= page_size) continue;
                const unsigned char* c = pg + cell_off;
                size_t avail = page_size - cell_off;
                long long payload = 0, rowid = 0;
                size_t h = varint(c, avail, &payload);
                h += varint(c + h, avail - h, &rowid);
                out.emplace_back(rowid, read_payload(c, avail, static_cast<unsigned long long>(payload), true, &h));
            }
        } else if (type == 5) {  // interior table
            for (unsigned i = 0; i < ncells; ++i) {
                size_t ptr_off = hdr_base + 12 + 2 * i;
                unsigned cell_off = static_cast<unsigned>((pg[ptr_off] << 8) | pg[ptr_off + 1]);
                if (cell_off + 4 > page_size) continue;
                unsigned child = static_cast<unsigned>((pg[cell_off] << 24) | (pg[cell_off + 1] << 16) | (pg[cell_off + 2] << 8) | pg[cell_off + 3]);
                walk(child, out, depth + 1);
            }
            unsigned right = static_cast<unsigned>((pg[hdr_base + 8] << 24) | (pg[hdr_base + 9] << 16) | (pg[hdr_base + 10] << 8) | pg[hdr_base + 11]);
            walk(right, out, depth + 1);
        }
    }
};

SqliteReader::SqliteReader(const std::string& path) : impl_(std::make_shared<Impl>()) {
    auto t = read_file(path);
    if (!t || t->size() < 100 || t->compare(0, 15, "SQLite format 3") != 0) return;
    impl_->data = std::move(*t);
    unsigned ps = static_cast<unsigned>(impl_->be(16, 2));
    impl_->page_size = ps == 1 ? 65536 : ps;
    impl_->usable = impl_->page_size - static_cast<unsigned>(static_cast<unsigned char>(impl_->data[20]));
    ok_ = impl_->page_size >= 512;
}

static std::vector<std::string> parse_columns(const std::string& sql) {
    std::vector<std::string> cols;
    size_t open = sql.find('(');
    if (open == std::string::npos) return cols;
    int depth = 0;
    std::string cur;
    auto flush = [&] {
        std::string t = trim(cur);
        cur.clear();
        if (t.empty()) return;
        std::string first = split_ws(t)[0];
        std::string up = first;
        for (auto& c : up) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        if (up == "PRIMARY" || up == "UNIQUE" || up == "FOREIGN" || up == "CONSTRAINT" || up == "CHECK") return;
        if (first.size() >= 2 && (first.front() == '"' || first.front() == '`' || first.front() == '[')) first = first.substr(1, first.size() - 2);
        cols.push_back(first);
    };
    for (size_t i = open + 1; i < sql.size(); ++i) {
        char c = sql[i];
        if (c == '(') { ++depth; cur += c; }
        else if (c == ')') { if (depth == 0) { flush(); break; } --depth; cur += c; }
        else if (c == ',' && depth == 0) flush();
        else cur += c;
    }
    return cols;
}

SqliteReader::Table SqliteReader::read_table(const std::string& name) const {
    Table t;
    if (!ok_) return t;
    std::vector<std::pair<long long, std::string>> master;
    impl_->walk(1, master);
    for (const auto& [rowid, rec] : master) {
        auto row = impl_->decode_record(rec);
        if (row.size() < 5 || row[0].str() != "table" || row[1].str() != name) continue;
        unsigned root = static_cast<unsigned>(row[3].integer());
        t.columns = parse_columns(row[4].str());
        std::vector<std::pair<long long, std::string>> rows;
        impl_->walk(root, rows);
        for (const auto& [rid, r] : rows) {
            auto vals = impl_->decode_record(r);
            // an INTEGER PRIMARY KEY column is stored as NULL in the record; fill it from the rowid
            if (!vals.empty() && !t.columns.empty() && vals[0].is_null()) vals[0].v = rid;
            vals.resize(std::max(vals.size(), t.columns.size()));
            t.rows.push_back(std::move(vals));
        }
        break;
    }
    return t;
}

}  // namespace tt
