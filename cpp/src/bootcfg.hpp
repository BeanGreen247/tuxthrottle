// Reads and edits the bootloader's kernel command line (GRUB, systemd-boot, /etc/kernel/cmdline).
#pragma once
#include <string>
#include <vector>

namespace tt::bootcfg {

std::string detect();                       // grub | systemd-boot | kernel-cmdline | unknown
std::vector<std::string> staged_tokens();   // what the bootloader will pass next boot
std::vector<std::string> live_tokens();     // /proc/cmdline
bool apply(const std::vector<std::string>& add, const std::vector<std::string>& del, std::string& message);

}  // namespace tt::bootcfg
