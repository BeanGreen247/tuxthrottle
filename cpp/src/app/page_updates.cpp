#include <cstdlib>

#include "../distro.hpp"
#include "../engine.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
struct Updates : Page {
    Updates() { title = "Updates"; group = "System"; }
    bool counted = false;

    void enter(App& a) override { if (!counted) { count(a); counted = true; } }

    void count(App& a) {
        a.job("updcount").start_fn([](const std::function<void(const std::string&)>& out) {
            std::vector<std::pair<std::string, std::string>> parts;
            auto sh = [](const std::string& cmd, int to) { auto r = run_argv({"bash", "-c", cmd}, to * 1000); return std::make_pair(r.code, r.out); };
            auto [rc, o] = sh("dnf -q --cacheonly check-update 2>/dev/null", 60);
            if (rc == 0 || rc == 100) {
                int n = 0;
                for (const auto& ln : split_lines(o)) {
                    auto w = split_ws(ln);
                    if (!ln.empty() && std::isalnum(static_cast<unsigned char>(ln[0])) && w.size() >= 3 && w[0].find('.') != std::string::npos) ++n;
                }
                parts.push_back({"dnf", std::to_string(n)});
            } else parts.push_back({"dnf", "?"});
            if (!which("flatpak").empty()) {
                auto [r2, o2] = sh("flatpak remote-ls --updates --columns=application 2>/dev/null", 45);
                int n = 0;
                for (const auto& ln : split_lines(o2)) if (!trim(ln).empty()) ++n;
                parts.push_back({"flatpak", r2 == 0 ? std::to_string(n) : "?"});
            }
            if (!which("fwupdmgr").empty()) {
                auto [r3, o3] = sh("fwupdmgr get-updates -y 2>/dev/null", 45);
                int n = 0;
                for (size_t p = o3.find("New version:"); p != std::string::npos; p = o3.find("New version:", p + 1)) ++n;
                parts.push_back({"firmware", r3 == 0 || r3 == 2 ? std::to_string(n) : "?"});
            }
            int total = 0;
            bool unknown = false;
            std::string detail;
            for (auto& [k, v] : parts) {
                if (v == "?") unknown = true; else total += std::atoi(v.c_str());
                detail += (detail.empty() ? "" : "  \xC2\xB7  ") + k + " " + v;
            }
            std::string age = engine::dnf_metadata_age();
            bool stale = engine::dnf_metadata_stale();
            std::string shown = stale && !unknown ? "~" + std::to_string(total) : std::to_string(total);
            std::string stamp;
            if (!age.empty()) stamp = std::string("   -   ") + (stale ? "(stale) " : "") + "dnf list " + age + (stale ? "; run \"Check for updates\" to refresh" : "");
            out("Updates available:  " + shown + "   (" + detail + ")" + stamp);
            out(stale ? "stale" : "fresh");
            return 0;
        });
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Updates");
        bool ns = !which("nobara-sync").empty(), fp = !which("flatpak").empty(), fw = !which("fwupdmgr").empty();
        const Distro& d = current_distro();
        std::string user = shell_quote(a.user);
        std::string sys_update = ns ? "nobara-sync cli " + user : expand("{PKG_UPDATE}", d);
        bool en = !a.busy();

        ui.card_begin("System updates");
        std::string mgrs = std::string(ns ? "dnf" : "dnf (no nobara-sync)") + (fp ? ", flatpak" : "") + (fw ? ", fwupd" : "");
        ui.wrapped("Package managers found: " + mgrs + ". Each has its own section below; \"Update everything\" runs the system + Flatpak updates back to back. Output streams to the log console at the bottom of the window; a reboot is recommended after a system or firmware update.", Tone::Muted);
        Job& j = a.job("updcount");
        auto lines = j.snapshot();
        if (j.busy()) { ui.label("Updates available:  checking...", Tone::Muted, 0, Font::Bold); ui.wake_after(150); }
        else if (!lines.empty()) ui.label(lines[0], lines.size() > 1 && lines[1] == "stale" ? Tone::Warning : Tone::Normal, 0, Font::Bold);
        ui.same_line();
        if (ui.button("recount", Btn::Ghost, 0, !j.busy())) count(a);
        ui.card_end();

        auto add = [&](const std::string& text, Btn st, const std::string& cmd, const std::string& desc, bool reboot = false) {
            if (ui.button_flow(text, st, en)) a.run_shell_task(desc, cmd, reboot);
        };
        auto row_end = [&] { ui.flow_reset(); };

        ui.card_begin("Everything", Tone::Success);
        std::string check_all = "echo '### dnf' ; " + std::string(ns ? "nobara-sync check-updates" : "dnf check-update") + " || true";
        if (fp) check_all += " ; echo '### flatpak' ; flatpak remote-ls --updates || true";
        if (fw) check_all += " ; echo '### fwupd' ; fwupdmgr get-updates || true";
        add("Check everything", Btn::OutlineInfo, check_all, "check all package managers");
        add("Update everything (system + Flatpak)", Btn::Success, sys_update + (fp ? " ; flatpak update -y" : ""), "update everything", true);
        row_end();
        ui.card_end();

        ui.card_begin(std::string("System - dnf") + (ns ? " / nobara-sync" : ""), Tone::Info);
        if (ns) {
            add("Check for updates", Btn::OutlineInfo, "nobara-sync check-updates || true", "check for updates");
            add("Update system", Btn::Success, "nobara-sync cli " + user, "update system + fixups", true);
            add("Apply known fixups", Btn::OutlineWarn, "nobara-sync install-fixups", "apply known fixups");
            add("Repair (distro-sync)", Btn::OutlineWarn, "nobara-sync repair", "repair via distro-sync", true);
            add("List enabled repos", Btn::Outline, "nobara-sync check-repos || true", "list enabled repos");
        } else add("Check for updates", Btn::OutlineInfo, "dnf check-update || true", "check for updates");
        add("dnf upgrade --refresh", Btn::Outline, "dnf upgrade --refresh -y", "dnf upgrade --refresh", true);
        add("Clean dnf cache", Btn::Outline, "dnf clean all && dnf makecache", "clean + rebuild dnf cache");
        add("Fix Fedora GPG keys", Btn::OutlineDanger, "rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-*-primary && dnf clean all && dnf makecache", "import Fedora GPG keys + rebuild cache");
        row_end();
        ui.card_end();

        if (fp) {
            ui.card_begin("Flatpak", Tone::Info);
            add("Check updates", Btn::OutlineInfo, "flatpak remote-ls --updates || true", "check Flatpak updates");
            add("Update Flatpaks", Btn::Success, "flatpak update -y", "update Flatpaks");
            add("Remove unused runtimes", Btn::Outline, "flatpak uninstall --unused -y", "remove unused Flatpak runtimes");
            row_end();
            ui.card_end();
        }
        if (fw) {
            ui.card_begin("Firmware - fwupd", Tone::Warning);
            add("Refresh metadata", Btn::OutlineInfo, "fwupdmgr refresh --force || true", "refresh firmware metadata");
            add("Show updates", Btn::OutlineInfo, "fwupdmgr get-updates || true", "list firmware updates");
            add("Apply firmware updates", Btn::OutlineDanger, "fwupdmgr update -y", "apply firmware updates", true);
            row_end();
            ui.card_end();
        }
        ui.wrapped("Tip: if an update aborts with a GPG signature error (Nobara ships some .fc44 packages signed with a key that's on disk but not imported), run \"Fix Fedora GPG keys\" then retry. Firmware updates can need the charger plugged in and a reboot to complete.", Tone::Muted);
    }
};
}  // namespace

std::unique_ptr<Page> make_updates() { return std::make_unique<Updates>(); }

}  // namespace tt::app
