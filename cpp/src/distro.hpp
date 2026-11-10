// Distro detection and the package / boot-arg command builders.
// Placeholders usable in config commands:
//   {PKG_INSTALL:a b} {PKG_REMOVE:a} {PKG_CHECK:a} {PKG_UPDATE}
//   {KARGS_ADD:x=1} {KARGS_DEL:x} {KARGS_CHECK:x=1} {KARGS_LIVE:x=1}
#pragma once
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt {

struct Distro {
    std::string id = "unknown", name = "unknown", version;
    std::vector<std::string> like;
    std::string family = "unknown";  // fedora debian arch gentoo suse unknown
    bool atomic = false;
    std::string pkg;  // dnf rpm-ostree apt pacman emerge zypper
};

std::string os_release_value(const std::string& text, const std::string& key);
Distro detect_distro(const std::string& os_release, bool ostree_booted);
const Distro& current_distro();  // reads /etc/os-release once

// config/pkgmap.json: logical name -> per-family package name (null = not in repos).
void load_pkgmap(const std::string& config_dir);
void set_pkgmap(Json map);  // tests
std::optional<std::string> resolve_pkg(const std::string& logical, const Distro& d);
std::optional<std::string> flatpak_id(const std::string& logical);

std::string pkg_install(const std::vector<std::string>& logical, const Distro& d);
std::string pkg_remove(const std::vector<std::string>& logical, const Distro& d);
std::string pkg_check(const std::vector<std::string>& logical, const Distro& d);
std::string pkg_update(const Distro& d);
// action: add | del | check | live. `kargs_tool` is the command that runs the
// boot-arg editor on non-Fedora systems (e.g. "/opt/tuxthrottle/ttctl kargs").
std::string kargs_cmd(const std::string& action, const std::string& args, const Distro& d,
                      const std::string& kargs_tool);

std::string expand(const std::string& cmd, const Distro& d, const std::string& kargs_tool = "ttctl kargs");

bool needs_fedora(const std::vector<std::string>& cmds);
bool portable(const std::vector<std::string>& cmds, const Distro& d);

}  // namespace tt
