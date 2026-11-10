#include "distro.hpp"

#include <algorithm>
#include <map>

#include "util.hpp"

namespace tt {

namespace {
const std::map<std::string, std::string> kFamilyById = {
    {"fedora", "fedora"}, {"rhel", "fedora"}, {"centos", "fedora"}, {"rocky", "fedora"},
    {"almalinux", "fedora"}, {"nobara", "fedora"}, {"bazzite", "fedora"}, {"ultramarine", "fedora"},
    {"debian", "debian"}, {"ubuntu", "debian"}, {"linuxmint", "debian"}, {"pop", "debian"},
    {"neon", "debian"}, {"kali", "debian"}, {"elementary", "debian"}, {"zorin", "debian"},
    {"raspbian", "debian"}, {"steamos", "arch"},
    {"arch", "arch"}, {"manjaro", "arch"}, {"endeavouros", "arch"}, {"cachyos", "arch"},
    {"garuda", "arch"}, {"artix", "arch"}, {"arcolinux", "arch"},
    {"gentoo", "gentoo"}, {"funtoo", "gentoo"}, {"calculate", "gentoo"},
    {"opensuse-tumbleweed", "suse"}, {"opensuse-leap", "suse"}, {"opensuse", "suse"}, {"sles", "suse"},
};
const char* kAtomicVariants[] = {"silverblue", "kinoite", "sericea", "onyx", "iot", "coreos", "cosmic-atomic"};
const char* kAtomicIds[] = {"bazzite", "bluefin", "aurora", "universal-blue", "steamos"};

Json g_pkgmap;
}  // namespace

std::string os_release_value(const std::string& text, const std::string& key) {
    for (const auto& line : split_lines(text)) {
        std::string l = trim(line);
        if (!starts_with(l, key + "=")) continue;
        std::string v = trim(l.substr(key.size() + 1));
        if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front())
            v = v.substr(1, v.size() - 2);
        return v;
    }
    return "";
}

Distro detect_distro(const std::string& os_release, bool ostree_booted) {
    Distro d;
    std::string id = lower(os_release_value(os_release, "ID"));
    d.id = id.empty() ? "unknown" : id;
    d.like = split_ws(lower(os_release_value(os_release, "ID_LIKE")));
    d.version = os_release_value(os_release, "VERSION_ID");
    std::string pretty = os_release_value(os_release, "PRETTY_NAME");
    if (pretty.empty()) pretty = os_release_value(os_release, "NAME");
    d.name = pretty.empty() ? d.id : pretty;

    auto it = kFamilyById.find(d.id);
    if (it != kFamilyById.end()) d.family = it->second;
    else
        for (const auto& l : d.like) {
            auto j = kFamilyById.find(l);
            if (j != kFamilyById.end()) { d.family = j->second; break; }
        }
    if (d.family == "unknown" &&
        (std::find(d.like.begin(), d.like.end(), "rhel") != d.like.end() ||
         std::find(d.like.begin(), d.like.end(), "fedora") != d.like.end()))
        d.family = "fedora";

    std::string variant = lower(os_release_value(os_release, "VARIANT_ID"));
    d.atomic = ostree_booted;
    for (const char* v : kAtomicVariants) d.atomic = d.atomic || variant == v;
    for (const char* v : kAtomicIds) d.atomic = d.atomic || d.id == v;

    if (d.family == "fedora") d.pkg = d.atomic ? "rpm-ostree" : "dnf";
    else if (d.family == "debian") d.pkg = "apt";
    else if (d.family == "arch") d.pkg = "pacman";
    else if (d.family == "gentoo") d.pkg = "emerge";
    else if (d.family == "suse") d.pkg = "zypper";
    return d;
}

const Distro& current_distro() {
    static const Distro d = [] {
        auto t = read_file("/etc/os-release");
        if (!t) t = read_file("/usr/lib/os-release");
        return detect_distro(t ? *t : "", path_exists("/run/ostree-booted"));
    }();
    return d;
}

void set_pkgmap(Json map) { g_pkgmap = std::move(map); }

void load_pkgmap(const std::string& config_dir) {
    auto t = read_file(config_dir + "/pkgmap.json");
    if (!t) return;
    try {
        g_pkgmap = Json::parse(*t);
    } catch (const std::exception&) {
    }
}

std::optional<std::string> resolve_pkg(const std::string& logical, const Distro& d) {
    const Json& e = g_pkgmap[logical];
    if (!e.is_object()) return logical;
    if (e.has(d.family)) {
        const Json& v = e[d.family];
        if (v.is_string()) return v.as_string();
        return std::nullopt;
    }
    if (e["default"].is_string()) return e["default"].as_string();
    return logical;
}

