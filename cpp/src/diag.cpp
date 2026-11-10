#include "diag.hpp"

#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <map>
#include <regex>
#include <set>

#include "diag_data.inc"
#include "distro.hpp"
#include "engine.hpp"
#include "devices.hpp"
#include "bootcfg.hpp"
#include "hwtools.hpp"
#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::diag {

// ---------------------------------------------------------------- redaction
std::string redact(const std::string& text_in, const std::string& user) {
    static const std::regex serial_line(R"(^(\s*(?:\S*/)?(?:product_serial|board_serial|chassis_serial|product_uuid|chassis_asset_tag|board_asset_tag)[:=\s]+).*$)",
                                        std::regex::icase | std::regex::multiline);
    static const std::regex dmi_serial(R"(^(\s*(?:Serial Number|UUID|Asset Tag):\s*).*$)", std::regex::icase | std::regex::multiline);
    static const std::regex mac(R"(\b([0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b)", std::regex::icase);
    static const std::regex uuid(R"(\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)", std::regex::icase);
    static const std::regex ipv4(R"(\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?\b)");
    std::string text = text_in;
    // std::regex has no multiline anchor support in all modes: process line by line for the anchored patterns
    std::string out;
    for (const auto& line : split_lines(text)) {
        std::string l = line;
        std::smatch m;
        if (std::regex_search(l, m, serial_line) && m.position() == 0) l = m[1].str() + "<redacted>";
        else if (std::regex_search(l, m, dmi_serial) && m.position() == 0) l = m[1].str() + "<redacted>";
        out += l + "\n";
    }
    if (!text.empty() && text.back() != '\n') out.pop_back();
    text = out;
    text = std::regex_replace(text, mac, "xx:xx:xx:xx:xx:xx");
    text = std::regex_replace(text, uuid, "<uuid>");
    text = regex_replace_fn(text, ipv4, [](const std::smatch& m) {
        std::string s = m.str();
        return starts_with(s, "127.") || starts_with(s, "0.") ? s : std::string("<ip>");
    });
    char host[256] = {0};
    if (gethostname(host, sizeof host - 1) == 0 && std::strlen(host) > 2) text = replace_all(text, host, "<host>");
    if (!user.empty() && user != "root" && user.size() > 2) {
        std::string res;
        size_t pos = 0;
        auto word = [](char c) { return std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-'; };
        while (true) {
            size_t f = text.find(user, pos);
            if (f == std::string::npos) { res += text.substr(pos); break; }
            bool pre = f == 0 || !word(text[f - 1]);
            size_t e = f + user.size();
            bool post = e >= text.size() || !word(text[e]);
            res += text.substr(pos, f - pos);
            res += pre && post ? "<user>" : user;
            pos = e;
        }
        text = res;
    }
    return text;
}

// ---------------------------------------------------------------- key caps
std::string decode_key_caps(const std::string& blob) {
    std::map<int, std::string> names;
    for (const auto& k : kKeyNames) names[k.code] = k.name;
    for (int i = 183; i < 195; ++i) names[i] = "F" + std::to_string(i - 170);
    std::string out, name = "?", keyline;
    auto flush = [&] {
        if (keyline.empty()) return;
        auto words = split_ws(keyline);
        std::vector<int> codes;
        int wi = 0;
        for (auto it = words.rbegin(); it != words.rend(); ++it, ++wi) {
            char* end = nullptr;
            unsigned long long val = std::strtoull(it->c_str(), &end, 16);
            if (end == it->c_str() || *end) continue;
            for (int bit = 0; bit < 64; ++bit) if ((val >> bit) & 1) codes.push_back(wi * 64 + bit);
        }
        std::sort(codes.begin(), codes.end());
        std::vector<std::string> pretty;
        for (int c : codes) if (c >= 55 || names.count(c)) pretty.push_back(std::to_string(c) + ":" + (names.count(c) ? names[c] : "KEY_" + std::to_string(c)));
        out += "[" + name + "]\n  " + (pretty.empty() ? "(only standard keys)" : join(pretty, ", ")) + "\n\n";
    };
    for (const auto& ln : split_lines(blob)) {
        if (starts_with(ln, "N: Name=")) {
            flush();
            size_t a = ln.find('"');
            size_t b = a == std::string::npos ? std::string::npos : ln.find('"', a + 1);
            name = a != std::string::npos && b != std::string::npos ? ln.substr(a + 1, b - a - 1) : ln.substr(8);
            keyline.clear();
        } else if (starts_with(ln, "B: KEY=")) keyline = trim(ln.substr(7));
    }
    flush();
    // python joins entries with "\n" after each "...\n" block, then returns that or a fallback
    if (out.empty()) return "(no KEY-capable devices found)";
    // each entry ended with a blank line; the reference joins entries with newline (no trailing blank)
    while (out.size() >= 2 && out.substr(out.size() - 2) == "\n\n") out.pop_back();
    return out;
}

// ---------------------------------------------------------------- built-in sections
static std::string fans_section() {
    std::vector<std::string> lines;
    std::string pp = sensors::get_platform_profile();
    std::vector<std::string> ch = sensors::platform_profile_choices();
    std::vector<std::string> q;
    for (const auto& c : ch) q.push_back("'" + c + "'");
    lines.push_back("platform_profile: " + pp + "  choices=[" + join(q, ", ") + "]");
    auto fans = sensors::read_fans();
    for (const auto& f : fans)
        lines.push_back("  " + f.label + ": " + std::to_string(f.rpm) + " rpm  (max " + std::to_string(f.max) + ", boost " + (f.boost ? std::to_string(*f.boost) : "None") + ")");
    if (fans.empty()) lines.push_back("  (no alienware_wmi / dell_smm fan interface found)");
    std::vector<std::string> pw;
    for (const auto& s : sensors::get_pwm_state())
        pw.push_back("(" + (s.enable ? std::to_string(*s.enable) : "None") + ", " + (s.pwm ? std::to_string(*s.pwm) : "None") + ")");
    lines.push_back("dell_smm pwm state (enable,value): [" + join(pw, ", ") + "]");
    lines.push_back(std::string("dGPU awake: ") + (sensors::dgpu_is_awake() ? "True" : "False"));
    return join(lines, "\n");
}

static std::string kbd_info_text() {
    try {
        Json inf = kbd::info();
        std::vector<std::string> l;
        for (const auto& kv : inf.object()) {
            std::string v = kv.second.is_string() ? kv.second.as_string() : (kv.second.is_bool() ? (kv.second.as_bool() ? "True" : "False") : kv.second.dump());
            char b[256];
            std::snprintf(b, sizeof b, "%-16s: %s", kv.first.c_str(), v.c_str());
            l.push_back(b);
        }
        return join(l, "\n");
    } catch (const std::exception& e) {
        return std::string("(error: ") + e.what() + ")";
    }
}

static std::string display_txt() {
    std::vector<std::string> out{"# kscreen-doctor"};
    for (const std::vector<std::string>& args : {std::vector<std::string>{"kscreen-doctor", "-o"}, std::vector<std::string>{"kscreen-doctor", "-j"}}) {
        out.push_back(sensors::session_ready() ? trim(run_argv_stdout(sensors::session_cmd(args), 6000).out) : "");
        out.push_back("");
    }
    out.push_back("# xrandr");
    auto xr = split_lines(run_argv_stdout(sensors::session_cmd({"xrandr", "--verbose"}), 6000).out);
    if (xr.size() > 120) xr.resize(120);
    out.push_back(join(xr, "\n"));
    out.push_back("");
    out.push_back("# drm modes");
    auto modes = glob_paths("/sys/class/drm/*/modes");
    std::sort(modes.begin(), modes.end());
    for (const auto& m : modes) { out.push_back(m + ":"); out.push_back(read_trim(m)); }
    out.push_back("");
    out.push_back("# vrr_capable");
    auto vrr = glob_paths("/sys/class/drm/*/vrr_capable");
    std::sort(vrr.begin(), vrr.end());
    for (const auto& m : vrr) out.push_back(m + ":" + read_trim(m));
    return join(out, "\n");
}

Json system_summary() {
    const Distro& d = current_distro();
    Json o = Json::obj();
    Json dj = Json::obj();
    dj.set("id", d.id);
    dj.set("name", d.name);
    dj.set("version", d.version);
    dj.set("family", d.family);
    dj.set("atomic", d.atomic);
    dj.set("pkg", d.pkg);
    Json like = Json::arr();
    for (const auto& l : d.like) like.push(l);
    dj.set("like", like);
    o.set("distro", dj);
    bool efi = path_exists("/sys/firmware/efi");
    std::string sb = "unknown";
    for (const auto& p : glob_paths("/sys/firmware/efi/efivars/SecureBoot-*")) {
        std::string b = read_file(p).value_or("");
        if (!b.empty()) sb = b.back() == '\x01' ? "enabled" : "disabled";
    }
    o.set("firmware", efi ? "uefi" : "bios");
    o.set("secure_boot", sb);
    o.set("bootloader", bootcfg::detect());
    Json staged = Json::arr();
    if (bootcfg::detect() != "unknown") for (const auto& t : bootcfg::staged_tokens()) staged.push(t);
    o.set("staged_kernel_args", staged);
    Json sess = Json::obj();
    for (const char* k : {"XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY", "DISPLAY"}) sess.set(k, env_or(k));
    o.set("session", sess);
    o.set("init", read_trim("/proc/1/comm").empty() ? "?" : read_trim("/proc/1/comm"));
    Json tools = Json::obj();
    for (const auto& t : split_ws(kBundleTools)) tools.set(t, which(t));
    o.set("tools", tools);
    return o;
}

Json capabilities() {
    auto items = engine::load_items();
    bool has_nv = sensors::has_nvidia_gpu(), has_amd = sensors::has_amd_gpu();
    Json out = Json::obj();
    int usable = 0;
    for (const auto& it : items) {
        std::vector<std::string> cmds = it.apply_cmds;
        cmds.insert(cmds.end(), it.undo_cmds.begin(), it.undo_cmds.end());
        Json reasons = Json::arr();
        if (!portable(cmds, current_distro())) reasons.push("distro");
        if (it.requires_vendor == "nvidia" && !has_nv) reasons.push("no-nvidia");
        if (it.requires_vendor == "amd" && !has_amd) reasons.push("no-amd");
        if (!sensors::model_allows(it.requires_models)) reasons.push("model");
        Json row = Json::obj();
        row.set("kind", it.kind);
        row.set("category", it.category);
        row.set("usable", reasons.array().empty());
        row.set("gated_by", reasons);
        usable += reasons.array().empty();
        out.set(it.id, row);
    }
    Json top = Json::obj();
    top.set("usable", usable);
    top.set("total", static_cast<double>(items.size()));
    top.set("items", out);
    return top;
}

// ---------------------------------------------------------------- reports
static std::string run_section(const char* cmd, int timeout_s) {
    auto r = run_argv({"timeout", "-k", "2", std::to_string(timeout_s), "bash", "-lc", cmd}, (timeout_s + 4) * 1000);
    return trim(r.out);
}

static std::string builtin(const std::string& name) {
    if (name == "@fans") return fans_section();
    if (name == "@key_caps") { auto r = run_shell("cat /proc/bus/input/devices"); return r.code == 0 ? decode_key_caps(r.out) : "(could not read /proc/bus/input/devices)"; }
    if (name == "@display") return display_txt();
    if (name == "@model_scaffold") {
        try { return modelgen::build_scaffold().dump(2); } catch (const std::exception& e) { return std::string("(model scaffold generation failed: ") + e.what() + ")"; }
    }
    if (name == "@system_summary") return system_summary().dump(2);
    if (name == "@capabilities") return capabilities().dump(2);
    return "(unknown builtin)";
}

std::string wrap_issue_block(const std::string& report) {
    std::string r = replace_all(report, "```", "``\xE2\x80\x8B`");
    while (!r.empty() && std::isspace(static_cast<unsigned char>(r.back()))) r.pop_back();
    return "<details><summary>debug report - TuxThrottle</summary>\n\n```\n" + r + "\n```\n\n</details>\n";
}

std::string issue_template() { return kIssueTemplate; }

std::string collect_debug_report(bool wrap) {
    std::vector<std::string> hdr;
    hdr.push_back("TuxThrottle - debug report");
    hdr.push_back("generated " + timestamp("%Y-%m-%d %H:%M:%S %Z") + "   toolkit " + engine::toolkit_version() + "   euid=" + std::to_string(geteuid()));
    hdr.push_back("REVIEW BEFORE PASTING - this contains your username, hostname and hardware IDs.");
    hdr.push_back(std::string(92, '='));
    hdr.push_back("");
    std::vector<std::string> body;
    for (const auto& c : kDebugCmds) {
        if (!c.cmd) { body.push_back("\n" + std::string(c.title)); continue; }
        std::string out;
        try { out = c.cmd[0] == '@' ? builtin(c.cmd) : run_section(c.cmd, 12); } catch (const std::exception& e) { out = std::string("(error: ") + e.what() + ")"; }
        out = trim(out);
        if (out.empty()) out = "(no output)";
        if (c.maxlines) {
            auto ls = split_lines(out);
            if (static_cast<int>(ls.size()) > c.maxlines) {
                std::string head = join(std::vector<std::string>(ls.begin(), ls.begin() + c.maxlines), "\n");
                out = head + "\n\xE2\x80\xA6 (" + std::to_string(ls.size() - static_cast<size_t>(c.maxlines)) + " more lines trimmed)";
            }
        }
        body.push_back("\n### " + std::string(c.title) + "\n" + out);
    }
    body.push_back("\n\n\xE2\x94\x80\xE2\x94\x80 TOOLKIT: KEYBOARD DRIVER \xE2\x94\x80\xE2\x94\x80");
    body.push_back("\n### tuxthrottle_kbd info\n" + kbd_info_text());
    body.push_back("\n\n\xE2\x94\x80\xE2\x94\x80 TOOLKIT: APPLY STATUS \xE2\x94\x80\xE2\x94\x80");
    auto items = engine::load_items();
    engine::evaluate_all(items);
    body.push_back("\n" + engine::format_status_report(items));
    body.push_back("\n\xE2\x94\x80\xE2\x94\x80 TOOLKIT: APPLY LEDGER (state.json) \xE2\x94\x80\xE2\x94\x80\n" + engine::ledger_load().dump(2, true));
    std::string report = join(hdr, "\n") + "\n" + join(body, "\n") + "\n";
    return wrap ? wrap_issue_block(report) : report;
}

std::string collect_hw_bundle(const std::string& dest_in, bool redact_data) {
    std::string prod = trim(read_file("/sys/class/dmi/id/product_name").value_or(""));
    if (prod.empty()) prod = "unknown";
    std::string slug = modelgen::slugify(prod);
    if (slug == "unknown-model") slug = "laptop";
    std::string stamp = timestamp("%Y%m%d-%H%M%S");
    std::string dirname = "tuxthrottle-hwdump-" + slug + "-" + stamp;
    std::string user = resolve_real_user();
    std::string dest = dest_in.empty() ? home_of(user) : dest_in;
    std::string work = dest + "/" + dirname;
    mkdir_p(work);
    static const std::set<std::string> no_redact = {"dsdt.b64", "model-scaffold.json", "system-summary.json", "tuxthrottle-capabilities.json"};
    std::vector<std::string> names;
    for (const auto& f : kBundleFiles) {
        std::string data;
        try { data = f.cmd[0] == '@' ? builtin(f.cmd) : run_section(f.cmd, 25); } catch (const std::exception& e) { data = std::string("(error: ") + e.what() + ")"; }
        while (!data.empty() && std::isspace(static_cast<unsigned char>(data.back()))) data.pop_back();
        if (data.empty()) data = "(no output)";
        data += "\n";
        if (redact_data && !no_redact.count(f.name)) data = redact(data, user);
        write_file(work + "/" + f.name, data);
        names.push_back(f.name);
    }
    std::string report = collect_debug_report();
    if (redact_data) report = redact(report, user);
    write_file(work + "/report.md", report);
    write_file(work + "/REDACTION.txt", redact_data ? "Redacted: serial numbers, UUIDs, asset tags, MAC addresses, non-loopback IPv4,\nthe hostname and the user name. Model ids (DMI product, PCI/USB ids) are kept.\n"
                                                    : "NOT redacted (collected with --no-redact).\n");
    std::string files;
    for (const auto& n : names) files += "  " + n + "\n";
    write_file(work + "/README-attach-this.txt",
               "TuxThrottle - hardware dump bundle\nmachine: " + prod + "   collected: " + stamp + "   euid=" + std::to_string(geteuid()) + "\n\n"
               "WHAT THIS IS\n  Raw sysfs / DMI / evdev / hwmon / PCI / OpenRGB / ACPI dumps + the\n  readable debug report, plus model-scaffold.json - an auto-generated\n"
               "  starting point for models/<slug>.json (probed fields filled, the\n  rest left under \"_todo\"). Together these are enough to add support\n"
               "  for this laptop: DMI strings to gate on, hwmon fan/pwm paths, the\n  platform_profile path + choices, evdev key codes for the Fn/media/\n"
               "  vendor keys, OpenRGB controller layout, battery method, GPU PCI ids,\n  panel modes, Dell/ASUS/Lenovo firmware tokens, and the decompilable\n  DSDT for reverse-engineering vendor WMI.\n\n"
               "HOW TO USE\n  1. Skim the files for anything private (hostname, serials in dmi.txt /\n     lsusb.txt / nvidia-smi). Redact if you care.\n"
               "  2. Open a 'new hardware support' issue and ATTACH this whole .tar.gz\n     (drag it onto the GitHub comment box).\n"
               "  3. Run this collector as root (sudo) if you can - dmidecode, the DSDT\n     and smbios-token-ctl need it. Re-run and re-attach if the first was\n     unprivileged.\n"
               "  4. If a Fn/media key doesn't work: run  sudo evtest  , pick the\n     keyboard / hotkey device, press the key, and paste those lines too.\n\n"
               "NEXT (maintainer): decode dsdt.b64 with  base64 -d dsdt.b64 > acpi.bin ;\n  iasl -d acpi.bin   ; finish model-scaffold.json's _todo fields; add\n"
               "  \"models\": [<slug>] gates to config/*.json entries that differ.\n\nFILES\n" + files + "  report.md\n  REDACTION.txt\n\n"
               "ONE-SHOT FIELDS (read these first)\n  system-summary.json  distro/family/atomic, UEFI, Secure Boot, bootloader, session, tools\n"
               "  tuxthrottle-capabilities.json  which tweaks/apps run, or are gated, on this machine\n  gpu-stack.txt  hybrid/MUX switchers, drivers installed, where PRIME lands\n"
               "  vendor-controls.txt  vendor WMI/EC modules + BIOS firmware-attributes\n");
    std::string tgz = dest + "/" + dirname + ".tar.gz";
    auto r = run_argv({"tar", "czf", tgz, "-C", dest, dirname}, 60000);
    run_argv({"rm", "-rf", work}, 10000);
    if (r.code != 0) throw std::runtime_error("tar failed: " + trim(r.out));
    if (is_root()) chown_to_user(tgz, user);
    return tgz;
}

int main(const std::vector<std::string>& a) {
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    if (has("--report")) {
        auto items = engine::load_items();
        engine::evaluate_all(items);
        std::puts(engine::format_status_report(items).c_str());
        if (!is_root()) std::puts("note: not running as root - privileged checks may read as 'Not applied'/'Check error'. Re-run with sudo for accuracy.");
        return 0;
    }
    if (has("--debug") || has("--diag")) {
        std::fputs(collect_debug_report().c_str(), stdout);
        if (!is_root()) std::fputs("\nnote: run with sudo for dmesg / RAPL / privileged checks.\n", stderr);
        return 0;
    }
    if (has("--collect") || has("--hw-bundle")) {
        std::string dest;
        for (const auto& x : a) if (!starts_with(x, "-")) { dest = x; break; }
        try {
            std::string path = collect_hw_bundle(dest, !has("--no-redact"));
            std::printf("hardware bundle written:\n  %s\nAttach it to a 'new hardware support' issue.\n", path.c_str());
            if (!is_root()) std::fputs("note: run with sudo for the full DSDT / dmesg / privileged dumps.\n", stderr);
            return 0;
        } catch (const std::exception& e) {
            std::fprintf(stderr, "collect failed: %s\n", e.what());
            return 1;
        }
    }
    std::puts("usage: diag {--report|--debug|--collect [dir] [--no-redact]}");
    return 2;
}

}  // namespace tt::diag
