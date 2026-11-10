#include "gpudrivers.hpp"

#include <sys/utsname.h>
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <set>

#include "safety.hpp"
#include "userenv.hpp"
#include "util.hpp"

namespace tt::gpudrivers {

namespace fs = std::filesystem;
static const char* kState = "/var/lib/tuxthrottle/gpudrivers-last.json";

std::string nvidia_generation(const std::string& device_id) {
    char* end = nullptr;
    unsigned long n = std::strtoul(device_id.c_str(), &end, 16);
    if (end == device_id.c_str() || *end) return "proprietary";
    if (n >= 0x1E00) return "open";
    if (n >= 0x1340) return "proprietary";
    if (n >= 0x0FC0) return "legacy";
    return "unsupported";
}

std::vector<Gpu> scan_gpus(const std::string& root) {
    std::vector<Gpu> out;
    std::error_code ec;
    if (!fs::is_directory(root, ec)) return out;
    std::vector<fs::path> devs;
    for (const auto& e : fs::directory_iterator(root, ec)) devs.push_back(e.path());
    std::sort(devs.begin(), devs.end());
    for (const auto& dev : devs) {
        auto cls = read_file((dev / "class").string());
        if (!cls || !starts_with(*cls, "0x03")) continue;
        std::string vid = read_trim((dev / "vendor").string()), did = read_trim((dev / "device").string());
        if (vid.empty() || did.empty()) continue;
        Gpu g;
        g.slot = dev.filename().string();
        g.vendor = vid == "0x10de" ? "nvidia" : vid == "0x1002" ? "amd" : vid == "0x8086" ? "intel" : "other";
        g.device_id = did;
        std::string drv = (dev / "driver").string();
        g.driver = path_exists(drv) ? base_name(real_path(drv)) : "";
        g.boot_vga = read_trim((dev / "boot_vga").string()) == "1";
        out.push_back(g);
    }
    for (auto& g : out) {
        bool others = false;
        for (const auto& o : out) if (&o != &g && !o.boot_vga) others = true;
        g.kind = (g.vendor == "intel" || (g.vendor == "amd" && g.boot_vga && others)) ? "integrated" : "discrete";
        if (g.vendor == "nvidia") g.generation = nvidia_generation(g.device_id);
    }
    return out;
}

std::string layout(const std::vector<Gpu>& gpus) {
    if (gpus.empty()) return "none";
    std::vector<std::string> vs;
    for (const auto& g : gpus) vs.push_back(g.vendor);
    std::set<std::string> uniq(vs.begin(), vs.end());
    if (gpus.size() == 1) return vs[0] + "-only";
    if (uniq.count("nvidia") && uniq.size() > 1) {
        std::string other;
        for (const auto& v : vs) if (v != "nvidia") { other = v; break; }
        return "hybrid-" + other + "+nvidia";
    }
    if (uniq.size() == 1) return "multi-" + vs[0];
    std::vector<std::string> u(uniq.begin(), uniq.end());
    return "hybrid-" + join(u, "+");
}

static int secure_boot_detect() {
    for (const auto& p : glob_paths("/sys/firmware/efi/efivars/SecureBoot-*")) {
        std::string b = read_file(p).value_or("");
        return !b.empty() && b.back() == '\x01' ? 1 : 0;
    }
    return -1;
}

static std::string raw_install(const Distro& d, const std::vector<std::string>& pkgs) {
    std::string q;
    for (const auto& p : pkgs) q += (q.empty() ? "" : " ") + shell_quote(p);
    if (d.pkg == "dnf") return "dnf install -y " + q;
    if (d.pkg == "rpm-ostree") return "rpm-ostree install --idempotent --allow-inactive " + q;
    if (d.pkg == "apt") return "DEBIAN_FRONTEND=noninteractive apt-get install -y " + q;
    if (d.pkg == "pacman") return "pacman -S --needed --noconfirm " + q;
    if (d.pkg == "emerge") return "emerge --noreplace " + q;
    if (d.pkg == "zypper") return "zypper --non-interactive install " + q;
    return "false";
}

static std::string raw_remove(const Distro& d, const std::vector<std::string>& pkgs) {
    std::string q;
    for (const auto& p : pkgs) q += (q.empty() ? "" : " ") + shell_quote(p);
    if (d.pkg == "dnf") return "dnf remove -y " + q;
    if (d.pkg == "rpm-ostree") return "rpm-ostree uninstall " + q;
    if (d.pkg == "apt") return "DEBIAN_FRONTEND=noninteractive apt-get remove -y " + q;
    if (d.pkg == "pacman") return "pacman -Rs --noconfirm " + q;
    if (d.pkg == "emerge") return "emerge --deselect " + q;
    if (d.pkg == "zypper") return "zypper --non-interactive remove " + q;
    return "false";
}

static std::string headers_pkg_arch() {
    struct utsname u;
    std::string rel = uname(&u) == 0 ? u.release : "";
    for (auto [tag, kern] : {std::pair<const char*, const char*>{"zen", "linux-zen"}, {"lts", "linux-lts"}, {"hardened", "linux-hardened"}, {"cachyos", "linux-cachyos"}})
        if (rel.find(tag) != std::string::npos) return std::string(kern) + "-headers";
    return "linux-headers";
}

static std::vector<Step> mesa_steps(const Distro& d, const std::vector<Gpu>& gpus, bool want32) {
    bool amd = false, intel = false;
    for (const auto& g : gpus) { amd = amd || g.vendor == "amd"; intel = intel || g.vendor == "intel"; }
    std::vector<std::string> pk;
    const std::string& f = d.family;
    auto add = [&](std::initializer_list<const char*> l) { for (const char* x : l) pk.push_back(x); };
    if (f == "fedora") {
        add({"mesa-vulkan-drivers", "mesa-dri-drivers", "mesa-va-drivers", "vulkan-tools"});
        if (want32 && !d.atomic) add({"mesa-vulkan-drivers.i686", "mesa-dri-drivers.i686"});
        if (intel) add({"intel-media-driver"});
    } else if (f == "debian") {
        add({"mesa-vulkan-drivers", "libgl1-mesa-dri", "mesa-va-drivers", "vulkan-tools"});
        if (want32) add({"mesa-vulkan-drivers:i386", "libgl1-mesa-dri:i386"});
        if (intel) add({"intel-media-va-driver"});
    } else if (f == "arch") {
        add({"mesa", "libva-mesa-driver", "vulkan-tools"});
        if (amd) add({"vulkan-radeon"});
        if (intel) add({"vulkan-intel", "intel-media-driver"});
        if (want32) { add({"lib32-mesa"}); if (amd) add({"lib32-vulkan-radeon"}); if (intel) add({"lib32-vulkan-intel"}); }
    } else if (f == "gentoo") add({"media-libs/mesa", "media-libs/vulkan-loader", "dev-util/vulkan-tools"});
    else { pk = {"Mesa-dri", amd ? "libvulkan_radeon" : "libvulkan_intel", "vulkan-tools"}; }
    std::vector<std::string> cmds;
    if (f == "debian" && want32) cmds.push_back("dpkg --add-architecture i386 && apt-get update");
    cmds.push_back(raw_install(d, pk));
    return {Step{"Mesa / Vulkan / VA-API for AMD and Intel", cmds, {}, false, "Shared graphics libraries; rollback leaves them in place."}};
}

static std::vector<Step> nvidia_steps(const Distro& d, const Gpu& gpu, bool want32, std::vector<std::string>& warnings) {
    const std::string& f = d.family;
    bool open_mod = gpu.generation == "open";
    std::vector<Step> steps;
    if (f == "fedora") {
        std::vector<std::string> pk{"akmod-nvidia", "xorg-x11-drv-nvidia-cuda"};
        if (d.id != "nobara" && !d.atomic)
            steps.push_back({"Enable RPM Fusion (free + nonfree)",
                             {"dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm "
                              "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm"},
                             {"dnf remove -y rpmfusion-free-release rpmfusion-nonfree-release"}, false, "Skipped automatically if already enabled (dnf is idempotent)."});
        if (want32 && !d.atomic) pk.push_back("xorg-x11-drv-nvidia-libs.i686");
        steps.push_back({"Install the NVIDIA driver (akmod)", {raw_install(d, pk)}, {raw_remove(d, pk)}, false,
                         "Reboot needed; the kernel module builds in the background first (wait ~5 min after install before rebooting)."});
        if (!d.atomic) steps.push_back({"Build the kernel module now", {"akmods --force", "dracut --force"}, {}, false, "Avoids booting before the akmod finished building."});
        else warnings.push_back("Atomic Fedora: RPM Fusion must already be layered (rpm-ostree install rpmfusion-*-release). Bazzite/Bluefin ship NVIDIA images - rebase instead of layering.");
    } else if (f == "debian") {
        bool ubuntu = d.id == "ubuntu" || std::find(d.like.begin(), d.like.end(), "ubuntu") != d.like.end();
        if (ubuntu) {
            steps.push_back({"Install the recommended NVIDIA driver", {"ubuntu-drivers install"}, {"apt-get remove -y --purge '^nvidia-.*' && apt-get autoremove -y"}, false,
                             "ubuntu-drivers picks the branch and signs DKMS for Secure Boot."});
        } else {
            steps.push_back({"Enable contrib / non-free / non-free-firmware",
                             {R"(sed -i.tuxthrottle-bak -E '/^(deb|Components)/ s/\bmain\b( contrib)?( non-free)?( non-free-firmware)?/main contrib non-free non-free-firmware/' /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true)",
                              "apt-get update"},
                             {"for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do [ -f $f.tuxthrottle-bak ] && mv -f $f.tuxthrottle-bak $f; done; apt-get update"}, false, ""});
            std::vector<std::string> pk{"nvidia-driver", "firmware-misc-nonfree", "linux-headers-amd64"};
            if (want32) pk.push_back("nvidia-driver-libs:i386");
            std::vector<std::string> cmds;
            if (want32) cmds.push_back("dpkg --add-architecture i386 && apt-get update");
            cmds.push_back(raw_install(d, pk));
            steps.push_back({"Install the NVIDIA driver (DKMS)", cmds, {raw_remove(d, pk)}, false, ""});
        }
    } else if (f == "arch") {
        std::vector<std::string> pk{open_mod ? "nvidia-open-dkms" : "nvidia-dkms", headers_pkg_arch(), "nvidia-utils", "nvidia-settings"};
        if (want32) { pk.push_back("lib32-nvidia-utils"); warnings.push_back("32-bit packages need the [multilib] repo enabled in /etc/pacman.conf."); }
        steps.push_back({"Install the NVIDIA driver (DKMS)", {raw_install(d, pk)}, {raw_remove(d, pk)}, false, ""});
    } else if (f == "gentoo") {
        steps.push_back({"Install x11-drivers/nvidia-drivers", {"emerge --noreplace x11-drivers/nvidia-drivers"}, {"emerge --deselect x11-drivers/nvidia-drivers"}, false,
                         std::string("Needs a kernel with CONFIG_MODULES and your /usr/src/linux symlink set; ") + (open_mod ? "add USE=kernel-open for this Turing+ GPU." : "")});
    } else if (f == "suse") {
        steps.push_back({"Add the NVIDIA repo and install the driver",
                         {"zypper --non-interactive addrepo --refresh https://download.nvidia.com/opensuse/tumbleweed NVIDIA || true",
                          "zypper --non-interactive --gpg-auto-import-keys refresh", "zypper --non-interactive install-new-recommends --repo NVIDIA"},
                         {"zypper --non-interactive remove -u 'nvidia-*' 'x11-video-nvidiaG0*' || true"}, false, "Experimental: targets Tumbleweed. On Leap use the matching NVIDIA repo URL."});
        warnings.push_back("openSUSE support is experimental and untested.");
    } else warnings.push_back("Unknown distro family - no NVIDIA install plan.");
    return steps;
}

static std::vector<Step> nvidia_extras(const Distro& d, bool hybrid, bool want_power, bool sb) {
    std::vector<Step> steps;
    if (d.family != "gentoo") {
        std::string kargs = "nvidia-drm.modeset=1 nvidia-drm.fbdev=1";
        steps.push_back({"Enable NVIDIA DRM modesetting (Wayland, PRIME sync)", {kargs_cmd("add", kargs, d, "tuxthrottlectl kargs")}, {kargs_cmd("del", kargs, d, "tuxthrottlectl kargs")}, false, ""});
    }
    steps.push_back({"Blacklist nouveau",
                     {R"(printf '%s\n' 'blacklist nouveau' 'options nouveau modeset=0' > /etc/modprobe.d/tuxthrottle-blacklist-nouveau.conf)"},
                     {"rm -f /etc/modprobe.d/tuxthrottle-blacklist-nouveau.conf"}, false, "Initramfs regeneration happens in the next step."});
    std::string svc = "nvidia-suspend.service nvidia-resume.service nvidia-hibernate.service";
    steps.push_back({"Keep VRAM across suspend (laptops/desktops)",
                     {R"(printf '%s\n' 'options nvidia NVreg_PreserveVideoMemoryAllocations=1' > /etc/modprobe.d/tuxthrottle-nvidia-suspend.conf)", "systemctl enable " + svc + " 2>/dev/null || true"},
                     {"rm -f /etc/modprobe.d/tuxthrottle-nvidia-suspend.conf", "systemctl disable " + svc + " 2>/dev/null || true"}, false, ""});
    if (hybrid && want_power)
        steps.push_back({"Let the dGPU power down when idle (hybrid laptop)",
                         {R"(printf '%s\n' 'options nvidia NVreg_DynamicPowerManagement=0x02' > /etc/modprobe.d/tuxthrottle-nvidia-runtimepm.conf)"},
                         {"rm -f /etc/modprobe.d/tuxthrottle-nvidia-runtimepm.conf"}, false, "Runtime D3 power management; needs Turing+ and a PCIe-capable laptop."});
    std::string regen = d.family == "fedora" || d.family == "gentoo" || d.family == "suse" ? "dracut --force" : d.family == "debian" ? "update-initramfs -u -k all" : d.family == "arch" ? "mkinitcpio -P" : "true";
    if (d.atomic) regen = "rpm-ostree initramfs --enable || true";
    steps.push_back({"Rebuild the initramfs", {regen}, {}, false, ""});
    if (hybrid)
        steps.push_back({"Add the 'run on discrete GPU' helper",
                         {raw_install(d, {resolve_pkg("switcheroo-control", d).value_or("switcheroo-control")}), "systemctl enable --now switcheroo-control.service 2>/dev/null || true"},
                         {"systemctl disable --now switcheroo-control.service 2>/dev/null || true"}, false,
                         "Gives KDE/GNOME the 'Launch using Discrete Graphics Card' entry. Per game: __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia."});
    if (sb) {
        std::string note = d.family == "fedora" ? "Run: sudo kmodgenca -a && sudo mokutil --import /etc/pki/akmods/certs/public_key.der - then reboot and choose 'Enroll MOK'."
                         : d.family == "debian" ? "Run: sudo mokutil --import /var/lib/dkms/mok.pub - then reboot and choose 'Enroll MOK' (Ubuntu's installer usually prompts for this)."
                         : d.family == "arch" ? "Sign the module (sbctl or a MOK key via dkms) or disable Secure Boot; then mokutil --import <key.der>, reboot, 'Enroll MOK'."
                                              : "Enroll a Machine Owner Key for the module, or disable Secure Boot.";
        steps.push_back({"Secure Boot: enroll the module-signing key", {}, {}, true, note});
    }
    return steps;
}

Plan build_plan(const std::vector<Gpu>& gpus, const Distro& d, bool want32, bool want_power, int sb, bool force) {
    std::string lay = layout(gpus);
    Plan p;
    p.distro = d.name;
    p.layout = lay;
    p.gpus = gpus;
    const Gpu* nv = nullptr;
    for (const auto& g : gpus) if (g.vendor == "nvidia") { nv = &g; break; }
    bool hybrid = starts_with(lay, "hybrid");
    if (gpus.empty()) p.warnings.push_back("No GPU found on the PCI bus (VM or container?).");
    if (d.family == "unknown") { p.warnings.push_back("Unsupported distro (" + d.name + "): no plan generated."); return p; }
    if (nv && (nv->generation == "legacy" || nv->generation == "unsupported")) {
        p.warnings.push_back("NVIDIA " + nv->device_id + " is too old for the current driver branches (Kepler needs the 470 legacy driver, older needs nouveau). Not installing; the open-source nouveau driver stays in use.");
        nv = nullptr;
    }
    if (nv && nv->driver == "nvidia" && !force) {
        p.warnings.push_back("The NVIDIA driver is already loaded - skipping its install (use --force to plan it anyway).");
        nv = nullptr;
    }
    bool any_mesa = false;
    for (const auto& g : gpus) any_mesa = any_mesa || g.vendor == "amd" || g.vendor == "intel";
    if (any_mesa) for (auto& s : mesa_steps(d, gpus, want32)) p.steps.push_back(s);
    if (nv) {
        for (auto& s : nvidia_steps(d, *nv, want32, p.warnings)) p.steps.push_back(s);
        bool sbv = sb < 0 ? secure_boot_detect() == 1 : sb == 1;
        for (auto& s : nvidia_extras(d, hybrid, want_power, sbv)) p.steps.push_back(s);
    }
    p.reboot = nv != nullptr || d.atomic;
    return p;
}

std::string render(const Plan& plan) {
    std::vector<std::string> lines{"Distro: " + plan.distro, "Layout: " + plan.layout};
    for (const auto& g : plan.gpus)
        lines.push_back("  GPU " + g.slot + " " + g.vendor + " " + g.device_id + " " + g.kind + (g.generation.empty() ? "" : " [" + g.generation + "]") + " driver=" + (g.driver.empty() ? "-" : g.driver));
    for (const auto& w : plan.warnings) lines.push_back("WARNING: " + w);
    int i = 0;
    for (const auto& s : plan.steps) {
        lines.push_back("\n" + std::to_string(++i) + ". " + s.title + (s.manual ? "  [MANUAL]" : ""));
        for (const auto& c : s.cmds) lines.push_back("   $ " + c);
        if (!s.note.empty()) lines.push_back("   note: " + s.note);
    }
    if (plan.steps.empty()) lines.push_back("\nNothing to install.");
    else if (plan.reboot) lines.push_back("\nReboot required after applying.");
    return join(lines, "\n");
}

Json plan_json(const Plan& plan) {
    Json o = Json::obj();
    o.set("distro", plan.distro);
    o.set("layout", plan.layout);
    Json gp = Json::arr();
    for (const auto& g : plan.gpus) {
        Json x = Json::obj();
        x.set("slot", g.slot); x.set("vendor", g.vendor); x.set("device_id", g.device_id); x.set("driver", g.driver);
        x.set("boot_vga", g.boot_vga); x.set("kind", g.kind); x.set("generation", g.generation);
        gp.push(x);
    }
    o.set("gpus", gp);
    Json st = Json::arr();
    for (const auto& s : plan.steps) {
        Json x = Json::obj();
        x.set("title", s.title);
        Json c = Json::arr(), u = Json::arr();
        for (const auto& a : s.cmds) c.push(a);
        for (const auto& a : s.undo) u.push(a);
        x.set("cmds", c); x.set("undo", u); x.set("manual", s.manual); x.set("note", s.note);
        st.push(x);
    }
    o.set("steps", st);
    Json w = Json::arr();
    for (const auto& s : plan.warnings) w.push(s);
    o.set("warnings", w);
    o.set("reboot", plan.reboot);
    return o;
}

int apply(const Plan& plan) {
    if (!is_root()) { std::puts("apply needs root (use sudo / pkexec)."); return 1; }
    if (plan.steps.empty()) { std::puts("Nothing to do."); return 0; }
    auto snap = btrfs::create_snapshot("pre-gpu-drivers");
    std::printf("snapshot: %s\n", snap.msg.c_str());
    mkdir_p(dir_name(kState));
    Json done = Json::arr();
    auto save = [&] {
        Json o = Json::obj();
        o.set("distro", plan.distro);
        o.set("steps", done);
        write_file(kState, o.dump(2));
    };
    save();
    int i = 0;
    for (const auto& s : plan.steps) {
        ++i;
        std::printf("\n[%d/%zu] %s\n", i, plan.steps.size(), s.title.c_str());
        std::fflush(stdout);
        if (s.manual) { std::printf("MANUAL STEP: %s\n", s.note.c_str()); continue; }
        for (const auto& c : s.cmds) {
            std::printf("$ %s\n", c.c_str());
            std::fflush(stdout);
            int rc = run_shell_stream(c, [](const std::string& l) { std::puts(l.c_str()); std::fflush(stdout); });
            if (rc != 0) { std::printf("FAILED: %s\nStopped. Run 'rollback --yes' to undo what was applied.\n", c.c_str()); return 1; }
        }
        Json x = Json::obj();
        x.set("title", s.title);
        Json u = Json::arr();
        for (const auto& a : s.undo) u.push(a);
        x.set("undo", u);
        done.push(x);
        save();
    }
    std::printf("\nDone.%s\n", plan.reboot ? " Reboot to load the new driver." : "");
    return 0;
}

int rollback() {
    if (!is_root()) { std::puts("rollback needs root."); return 1; }
    Json saved = read_json_file(kState);
    if (saved.object().empty()) { std::puts("No saved install to roll back."); return 1; }
    auto steps = saved["steps"].array();
    for (auto it = steps.rbegin(); it != steps.rend(); ++it)
        for (const auto& c : (*it)["undo"].array()) {
            std::printf("$ %s\n", c.str().c_str());
            std::fflush(stdout);
            run_shell_stream(c.str(), [](const std::string& l) { std::puts(l.c_str()); });
        }
    ::unlink(kState);
    std::puts("Rolled back. Reboot, then verify the display stack.");
    return 0;
}

int main(const std::vector<std::string>& a) {
    if (a.empty() || (a[0] != "detect" && a[0] != "plan" && a[0] != "apply" && a[0] != "rollback")) {
        std::puts("usage: gpu-drivers {detect|plan|apply --yes|rollback --yes} [--json] [--no-32bit] [--no-power] [--force]");
        return 2;
    }
    auto has = [&](const char* f) { return std::find(a.begin(), a.end(), f) != a.end(); };
    const std::string& cmd = a[0];
    if (cmd == "detect") {
        auto gpus = scan_gpus();
        if (has("--json")) { Plan p; p.gpus = gpus; std::puts(plan_json(p)["gpus"].dump(2).c_str()); return 0; }
        for (const auto& g : gpus) std::printf("%s %s %s %s driver=%s\n", g.slot.c_str(), g.vendor.c_str(), g.device_id.c_str(), g.kind.c_str(), g.driver.empty() ? "-" : g.driver.c_str());
        std::printf("layout: %s\n", layout(gpus).c_str());
        return 0;
    }
    if (cmd == "rollback") {
        if (!has("--yes")) { std::puts("Add --yes to run the saved undo commands."); return 2; }
        return rollback();
    }
    Plan plan = build_plan(scan_gpus(), current_distro(), !has("--no-32bit"), !has("--no-power"), -1, has("--force"));
    if (cmd == "plan") { std::puts(has("--json") ? plan_json(plan).dump(2).c_str() : render(plan).c_str()); return 0; }
    if (!has("--yes")) { std::puts(render(plan).c_str()); std::puts("\nDry run only. Add --yes to apply."); return 2; }
    return apply(plan);
}

}  // namespace tt::gpudrivers