std::optional<std::string> flatpak_id(const std::string& logical) {
    const Json& e = g_pkgmap[logical];
    if (e.is_object() && e["flatpak"].is_string()) return e["flatpak"].as_string();
    return std::nullopt;
}

static std::string quote_all(const std::vector<std::string>& v) {
    std::vector<std::string> q;
    for (const auto& s : v) q.push_back(shell_quote(s));
    return join(q, " ");
}

static std::string flatpak_install(const std::string& id) {
    return "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo && "
           "flatpak install -y flathub " + shell_quote(id);
}

std::string pkg_install(const std::vector<std::string>& logical, const Distro& d) {
    std::vector<std::string> cmds, names;
    for (const auto& lg : logical) {
        auto n = resolve_pkg(lg, d);
        if (n) {
            for (const auto& w : split_ws(*n)) names.push_back(w);
        } else if (auto f = flatpak_id(lg)) {
            cmds.push_back(flatpak_install(*f));
        } else {
            cmds.push_back("echo 'No package for " + lg + " on " + d.name + "' >&2; exit 1");
        }
    }
    if (!names.empty()) {
        std::string q = quote_all(names), base;
        if (d.pkg == "dnf") base = "dnf install -y " + q;
        else if (d.pkg == "rpm-ostree") base = "rpm-ostree install --idempotent --allow-inactive " + q;
        else if (d.pkg == "apt") base = "DEBIAN_FRONTEND=noninteractive apt-get install -y " + q;
        else if (d.pkg == "pacman") base = "pacman -S --needed --noconfirm " + q;
        else if (d.pkg == "emerge") base = "emerge --noreplace " + q;
        else if (d.pkg == "zypper") base = "zypper --non-interactive install " + q;
        else base = "echo 'Unsupported distro " + d.name + "' >&2; exit 1";
        cmds.insert(cmds.begin(), base);
    }
    return cmds.empty() ? "true" : join(cmds, " && ");
}

std::string pkg_remove(const std::vector<std::string>& logical, const Distro& d) {
    std::vector<std::string> names;
    for (const auto& lg : logical)
        if (auto n = resolve_pkg(lg, d))
            for (const auto& w : split_ws(*n)) names.push_back(w);
    if (names.empty()) return "true";
    std::string q = quote_all(names);
    if (d.pkg == "dnf") return "dnf remove -y " + q;
    if (d.pkg == "rpm-ostree") return "rpm-ostree uninstall " + q;
    if (d.pkg == "apt") return "DEBIAN_FRONTEND=noninteractive apt-get remove -y " + q;
    if (d.pkg == "pacman") return "pacman -Rs --noconfirm " + q;
    if (d.pkg == "emerge") return "emerge --deselect " + q;
    if (d.pkg == "zypper") return "zypper --non-interactive remove " + q;
    return "false";
}

std::string pkg_check(const std::vector<std::string>& logical, const Distro& d) {
    std::vector<std::string> parts;
    for (const auto& lg : logical) {
        auto n = resolve_pkg(lg, d);
        if (!n) { parts.push_back("false"); continue; }
        for (const auto& w : split_ws(*n)) {
            std::string q = shell_quote(w);
            if (d.pkg == "dnf" || d.pkg == "rpm-ostree" || d.pkg == "zypper")
                parts.push_back("rpm -q " + q + " >/dev/null 2>&1");
            else if (d.pkg == "apt")
                parts.push_back("dpkg-query -W -f='${Status}' " + q + " 2>/dev/null | grep -q 'install ok installed'");
            else if (d.pkg == "pacman") parts.push_back("pacman -Qi " + q + " >/dev/null 2>&1");
            else if (d.pkg == "emerge")
                parts.push_back("qlist -I " + q + " >/dev/null 2>&1 || ls /var/db/pkg/*/" + q + "-[0-9]* >/dev/null 2>&1");
            else parts.push_back("false");
        }
    }
    return parts.empty() ? "true" : join(parts, " && ");
}

std::string pkg_update(const Distro& d) {
    if (d.pkg == "dnf") return "dnf upgrade -y";
    if (d.pkg == "rpm-ostree") return "rpm-ostree upgrade";
    if (d.pkg == "apt") return "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y";
    if (d.pkg == "pacman") return "pacman -Syu --noconfirm";
    if (d.pkg == "emerge") return "emaint sync -a && emerge -uDN @world";
    if (d.pkg == "zypper") return "zypper --non-interactive dup";
    return "false";
}

