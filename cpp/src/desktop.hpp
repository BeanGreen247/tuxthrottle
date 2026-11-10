// Desktop integration helpers: KDE panel applet tweaks and the /etc/fstab auto-mount manager.
#pragma once
#include <string>
#include <vector>

namespace tt::kdepanel {
// args: {action, "on"|"off"}; returns an exit code (runs as the session user).
int main(const std::vector<std::string>& args);
}  // namespace tt::kdepanel

namespace tt::automount {
struct Candidate { std::string path, uuid, fstype, label, size, target; };
// Pure selection logic, parameterised for tests.
std::vector<Candidate> candidates_from(const std::string& lsblk_json, const std::string& fstab_text);
std::string fstab_line(const Candidate& c, int uid, int gid);
std::string drop_windows_names(const std::string& line);
int main(const std::vector<std::string>& args);  // list | enable-all | disable-all, with --user NAME
}  // namespace tt::automount
