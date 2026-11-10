#include <memory>
#include <mutex>

#include "../drivehealth.hpp"
#include "../util.hpp"
#include "pages.hpp"

namespace tt::app {
using namespace ui;

namespace {
Tone level_tone(const std::string& l) { return l == "ok" ? Tone::Success : l == "warn" ? Tone::Warning : l == "bad" ? Tone::Danger : Tone::Muted; }
const char* level_mark(const std::string& l) { return l == "ok" ? "\xE2\x9C\x93" : l == "warn" ? "!" : l == "bad" ? "x" : "\xC2\xB7"; }

std::string size_str(double n) {
    double gib = n / (1 << 30);
    if (n >= 10.0 * (1 << 30)) return format_double(gib, 0) + " GiB";
    if (n >= (1 << 30)) return format_double(gib, 1) + " GiB";
    return std::to_string(static_cast<long long>(n) >> 20) + " MiB";
}

struct Drives : Page {
    Drives() { title = "Drives"; group = "System"; }
    std::shared_ptr<std::pair<std::mutex, Json>> res = std::make_shared<std::pair<std::mutex, Json>>();
    Json report;
    bool checked = false, checking = false;
    std::string ntfs_dev, ntfs_mount;

    void enter(App& a) override { if (!checked) check(a); }

    void check(App& a) {
        checking = true;
        checked = true;
        auto r = res;
        std::string user = a.user;
        a.job("drives-check").start_fn([r, user](const std::function<void(const std::string&)>&) {
            Json j;
            try { j = drivehealth::report(user); } catch (...) { j = Json::obj(); j.set("error", true); }
            std::lock_guard<std::mutex> l(r->first);
            r->second = j;
            return 0;
        });
    }

    void run(App& a, const std::string& desc, const std::string& args) {
        a.run_shell_task(desc, shell_quote(ctl_exe()) + " drives " + args);
        want_recheck = true;
    }
    bool want_recheck = false;

    void findings(App& a, const Json& list) {
        for (const auto& f : list.array()) a.ui.wrapped(std::string(level_mark(f["level"].str())) + "  " + f["text"].str(), level_tone(f["level"].str()));
    }

