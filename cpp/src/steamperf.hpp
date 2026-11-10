// Steam client "low-resource mode" and the iGPU pin for the Steam launcher (port of tuxthrottle_steamperf.py).
#pragma once
#include <string>
#include <utility>
#include <vector>

namespace tt::steamperf {

// Pure text transforms (exposed for tests).
std::string stamp(const std::string& text, const std::string& marker, const std::string& val = "true");
std::string patch_exec_lines(const std::string& text, const std::string& flags, int mem_high_mb, bool have_systemd_run);
std::string unpatch_exec_lines(const std::string& text);
std::string force_igpu_keys(const std::string& text);
std::string flip_block_keys(const std::string& txt, const std::string& block, const std::vector<std::string>& keys, const std::string& value,
                            bool insert_missing = true);

std::pair<bool, std::string> enable(bool autostart = true);
std::pair<bool, std::string> disable();
std::string status();                       // off | on | on+autostart
std::pair<bool, std::string> enable_igpu();
std::pair<bool, std::string> disable_igpu();
std::string status_igpu();                  // on | off
std::vector<std::pair<std::string, std::string>> diagnose(const std::string& user = "");   // {"ok"|"bad", message}
// Moves stray steam.desktop.* backups out of ~/.config/autostart; returns the moved file names.
std::vector<std::string> migrate_stray_bak();
std::vector<std::string> stray_autostart_files();
int main(const std::vector<std::string>& args);

}  // namespace tt::steamperf
