#include "bootcfg.hpp"

#include <dirent.h>

#include <algorithm>

#include "kargs.hpp"
#include "util.hpp"

namespace tt::bootcfg {

static const char* kGrubDefault = "/etc/default/grub";
static const char* kKernelCmdline = "/etc/kernel/cmdline";
static const char* kEntryDirs[] = {"/boot/loader/entries", "/efi/loader/entries", "/boot/efi/loader/entries"};

static std::vector<std::string> entry_files() {
    std::vector<std::string> out;
    for (const char* dir : kEntryDirs) {
        DIR* d = opendir(dir);
        if (!d) continue;
        std::vector<std::string> names;
        while (dirent* e = readdir(d)) {
            std::string n = e->d_name;
            if (ends_with(n, ".conf") && n.find("fallback") == std::string::npos) names.push_back(n);
        }
        closedir(d);
        std::sort(names.begin(), names.end());
        for (auto& n : names) out.push_back(std::string(dir) + "/" + n);
    }
    return out;
}

std::string detect() {
    if (!entry_files().empty()) return "systemd-boot";
    if (path_exists(kKernelCmdline)) return "kernel-cmdline";
    if (path_exists(kGrubDefault)) return "grub";
    return "unknown";
}

// "options ..." line of a systemd-boot entry: returns {begin, end} of the whole line, npos if absent
static std::pair<size_t, size_t> options_line(const std::string& text) {
    size_t pos = 0;
    while (pos <= text.size()) {
        size_t eol = text.find('\n', pos);
        if (eol == std::string::npos) eol = text.size();
        if (text.compare(pos, 8, "options ") == 0 || text.compare(pos, 8, "options\t") == 0) return {pos, eol};
        pos = eol + 1;
    }
    return {std::string::npos, 0};
}

std::vector<std::string> staged_tokens() {
    std::string kind = detect();
    if (kind == "systemd-boot") {
        for (const auto& f : entry_files()) {
            auto t = read_file(f);
            if (!t) continue;
            auto [b, e] = options_line(*t);
            if (b != std::string::npos) return split_ws(t->substr(b + 8, e - b - 8));
        }
        return {};
    }
    if (kind == "kernel-cmdline") return split_ws(read_file(kKernelCmdline).value_or(""));
    if (kind == "grub") return kargs::grub_tokens(read_file(kGrubDefault).value_or(""));
    return {};
}

std::vector<std::string> live_tokens() { return split_ws(read_file("/proc/cmdline").value_or("")); }

static void backup(const std::string& path) {
    std::string bak = path + ".tuxthrottle-bak";
    if (!path_exists(bak)) run_shell("cp -p " + shell_quote(path) + " " + shell_quote(bak));
}

static bool save(const std::string& path, const std::string& data) {
    backup(path);
    std::string tmp = path + ".tuxthrottle-tmp";
    if (!write_file(tmp, data)) return false;
    run_shell("chmod --reference=" + shell_quote(path) + " " + shell_quote(tmp));
    return run_shell("mv -f " + shell_quote(tmp) + " " + shell_quote(path)).code == 0;
}

bool apply(const std::vector<std::string>& add, const std::vector<std::string>& del, std::string& message) {
    std::string kind = detect();
    if (kind == "systemd-boot") {
        for (const auto& f : entry_files()) {
            std::string text = read_file(f).value_or("");
            auto [b, e] = options_line(text);
            std::vector<std::string> cur = b == std::string::npos ? std::vector<std::string>{}
                                                                  : split_ws(text.substr(b + 8, e - b - 8));
            std::string line = "options " + join(kargs::edit_tokens(cur, add, del), " ");
            std::string out = b == std::string::npos ? text + (text.empty() || text.back() == '\n' ? "" : "\n") + line + "\n"
                                                     : text.substr(0, b) + line + text.substr(e);
            if (!save(f, out)) { message = "cannot write " + f; return false; }
        }
        return true;
    }
    if (kind == "kernel-cmdline") {
        auto toks = kargs::edit_tokens(split_ws(read_file(kKernelCmdline).value_or("")), add, del);
        if (!save(kKernelCmdline, join(toks, " ") + "\n")) { message = "cannot write /etc/kernel/cmdline"; return false; }
        message = "/etc/kernel/cmdline updated - reinstall your kernels/UKI (kernel-install, mkinitcpio -P or dracut)";
        return true;
    }
    if (kind == "grub") {
        std::string text = read_file(kGrubDefault).value_or("");
        if (!save(kGrubDefault, kargs::grub_edit(text, add, del))) { message = "cannot write /etc/default/grub"; return false; }
        const char* cmds[] = {"command -v update-grub >/dev/null 2>&1 && update-grub",
                              "[ -f /boot/grub2/grub.cfg ] && command -v grub2-mkconfig >/dev/null 2>&1 && grub2-mkconfig -o /boot/grub2/grub.cfg",
                              "[ -f /boot/grub/grub.cfg ] && command -v grub-mkconfig >/dev/null 2>&1 && grub-mkconfig -o /boot/grub/grub.cfg"};
        for (const char* c : cmds)
            if (run_shell(c).code == 0) return true;
        message = "grub config written but no update-grub/grub-mkconfig ran - regenerate it yourself";
        return false;
    }
    message = "no supported bootloader config found (GRUB, systemd-boot, /etc/kernel/cmdline)";
    return false;
}

}  // namespace tt::bootcfg
