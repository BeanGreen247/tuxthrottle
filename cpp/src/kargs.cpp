#include "kargs.hpp"

#include <algorithm>

#include "util.hpp"

namespace tt::kargs {

static std::string key_of(const std::string& t) { return t.substr(0, t.find('=')); }
static bool has_eq(const std::string& t) { return t.find('=') != std::string::npos; }

std::vector<std::string> edit_tokens(std::vector<std::string> tokens, const std::vector<std::string>& add,
                                     const std::vector<std::string>& del) {
    for (const auto& d : del) {
        tokens.erase(std::remove_if(tokens.begin(), tokens.end(),
                                    [&](const std::string& t) { return has_eq(d) ? t == d : key_of(t) == d; }),
                     tokens.end());
    }
    for (const auto& a : add) {
        if (has_eq(a))
            tokens.erase(std::remove_if(tokens.begin(), tokens.end(),
                                        [&](const std::string& t) { return key_of(t) == key_of(a); }),
                         tokens.end());
        if (std::find(tokens.begin(), tokens.end(), a) == tokens.end()) tokens.push_back(a);
    }
    return tokens;
}

bool has_tokens(const std::vector<std::string>& tokens, const std::vector<std::string>& want) {
    for (const auto& w : want) {
        bool found = false;
        for (const auto& t : tokens)
            if (has_eq(w) ? t == w : key_of(t) == w) { found = true; break; }
        if (!found) return false;
    }
    return true;
}

namespace {
struct GrubLine {
    size_t begin = std::string::npos, end = 0;  // the whole line, without trailing newline
    std::string value;
};
// Finds `GRUB_CMDLINE_LINUX_DEFAULT="..."` (or single-quoted) at the start of a line.
GrubLine find_line(const std::string& text) {
    static const std::string key = "GRUB_CMDLINE_LINUX_DEFAULT=";
    size_t pos = 0;
    while (pos <= text.size()) {
        size_t eol = text.find('\n', pos);
        if (eol == std::string::npos) eol = text.size();
        if (text.compare(pos, key.size(), key) == 0) {
            std::string rest = trim(text.substr(pos + key.size(), eol - pos - key.size()));
            if (rest.size() >= 2 && (rest.front() == '"' || rest.front() == '\'') && rest.back() == rest.front())
                return {pos, eol, rest.substr(1, rest.size() - 2)};
        }
        pos = eol + 1;
    }
    return {};
}
}  // namespace

std::vector<std::string> grub_tokens(const std::string& text) {
    GrubLine g = find_line(text);
    return g.begin == std::string::npos ? std::vector<std::string>{} : split_ws(g.value);
}

std::string grub_edit(const std::string& text, const std::vector<std::string>& add,
                      const std::vector<std::string>& del) {
    GrubLine g = find_line(text);
    if (g.begin == std::string::npos) {
        std::string base = text;
        while (!base.empty() && base.back() == '\n') base.pop_back();
        return base + "\nGRUB_CMDLINE_LINUX_DEFAULT=\"" + join(edit_tokens({}, add, del), " ") + "\"\n";
    }
    auto toks = edit_tokens(split_ws(g.value), add, del);
    return text.substr(0, g.begin) + "GRUB_CMDLINE_LINUX_DEFAULT=\"" + join(toks, " ") + "\"" + text.substr(g.end);
}

}  // namespace tt::kargs