    void draw(App& a) override {
        Ui& ui = a.ui;
        a.page_title("Drives", "SMART, filesystems, this boot's kernel log and the Steam libraries. Checking only reads; the buttons are what change things.");
        Job& j = a.job("drives-check");
        if (checking && !j.busy()) { std::lock_guard<std::mutex> l(res->first); report = res->second; checking = false; }
        if (want_recheck && !a.busy()) { want_recheck = false; check(a); }

        const auto& plan = report["fix_plan"].array();
        int auto_n = 0;
        for (const auto& p : plan) if (p["action"].str() != "manual") ++auto_n;
        if (ui.button("Check drives", Btn::OutlineInfo, 0, !checking)) check(a);
        ui.tooltip("Run every check again. Read-only.");
        ui.same_line();
        if (ui.button(auto_n ? "Fix issues (" + std::to_string(auto_n) + ")" : "Fix issues", Btn::Success, 0, auto_n > 0 && !a.busy())) ui.open_popup("confirm:dh-fix");
        ui.tooltip("Run every safe automatic fix for all drives at once: scrub a Btrfs filesystem that needs it, repair a dirty NTFS volume, mend Steam's shadercache link, TRIM the SSDs. Asks first and lists exactly what it will do.");
        ui.same_line();
        if (ui.button("TRIM all SSDs", Btn::Outline, 0, !a.busy())) run(a, "Drives - TRIM", "trim");
        ui.tooltip("fstrim every mounted filesystem that supports it - tells the SSD which blocks are free. Safe; normally a weekly timer does it.");
        ui.same_line();
        if (checking) ui.label_c("checking...", Tone::Muted);
        else if (report["error"].as_bool()) ui.label_c("the check could not run", Tone::Danger);
        else if (!report.is_null() && !report.object().empty()) {
            int bad = static_cast<int>(report["bad"].as_number()), warn = static_cast<int>(report["warn"].as_number());
            ui.label_c(bad || warn ? std::to_string(bad) + " problem(s), " + std::to_string(warn) + " warning(s)" : "no problems found", bad ? Tone::Danger : warn ? Tone::Warning : Tone::Success);
        }
        ui.flow_reset();
        if (checking) ui.wake_after(200);

        if (a.confirm("dh-fix", "Fix issues on all drives", [&] {
                std::string msg = "This will:\n\n";
                std::string manual;
                for (const auto& p : plan) { if (p["action"].str() != "manual") msg += "  - " + p["text"].str() + "\n"; else manual += "  - " + p["text"].str() + "\n"; }
                if (!manual.empty()) msg += "\nNot fixable from here:\n\n" + manual;
                for (const auto& p : plan) if (p["action"].str() == "ntfs-repair") { msg += "\nAn NTFS repair unmounts that drive for a moment and is skipped if something is using it."; break; }
                return msg + "\nGo ahead?";
            }(), "Fix")) run(a, "Drives - fix issues on all drives", "fix-all --user " + shell_quote(a.user));

        if (report.object().empty() || report["error"].as_bool()) return;
        ui.spacing(ui.S(6));
        for (const auto& d : report["drives"].array()) {
            ui.push_id(d["dev"].str());
            ui.card_begin(d["dev"].str() + " - " + (d["model"].str().empty() ? "drive" : d["model"].str()));
            const Json& s = d["smart"];
            std::vector<std::string> bits{std::to_string(static_cast<long long>(d["size"].as_number()) >> 30) + " GiB"};
            std::string tran = d["tran"].str();
            for (auto& c : tran) c = static_cast<char>(std::toupper(c));
            if (!tran.empty()) bits.push_back(tran);
            bits.push_back(d["rotational"].as_bool() ? "HDD" : "SSD");
            struct F { const char* key; const char* suffix; };
            for (F f : {F{"temperature_c", " \xC2\xB0""C"}, F{"wear_percent", "% worn"}, F{"spare_percent", "% spare"}, F{"media_errors", " media errors"}, F{"power_on_hours", " h powered on"}})
                if (s[f.key].is_number()) bits.push_back(format_double(s[f.key].as_number(), 0) + f.suffix);
            ui.label(join(bits, "   \xC2\xB7   "));
            findings(a, d["findings"]);
            if (ui.button("Short self-test", Btn::Outline, 0, !a.busy())) run(a, "Drives - self-test " + d["dev"].str(), "selftest " + d["dev"].str());
            ui.tooltip("Start the drive's own short self-test (about two minutes, the drive stays usable). The result shows in the SMART data at the next check.");
            ui.card_end();
            ui.pop_id();
        }
        ui.card_begin("Filesystems");
        for (const auto& m : report["filesystems"].array()) {
            ui.push_id(m["mount"].str());
            ui.label_c(m["mount"].str(), Tone::Normal, Font::Bold);
            ui.same_line(ui.S(14));
            ui.label_c(m["fstype"].str() + " \xC2\xB7 " + m["dev"].str() + " \xC2\xB7 " + format_double(m["used_pct"].as_number(), 0) + "% used \xC2\xB7 " + size_str(m["free"].as_number()) + " free", Tone::Muted);
            if (m["fstype"].str() == "btrfs") {
                ui.same_line();
                if (ui.button("Start scrub", Btn::OutlineInfo, 0, !a.busy())) run(a, "Drives - scrub " + m["mount"].str(), "scrub " + shell_quote(m["mount"].str()));
                ui.tooltip("Read every block and verify its checksum, in the background while the system stays usable.");
            }
            std::string ft = m["fstype"].str();
            if (ft == "ntfs" || ft == "ntfs3" || ft == "fuseblk") {
                ui.same_line();
                if (ui.button("Repair NTFS", Btn::OutlineWarn, 0, !a.busy())) { ntfs_dev = m["dev"].str(); ntfs_mount = m["mount"].str(); ui.open_popup("confirm:dh-ntfs"); }
                ui.tooltip("Unmount, run ntfsfix (clears the dirty flag, fixes basic inconsistencies), mount again. Close Steam and anything using the drive first.");
            }
            findings(a, m["findings"]);
            ui.spacing(ui.S(4));
            ui.pop_id();
        }
        for (const auto& n : report["ntfs_unmounted"].array()) {
            ui.push_id(n["dev"].str());
            ui.label_c(n["dev"].str() + "  " + n["label"].str(), Tone::Normal, Font::Bold);
            ui.same_line(ui.S(14));
            ui.label_c("NTFS \xC2\xB7 not mounted", Tone::Muted);
            ui.same_line();
            if (ui.button("Repair NTFS", Btn::OutlineWarn, 0, !a.busy())) { ntfs_dev = n["dev"].str(); ntfs_mount.clear(); ui.open_popup("confirm:dh-ntfs"); }
            findings(a, n["findings"]);
            ui.pop_id();
        }
        ui.card_end();
        if (a.confirm("dh-ntfs", "Repair NTFS", "Run ntfsfix on " + ntfs_dev + "?\n\n" + (ntfs_mount.empty() ? "" : ntfs_mount + " is unmounted first and mounted again afterwards - close Steam, games and file managers using it, or the unmount is refused.\n\n") + "ntfsfix clears the dirty flag and fixes basic inconsistencies. It is not a full check: for real damage run chkdsk /f from Windows.", "Repair"))
            run(a, "Drives - repair NTFS " + ntfs_dev, "ntfs-repair " + shell_quote(ntfs_dev));

        ui.card_begin("Kernel log (this boot)");
        if (report["kernel_log"].array().empty()) ui.label("\xE2\x9C\x93  no I/O or filesystem errors logged since boot", Tone::Success);
        for (const auto& k : report["kernel_log"].array()) {
            ui.label(std::string(level_mark(k["level"].str())) + "  " + k["label"].str() + " - " + format_double(k["count"].as_number(), 0) + " line(s)", level_tone(k["level"].str()));
            for (const auto& ln : k["last"].array()) ui.label("      " + ln.str().substr(0, 170), Tone::Muted, ui.S(12), Font::Mono);
        }
        ui.card_end();
        if (!report["steam"].array().empty()) {
            ui.card_begin("Steam libraries");
            for (const auto& s : report["steam"].array()) { ui.label(s["path"].str(), Tone::Normal, 0, Font::Bold); findings(a, s["findings"]); }
            ui.card_end();
        }
    }
};
}  // namespace

std::unique_ptr<Page> make_drives() { return std::make_unique<Drives>(); }

}  // namespace tt::app