std::string kargs_cmd(const std::string& action, const std::string& args, const Distro& d,
                      const std::string& kargs_tool) {
    if (d.family == "fedora" && action == "check") {
        std::vector<std::string> parts;
        for (const auto& a : split_ws(args))
            parts.push_back(d.atomic ? "rpm-ostree kargs 2>/dev/null | grep -qwF -- " + shell_quote(a)
                                     : "grubby --info=DEFAULT 2>/dev/null | grep -qF -- " + shell_quote(a));
        return join(parts, " && ");
    }
    if (d.family == "fedora" && (action == "add" || action == "del")) {
        if (d.atomic) {
            std::vector<std::string> parts;
            for (const auto& a : split_ws(args))
                parts.push_back(std::string("rpm-ostree kargs ") +
                                (action == "add" ? "--append-if-missing=" : "--delete=") + shell_quote(a));
            return join(parts, " && ");
        }
        return std::string("grubby --update-kernel=ALL ") + (action == "add" ? "--args=" : "--remove-args=") +
               shell_quote(args);
    }
    return kargs_tool + " " + action + " " + shell_quote(args);
}

std::string expand(const std::string& cmd, const Distro& d, const std::string& kargs_tool) {
    static const char* kinds[] = {"PKG_INSTALL", "PKG_REMOVE", "PKG_CHECK", "PKG_UPDATE",
                                  "KARGS_ADD", "KARGS_DEL", "KARGS_CHECK", "KARGS_LIVE"};
    std::string out;
    size_t i = 0;
    while (i < cmd.size()) {
        if (cmd[i] != '{') { out += cmd[i++]; continue; }
        size_t close = cmd.find('}', i);
        size_t inner_open = cmd.find('{', i + 1);
        if (close == std::string::npos || (inner_open != std::string::npos && inner_open < close)) {
            out += cmd[i++];
            continue;
        }
        std::string body = cmd.substr(i + 1, close - i - 1);
        std::string kind = body.substr(0, body.find(':'));
        std::string arg = body.find(':') == std::string::npos ? "" : trim(body.substr(body.find(':') + 1));
        bool known = false;
        for (const char* k : kinds) known = known || kind == k;
        if (!known) { out += cmd[i++]; continue; }
        auto names = split_ws(arg);
        if (kind == "PKG_INSTALL") out += pkg_install(names, d);
        else if (kind == "PKG_REMOVE") out += pkg_remove(names, d);
        else if (kind == "PKG_CHECK") out += pkg_check(names, d);
        else if (kind == "PKG_UPDATE") out += pkg_update(d);
        else if (kind == "KARGS_ADD") out += kargs_cmd("add", arg, d, kargs_tool);
        else if (kind == "KARGS_DEL") out += kargs_cmd("del", arg, d, kargs_tool);
        else if (kind == "KARGS_CHECK") out += kargs_cmd("check", arg, d, kargs_tool);
        else out += kargs_cmd("live", arg, d, kargs_tool);
        i = close + 1;
    }
    return out;
}

bool needs_fedora(const std::vector<std::string>& cmds) {
    static const char* tools[] = {"dnf", "dnf5", "grubby", "rpm-ostree", "nobara-sync", "dracut", "akmods"};
    for (const auto& c : cmds) {
        // word-boundary scan: a tool name preceded by start/space/;&|( and followed by space/end
        for (const char* t : tools) {
            std::string tool = t;
            size_t pos = 0;
            while ((pos = c.find(tool, pos)) != std::string::npos) {
                bool pre = pos == 0 || std::string(" \t\n;&|(").find(c[pos - 1]) != std::string::npos;
                size_t e = pos + tool.size();
                bool post = e == c.size() || c[e] == ' ' || c[e] == '\t' || c[e] == '\n';
                if (pre && post) return true;
                pos += tool.size();
            }
        }
        // nobara-* helpers and akmod-* packages
        for (const char* pfx : {"nobara-", "akmod-"}) {
            size_t pos = 0;
            while ((pos = c.find(pfx, pos)) != std::string::npos) {
                bool pre = pos == 0 || std::string(" \t\n;&|(").find(c[pos - 1]) != std::string::npos;
                if (pre) return true;
                pos += 6;
            }
        }
    }
    return false;
}

bool portable(const std::vector<std::string>& cmds, const Distro& d) {
    return (d.family == "fedora" && !d.atomic) || !needs_fedora(cmds);
}

}  // namespace tt
