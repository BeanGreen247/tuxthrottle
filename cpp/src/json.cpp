#include "json.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <stdexcept>

namespace tt {

static const Json kNull;
static const std::string kEmptyString;
static const Json::Array kEmptyArray;
static const Json::Object kEmptyObject;

const std::string& Json::as_string() const {
    return is_string() ? std::get<std::string>(v_) : kEmptyString;
}
const Json::Array& Json::array() const& { return is_array() ? std::get<Array>(v_) : kEmptyArray; }
Json::Array Json::array() && { return is_array() ? std::get<Array>(std::move(v_)) : Array{}; }
const Json::Object& Json::object() const& { return is_object() ? std::get<Object>(v_) : kEmptyObject; }
Json::Object Json::object() && { return is_object() ? std::get<Object>(std::move(v_)) : Object{}; }

Json Json::operator[](const std::string& key) && {
    const Json& r = static_cast<const Json&>(*this)[key];
    return r;
}

const Json& Json::operator[](const std::string& key) const& {
    if (is_object())
        for (const auto& kv : std::get<Object>(v_))
            if (kv.first == key) return kv.second;
    return kNull;
}
bool Json::has(const std::string& key) const {
    if (is_object())
        for (const auto& kv : std::get<Object>(v_))
            if (kv.first == key) return true;
    return false;
}
Json& Json::set(const std::string& key, Json value) {
    auto& o = make_object();
    for (auto& kv : o)
        if (kv.first == key) { kv.second = std::move(value); return kv.second; }
    o.emplace_back(key, std::move(value));
    return o.back().second;
}
bool Json::erase(const std::string& key) {
    if (!is_object()) return false;
    auto& o = std::get<Object>(v_);
    for (auto it = o.begin(); it != o.end(); ++it)
        if (it->first == key) { o.erase(it); return true; }
    return false;
}
Json::Array& Json::make_array() {
    if (!is_array()) v_ = Array{};
    return std::get<Array>(v_);
}
Json::Object& Json::make_object() {
    if (!is_object()) v_ = Object{};
    return std::get<Object>(v_);
}

// ---------------------------------------------------------------- parser
namespace {
struct Parser {
    const std::string& s;
    size_t i = 0;

    [[noreturn]] void fail(const char* msg) const {
        throw std::runtime_error(std::string("json: ") + msg + " at offset " + std::to_string(i));
    }
    void ws() {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\t' || s[i] == '\r')) ++i;
    }
    bool eat(char c) {
        ws();
        if (i < s.size() && s[i] == c) { ++i; return true; }
        return false;
    }
    void expect(char c) {
        if (!eat(c)) fail("unexpected character");
    }
    static void put_utf8(std::string& out, unsigned cp) {
        if (cp < 0x80) out += static_cast<char>(cp);
        else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }
    unsigned hex4() {
        if (i + 4 > s.size()) fail("bad \\u escape");
        unsigned v = 0;
        for (int k = 0; k < 4; ++k) {
            char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else fail("bad hex digit");
        }
        return v;
    }
    std::string string() {
        expect('"');
        std::string out;
        while (true) {
            if (i >= s.size()) fail("unterminated string");
            char c = s[i++];
            if (c == '"') break;
            if (c != '\\') { out += c; continue; }
            if (i >= s.size()) fail("bad escape");
            char e = s[i++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    unsigned cp = hex4();
                    if (cp >= 0xD800 && cp < 0xDC00 && i + 1 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                        i += 2;
                        unsigned lo = hex4();
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    }
                    put_utf8(out, cp);
                    break;
                }
                default: fail("bad escape");
            }
        }
        return out;
    }
    Json value(int depth) {
        if (depth > 200) fail("nesting too deep");
        ws();
        if (i >= s.size()) fail("unexpected end");
        char c = s[i];
        if (c == '{') {
            ++i;
            Json::Object o;
            if (eat('}')) return Json(std::move(o));
            do {
                ws();
                std::string k = string();
                expect(':');
                o.emplace_back(std::move(k), value(depth + 1));
            } while (eat(','));
            expect('}');
            return Json(std::move(o));
        }
        if (c == '[') {
            ++i;
            Json::Array a;
            if (eat(']')) return Json(std::move(a));
            do a.push_back(value(depth + 1));
            while (eat(','));
            expect(']');
            return Json(std::move(a));
        }
        if (c == '"') return Json(string());
        if (s.compare(i, 4, "true") == 0) { i += 4; return Json(true); }
        if (s.compare(i, 5, "false") == 0) { i += 5; return Json(false); }
        if (s.compare(i, 4, "null") == 0) { i += 4; return Json(); }
        double d = 0;
        auto r = std::from_chars(s.data() + i, s.data() + s.size(), d);
        if (r.ec != std::errc()) fail("unexpected token");
        i = static_cast<size_t>(r.ptr - s.data());
        return Json(d);
    }
};

void escape(std::string& out, const std::string& v) {
    out += '"';
    for (unsigned char c : v) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else out += static_cast<char>(c);
        }
    }
    out += '"';
}
}  // namespace

Json Json::parse(const std::string& text) {
    Parser p{text};
    Json v = p.value(0);
    p.ws();
    if (p.i != text.size()) p.fail("trailing data");
    return v;
}

void Json::dump_to(std::string& out, int indent, int depth, bool sort_keys) const {
    auto nl = [&](int d) {
        if (indent > 0) { out += '\n'; out.append(static_cast<size_t>(indent * d), ' '); }
    };
    if (is_null()) out += "null";
    else if (is_bool()) out += std::get<bool>(v_) ? "true" : "false";
    else if (is_number()) {
        double d = std::get<double>(v_);
        char buf[40];
        if (d == std::floor(d) && std::fabs(d) < 1e15) std::snprintf(buf, sizeof buf, "%.0f", d);
        else std::snprintf(buf, sizeof buf, "%.15g", d);
        out += buf;
    } else if (is_string()) escape(out, std::get<std::string>(v_));
    else if (is_array()) {
        const auto& a = std::get<Array>(v_);
        if (a.empty()) { out += "[]"; return; }
        out += '[';
        for (size_t k = 0; k < a.size(); ++k) {
            if (k) out += ',';
            nl(depth + 1);
            a[k].dump_to(out, indent, depth + 1, sort_keys);
        }
        nl(depth);
        out += ']';
    } else {
        Object sorted;
        if (sort_keys) {
            sorted = std::get<Object>(v_);
            std::stable_sort(sorted.begin(), sorted.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        }
        const auto& o = sort_keys ? sorted : std::get<Object>(v_);
        if (o.empty()) { out += "{}"; return; }
        out += '{';
        for (size_t k = 0; k < o.size(); ++k) {
            if (k) out += ',';
            nl(depth + 1);
            escape(out, o[k].first);
            out += indent > 0 ? ": " : ":";
            o[k].second.dump_to(out, indent, depth + 1, sort_keys);
        }
        nl(depth);
        out += '}';
    }
}

std::string Json::dump(int indent, bool sort_keys) const {
    std::string out;
    dump_to(out, indent, 0, sort_keys);
    return out;
}

}  // namespace tt
