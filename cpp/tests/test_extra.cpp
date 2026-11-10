// Extra unit tests: MangoHud string surgery, launcher parsing, formats (VDF/YAML/SQLite helpers), steam parsers,
// drive-health analysis, Steam client patching. Expected strings come from the Python reference implementation.
#include <cmath>
#include <cstdio>
#include <string>

#include "bench.hpp"
#include "compat.hpp"
#include "drivehealth.hpp"
#include <unistd.h>

#include "devices.hpp"
#include "engine.hpp"
#include "userenv.hpp"
#include "sensors.hpp"
#include "formats.hpp"
#include "ui/png.hpp"
#include "mhedit.hpp"
#include "gpudrivers.hpp"
#include "launchers.hpp"
#include "mangohud.hpp"
#include "steam.hpp"
#include "steamperf.hpp"
#include "util.hpp"

using namespace tt;

extern int g_fail, g_run;
#define CHECK(cond)                                                              \
    do {                                                                         \
        ++g_run;                                                                 \
        if (!(cond)) { ++g_fail; std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
    } while (0)
#define CHECK_EQ(a, b)                                                                                              \
    do {                                                                                                            \
        ++g_run;                                                                                                    \
        if (!((a) == (b))) { ++g_fail; std::printf("FAIL %s:%d  %s\n   got:  %s\n   want: %s\n", __FILE__, __LINE__, #a, std::string(a).c_str(), std::string(b).c_str()); } \
    } while (0)

static void test_mangohud() {
    using namespace mangohud;
    CHECK_EQ(transform("", true), std::string("mangohud %command%"));
    CHECK_EQ(transform("gamemoderun %command%", true), std::string("gamemoderun mangohud %command%"));
    CHECK_EQ(transform("mangohud %command%", false), std::string("%command%"));
    CHECK_EQ(transform("MANGOHUD=0 FOO=1 %command% -windowed", true, "/home/u/My Dir/x.conf"),
             std::string("MANGOHUD_CONFIGFILE=\"/home/u/My Dir/x.conf\" FOO=1 mangohud %command% -windowed"));
    CHECK_EQ(transform("gamescope -W 1920 -- %command%", true, "/a/b.conf"), std::string("gamescope -W 1920 -- env MANGOHUD_CONFIGFILE=/a/b.conf mangohud %command%"));
    CHECK_EQ(transform("gamescope -W 1920 -- env X=1 %command%", true, "/a/b.conf"), std::string("gamescope -W 1920 -- env MANGOHUD_CONFIGFILE=/a/b.conf X=1 mangohud %command%"));
    CHECK_EQ(transform("MANGOHUD_CONFIGFILE=\"/a b/c.conf\" mangohud %command%", false), std::string("%command%"));
    CHECK_EQ(transform("DXVK_HUD=1 mangohud MANGOHUD_CONFIGFILE=/x.conf %command%", true, "/y.conf"), std::string("MANGOHUD_CONFIGFILE=/y.conf DXVK_HUD=1 mangohud %command%"));

    const char* nv = "__NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only __GLX_VENDOR_LIBRARY_NAME=nvidia";
    CHECK_EQ(transform_dgpu("%command%", true, true), std::string(nv) + " %command%");
    CHECK_EQ(transform_dgpu("FOO=1 %command%", true, false), std::string("DRI_PRIME=1 FOO=1 %command%"));
    CHECK_EQ(transform_dgpu(std::string(nv) + " %command%", false, true), std::string(""));
    CHECK_EQ(transform_dgpu("gamescope -- %command%", true, true), std::string("gamescope -- env ") + nv + " %command%");
    CHECK_EQ(transform_dgpu("DRI_PRIME=1 mangohud %command%", false, false), std::string("mangohud %command%"));

    auto s1 = state("MANGOHUD_CONFIGFILE=\"/a b/c.conf\" mangohud %command%");
    CHECK(s1.enabled && s1.conf == "/a b/c.conf");
    auto s2 = state("x MANGOHUD_CONFIGFILE=/q.conf %command%");
    CHECK(!s2.enabled && s2.conf == "/q.conf");
    CHECK(has_dgpu("__NV_PRIME_RENDER_OFFLOAD=1 %command%") && !has_dgpu("FOO=1 %command%"));

    Json settings = Json::parse(R"({"position":"top-left","font_size":"20","fps_limit":"0","detail":"full"})");
    CHECK_EQ(render_conf("123", settings, "position=bottom\n# c\nfoo=1\nfont_size=10\nfull\n"),
             std::string("# MangoHud config for Steam AppID 123 (managed by TuxThrottle)\nfoo=1\nposition=top-left\nfont_size=20\nfull\n"));
    CHECK(std::string(kGateScript).find("tuxthrottle-mangohud-gate") != std::string::npos);
}

static void test_formats() {
    CHECK_EQ(base64_encode("hello world"), std::string("aGVsbG8gd29ybGQ="));
    CHECK_EQ(base64_decode("aGVsbG8gd29ybGQ="), std::string("hello world"));
    CHECK_EQ(base64_decode(base64_encode("a")), std::string("a"));
    bool threw = false;
    try { base64_decode("@@@"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);

    auto t = shlex_split("A=1 B='x y' \"c d\" e\\ f");
    CHECK(t.size() == 4 && t[1] == "B=x y" && t[2] == "c d" && t[3] == "e f");
    threw = false;
    try { shlex_split("'oops"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);

    Vdf v = Vdf::parse("\"Root\"\n{\n\t\"Software\"\n\t{\n\t\t\"Valve\"\n\t\t{\n\t\t\t\"apps\"\n\t\t\t{\n\t\t\t\t\"123\"\n\t\t\t\t{\n\t\t\t\t\t\"LaunchOptions\"\t\t\"a \\\"b\\\" c\"\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n");
    const Vdf* apps = v.find_ci("root")->find_ci("software")->find_ci("VALVE")->find_ci("apps");
    CHECK(apps && apps->find_ci("123")->find_ci("launchoptions")->str() == "a \"b\" c");
    Vdf again = Vdf::parse(v.dump());
    CHECK_EQ(again.dump(), v.dump());

    Yaml y = Yaml::parse("game:\n  exe: C:\\x.exe\n  prefix: /home/u/p\nsystem:\n  mangohud: true\n  env:\n    A: '1'\n    B: hello world\n  list:\n  - one\n  - two\n");
    CHECK(y.get("system")->get("mangohud")->is_true());
    CHECK_EQ(y.get("system")->get("env")->get("B")->str(), std::string("hello world"));
    CHECK(y.get("system")->get("list")->seq.size() == 2);
    y.get("system")->get("env")->set("C", Yaml::make_scalar("2", true));
    std::string out = y.dump();
    Yaml z = Yaml::parse(out);
    CHECK_EQ(z.get("system")->get("env")->get("C")->str(), std::string("2"));
    CHECK_EQ(z.get("system")->get("env")->get("A")->str(), std::string("1"));
    CHECK(out.find("C: '2'") != std::string::npos);
    CHECK(Yaml::parse("").is_null());
    CHECK(Yaml::parse("a: {}\nb: []\n").get("a")->is_map());
}

static void test_launchers() {
    auto p = launchers::parse_opts("__NV_PRIME_RENDER_OFFLOAD=1 FOO=bar gamemoderun mangohud taskset %command% ignored");
    CHECK(p.env.size() == 2 && p.env["FOO"] == "bar");
    CHECK(p.flags["useGameMode"] && p.flags["showMangohud"]);
    CHECK(p.wrappers.size() == 1 && p.wrappers[0] == "taskset");
    auto g = launchers::parse_opts("gamescope -W 1920 -- A=1 %command%");
    CHECK(g.env.size() == 1 && g.wrappers.empty());
    auto h = launchers::parse_opts("P=~/x/y %command%");
    CHECK(h.env["P"].find("/x/y") != std::string::npos);
    CHECK_EQ(launchers::steam_baseline({"a", "b", "a", "", "a", "b"}), std::string("a"));
    CHECK_EQ(launchers::steam_baseline({"", ""}), std::string(""));
    auto w = launchers::lutris_wanted_env("/pfx");
    CHECK(w.size() == 6 && w["DXVK_STATE_CACHE_PATH"] == "/pfx" && w["DXVK_HUD"] == "compiler");
}

static void test_steam() {
    auto libs = steam::parse_library_paths("\"libraryfolders\"{\"0\"{\"path\" \"/home/u/.local/share/Steam\"}\"1\"{\"path\"\t\"/mnt/Games/SteamLibrary\"}}");
    CHECK(libs.size() == 2 && libs[1] == "/mnt/Games/SteamLibrary");
    CHECK_EQ(steam::parse_appmanifest_name("\"AppState\"{\"appid\" \"10\" \"name\" \"Counter-Strike\"}"), std::string("Counter-Strike"));
    {
        const std::string y = "game:\n  exe: a.exe\n  prefix: /mnt/x/wow\n  working_dir: /mnt/x/wow\nscript:\n  game:\n    prefix: $GAMEDIR\n";
        CHECK_EQ(steam::lutris_yml_prefix(y), std::string("/mnt/x/wow"));
        CHECK_EQ(steam::lutris_yml_prefix("game:\n  prefix: \"/a b\"\n"), std::string("/a b"));
        CHECK_EQ(steam::lutris_yml_prefix("script:\n  game:\n    prefix: /nope\n"), std::string(""));
        CHECK(steam::lutris_yml_retarget(y, "/mnt/x/wow", "/home/u/Games/wow").find("/mnt/x/wow") == std::string::npos);
    }
    std::string vdf = "\"ShaderCacheManager\"\n\t{\n\t\t\"EnableShaderBackgroundProcessing\"\t\t\"1\"\n\t}\n";
    auto off = steam::set_bg_shader_key(vdf, false);
    CHECK(off && off->find("\"EnableShaderBackgroundProcessing\"\t\t\"0\"") != std::string::npos);
    auto on = steam::set_bg_shader_key(*off, true);
    CHECK(on && on->find("\"EnableShaderBackgroundProcessing\"\t\t\"1\"") != std::string::npos);
    auto added = steam::set_bg_shader_key("\"ShaderCacheManager\"\n{\n}\n", false);
    CHECK(added && added->find("\"EnableShaderBackgroundProcessing\"") != std::string::npos);
    CHECK(!steam::set_bg_shader_key("nothing here", false).has_value());
}

static void test_drivehealth() {
    using namespace drivehealth;
    Json bad = Json::parse(R"({"smart_status":{"passed":false},"temperature":{"current":80},
      "nvme_smart_health_information_log":{"critical_warning":4,"media_errors":3,"available_spare":5,"available_spare_threshold":10,
      "percentage_used":90,"unsafe_shutdowns":150}})");
    auto [summ, f] = smart_findings(bad);
    CHECK_EQ(worst(f), std::string("bad"));
    int bads = 0, warns = 0;
    for (const auto& x : f.array()) { bads += x["level"].str() == "bad"; warns += x["level"].str() == "warn"; }
    CHECK(bads >= 4 && warns >= 2);   // failed, critical warning, media errors, spare; wear + heat
    CHECK(summ["temperature_c"].as_number() == 80);
    Json ok = Json::parse(R"({"smart_status":{"passed":true},"temperature":{"current":40}})");
    auto [s2, f2] = smart_findings(ok);
    CHECK(f2.array().size() == 1 && f2.array()[0]["level"].str() == "ok");
    CHECK(usage_finding(50, 100LL << 30).is_null());
    CHECK_EQ(usage_finding(95, 20LL << 30)["level"].str(), std::string("warn"));
    CHECK_EQ(usage_finding(99, 2LL << 30)["level"].str(), std::string("bad"));
    CHECK(btrfs_stats_findings("[/dev/nvme0n1p3].write_io_errs 0\n[/dev/nvme0n1p3].corruption_errs 2\n").array().size() == 1);
    CHECK_EQ(btrfs_scrub_finding("Scrub started:    Mon\nStatus:           finished\nError summary:    no errors found")["level"].str(), std::string("ok"));
    CHECK_EQ(btrfs_scrub_finding("no stats available")["level"].str(), std::string("note"));
    CHECK_EQ(btrfs_scrub_finding("Status: finished\nError summary: csum=4")["level"].str(), std::string("bad"));
    Json log = scan_log("nvme nvme0: I/O 5 QID 1 timeout, reset controller\nok line\nBuffer I/O error on dev sda\nntfs3: volume is dirty\n");
    CHECK(log.array().size() == 3);
    CHECK(trend_finding(0, 5, 0, 1).is_null());
    CHECK_EQ(trend_finding(3, 0, 0, 2)["level"].str(), std::string("ok"));
    CHECK_EQ(trend_finding(3, 3, 0, 2)["level"].str(), std::string("warn"));
    CHECK_EQ(trend_finding(5, 2, 0, 2)["level"].str(), std::string("note"));
}

static void test_steamperf() {
    using namespace steamperf;
    std::string desk = "[Desktop Entry]\nName=Steam\nExec=/usr/bin/steam %U\nPrefersNonDefaultGPU=true\n[Desktop Action Store]\nExec=steam steam://store\n";
    std::string patched = patch_exec_lines(desk, "-silent -cef-disable-gpu", 1200, true);
    CHECK(patched.find("Exec=systemd-run --user --scope --quiet --collect -p MemoryHigh=1200M -- /usr/bin/steam -silent -cef-disable-gpu %U") != std::string::npos);
    CHECK(patched.find("Exec=steam steam://store") != std::string::npos);
    CHECK_EQ(patch_exec_lines(patched, "-silent -cef-disable-gpu", 1200, true), patched);     // idempotent
    CHECK_EQ(unpatch_exec_lines(patched), desk);
    CHECK(patch_exec_lines(desk, "-x", 1, false).find("Exec=/usr/bin/steam -x %U") != std::string::npos);
    std::string igpu = force_igpu_keys(desk);
    CHECK(igpu.find("PrefersNonDefaultGPU=false") != std::string::npos && igpu.find("X-KDE-RunOnDiscreteGpu=false") != std::string::npos);
    std::string st = stamp(desk, "X-TuxThrottle-LowResource", "true");
    CHECK(st.find("[Desktop Entry]\nX-TuxThrottle-LowResource=true\n") != std::string::npos);
    CHECK_EQ(stamp(st, "X-TuxThrottle-LowResource", "true"), st);
    std::string cfg = "\"friends\"\n\t{\n\t\t\"SignIntoFriends\"\t\t\"1\"\n\t}\n";
    CHECK(flip_block_keys(cfg, "friends", {"SignIntoFriends"}, "0").find("\"SignIntoFriends\"\t\t\"0\"") != std::string::npos);
    CHECK(flip_block_keys(cfg, "friends", {"AnimatedAvatars"}, "0").find("\"AnimatedAvatars\"\t\t\"0\"") != std::string::npos);
    CHECK(flip_block_keys(cfg, "friends", {"AnimatedAvatars"}, "0", false) == cfg);
}

static void test_compat() {
    using namespace compat;
    CHECK_EQ(sha512_hex("abc"), std::string("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"));
    CHECK_EQ(sha512_hex(""), std::string("cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"));
    CHECK(natural_less("GE-Proton9-2", "GE-Proton10-1"));
    CHECK(!natural_less("GE-Proton10-1", "GE-Proton9-2"));
    CHECK(natural_less("proton 8", "Proton 9"));
    std::string txt = "\"InstallConfigStore\"\n{\n\t\"Software\"\n\t{\n\t\t\"Valve\"\n\t\t{\n\t\t\t\"Steam\"\n\t\t\t{\n\t\t\t\t\"CompatToolMapping\"\n\t\t\t\t{\n"
                      "\t\t\t\t\t\"0\"\n\t\t\t\t\t{\n\t\t\t\t\t\t\"name\"\t\t\"proton_experimental\"\n\t\t\t\t\t\t\"config\"\t\t\"\"\n\t\t\t\t\t\t\"priority\"\t\t\"75\"\n\t\t\t\t\t}\n"
                      "\t\t\t\t\t\"12345\"\n\t\t\t\t\t{\n\t\t\t\t\t\t\"name\"\t\t\"proton_hotfix\"\n\t\t\t\t\t\t\"config\"\t\t\"\"\n\t\t\t\t\t\t\"priority\"\t\t\"250\"\n\t\t\t\t\t}\n"
                      "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n";
    auto m = read_mapping(txt);
    CHECK(m.size() == 2 && m["0"] == "proton_experimental" && m["12345"] == "proton_hotfix");
    std::string out = write_mapping(txt, {{"0", "GE-Proton9-1"}, {"999", "proton_9"}});
    auto m2 = read_mapping(out);
    CHECK(m2["0"] == "GE-Proton9-1" && m2["999"] == "proton_9" && m2["12345"] == "proton_hotfix");
    CHECK(out.find("\t\t\t\t\t\"999\"\n\t\t\t\t\t{\n\t\t\t\t\t\t\"name\"\t\t\"proton_9\"\n\t\t\t\t\t\t\"config\"\t\t\"\"\n\t\t\t\t\t\t\"priority\"\t\t\"250\"") != std::string::npos);
    bool threw = false;
    try { write_mapping("no block", {{"0", "x"}}); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
    CHECK_EQ(pick_arch(true), std::string("x86_64_v3"));

    Json assets = Json::parse(R"([
      {"name":"proton-cachyos-9-x86_64.tar.xz","browser_download_url":"u1","size":1},
      {"name":"proton-cachyos-9-x86_64_v3.tar.xz","browser_download_url":"u2","size":2},
      {"name":"proton-cachyos-9-arm64.tar.xz","browser_download_url":"u3","size":3},
      {"name":"proton-cachyos-9-x86_64_v3.sha512sum","browser_download_url":"s2","size":1}])");
    Json v3 = pick_assets("Proton-CachyOS", assets, "x86_64_v3");
    CHECK_EQ(v3["asset"].str(), std::string("proton-cachyos-9-x86_64_v3.tar.xz"));
    CHECK_EQ(v3["checksum_url"].str(), std::string("s2"));
    CHECK_EQ(pick_assets("Proton-CachyOS", assets, "x86_64")["asset"].str(), std::string("proton-cachyos-9-x86_64.tar.xz"));
    CHECK(pick_assets("GE-Proton", assets, "").is_null());   // wants .tar.gz
    CHECK(pick_assets("Nope", assets, "").is_null());
}

static void test_mhedit() {
    using namespace mhedit;
    Conf c = parse_conf("# hi\ncpu_text=Ryzen 7 5800H\ngpu_text=RTX 3050 Ti, Radeon Vega\nposition=top-right\noffset_x=12\nfont_size=22.0\ncpu_temp\ngpu_name=0\nfps=1\n");
    CHECK_EQ(c.cpu, std::string("Ryzen 7 5800H"));
    CHECK(c.gpus.size() == 2);
    CHECK_EQ(c.gpu, std::string("RTX 3050 Ti, Radeon Vega"));
    CHECK_EQ(c.position, std::string("top-right"));
    CHECK(c.offset_x == 12);
    CHECK(c.font_size == 22);
    CHECK(c.elements.count("cpu_temp") && c.elements.count("fps") && c.off.count("gpu_name"));
    CHECK_EQ(level_of(c.elements, "cpu"), std::string("full"));
    CHECK_EQ(level_of(c.elements, "gpu"), std::string("minimal"));
    Managed m;
    set(m, "fps", Val::remove());
    set(m, "position", Val::value("bottom-left"));
    set(m, "ram", Val::bare());
    set(m, "ram", Val::bare());
    std::string out = merge_conf("# hi\n\nfps=1\nposition=top-left # c\nposition=top-right\nfoo bar baz\ncpu_temp\n", m);
    CHECK_EQ(out, std::string("# hi\nposition=bottom-left\ncpu_temp\nram\n"));
    CHECK(!hud_width({"RTX", "Ryzen"}, false, 0).has_value());
    CHECK_EQ(*hud_width({"AMD Ryzen 7 5800H with Radeon"}, true, 20), std::string("733"));
    CHECK_EQ(*hud_width({"NVIDIA GeForce RTX"}, false, 24), std::string("559"));
    CHECK(!gpu_list(1, {}, {}).has_value());
    CHECK_EQ(*gpu_list(2, {"0000:01:00.0", "0000:06:00.0"}, {"0000:06:00.0", "0000:01:00.0"}), std::string("1,0"));
    CHECK_EQ(*gpu_list(2, {"0000:01:00.0", ""}, {"a"}), std::string("0,1"));
    auto a = anchor_offset(0, 0, 1920, 1080);
    CHECK_EQ(a.pos, std::string("top-left"));
    CHECK(a.ox == 0 && a.oy == 0);
    auto b2 = anchor_offset(1920 - 300 + 10, 20, 1920, 1080);
    CHECK_EQ(b2.pos, std::string("top-right"));
    CHECK(b2.ox == 10 && b2.oy == 20);
    auto xy = box_xy("bottom-right", -5, -5, 1920, 1080);
    CHECK(xy.first == 1920 - 300 - 5 && xy.second == 1080 - 240 - 5);
    Editor e;
    e.cpu = "Ryzen"; e.gpus = {"A", "B"}; e.pcis = {"p1", "p2"}; e.mangohud_gpu_order = {"p2", "p1"}; e.graph = true; e.full_gpu = true; e.pos_set = true; e.pos = "top-center"; e.ox = 5;
    std::string text = merge_conf("font_size=20\n", build_managed(e));
    CHECK(text.find("gpu_list=1,0") != std::string::npos && text.find("frametime=1") != std::string::npos && text.find("gpu_temp") != std::string::npos && text.find("position=top-center") != std::string::npos && text.find("offset_x=5") != std::string::npos && text.find("offset_y") == std::string::npos);
    CHECK(text.find("cpu_temp") == std::string::npos && text.find("gpu_name=1") != std::string::npos);
}

static void test_rewrite() {
    std::string a = engine::substitute("python3 {TOOLKIT_DIR}/tuxthrottle_shadercache.py heal >/dev/null 2>&1", "bob");
    CHECK(a.find("python3") == std::string::npos && a.find(" shadercache heal >/dev/null") != std::string::npos);
    std::string b = engine::substitute("ExecStart=/usr/bin/python3 {TOOLKIT_DIR}/tuxthrottle_powerd.py run --user {USER}", "bob");
    CHECK(b.find("python3") == std::string::npos && b.find("tuxthrottled") != std::string::npos && b.find(" run --user bob") != std::string::npos);
    std::string c = engine::substitute("python3 {TOOLKIT_DIR}/apply_tweak.py --id X", "bob");
    CHECK(c.find("apply-tweak --id X") != std::string::npos);
    std::string e1 = engine::substitute("install -m 0755 {TOOLKIT_DIR}/tuxthrottle_kbd.py /usr/local/bin/tuxthrottle-kbd", "bob");
    CHECK(e1.find("python") == std::string::npos && e1.find(" kbd \"$@\"") != std::string::npos);
    CHECK_EQ(engine::substitute("{PKG_INSTALL:python3-evdev}", "bob"), std::string(":"));
    CHECK_EQ(engine::substitute("chmod +x {TOOLKIT_DIR}/hotkey_listener.py", "bob"), std::string(":"));
    std::string e2 = engine::substitute("sc=$(cd {TOOLKIT_DIR} && python3 -c \"import tuxthrottle_shadercache as s; print(s.cache_dir())\")", "bob");
    CHECK(e2.find("python") == std::string::npos && e2.find(" shadercache dir)") != std::string::npos);
    std::string d = engine::substitute("python3 {TOOLKIT_DIR}/tuxthrottle_drivehealth.py scrub-all", "bob");
    CHECK(d.find(" drives scrub-all") != std::string::npos);
}

static void test_gpudrivers() {
    using namespace gpudrivers;
    auto D = [](const char* os) { return detect_distro(os, false); };
    Distro ubuntu = D("ID=ubuntu\nID_LIKE=debian"), debian = D("ID=debian"), arch = D("ID=arch"), fedora = D("ID=fedora"), nobara = D("ID=nobara\nID_LIKE=fedora"),
           gentoo = D("ID=gentoo"), sb = detect_distro("ID=fedora\nVARIANT_ID=silverblue", false);
    auto gpu = [](const char* vendor, const char* did, const char* drv = "", bool boot = false, const char* slot = "0000:01:00.0") {
        Gpu g;
        g.slot = slot; g.vendor = vendor; g.device_id = did; g.driver = drv; g.boot_vga = boot;
        if (g.vendor == "nvidia") g.generation = nvidia_generation(did);
        return g;
    };
    std::vector<Gpu> hybrid = {gpu("amd", "0x1638", "amdgpu", true, "0000:06:00.0"), gpu("nvidia", "0x25a0")};
    auto cmds = [](const Plan& p) { std::string o; for (const auto& s : p.steps) for (const auto& c : s.cmds) o += c + " ; "; return o; };
    CHECK_EQ(nvidia_generation("0x25a0"), std::string("open"));
    CHECK_EQ(nvidia_generation("0x1c82"), std::string("proprietary"));
    CHECK_EQ(nvidia_generation("0x1021"), std::string("legacy"));
    CHECK_EQ(nvidia_generation("0x06c0"), std::string("unsupported"));
    CHECK_EQ(layout(hybrid), std::string("hybrid-amd+nvidia"));
    CHECK_EQ(layout({gpu("nvidia", "0x25a0")}), std::string("nvidia-only"));
    CHECK_EQ(layout({}), std::string("none"));
    CHECK(cmds(build_plan(hybrid, ubuntu, true, true, 0)).find("ubuntu-drivers install") != std::string::npos);
    std::string deb = cmds(build_plan(hybrid, debian, true, true, 0));
    CHECK(deb.find("non-free-firmware") != std::string::npos && deb.find("dpkg --add-architecture i386") != std::string::npos);
    CHECK(cmds(build_plan(hybrid, debian, false, true, 0)).find("i386") == std::string::npos);
    std::string ar = cmds(build_plan(hybrid, arch, true, true, 0));
    CHECK(ar.find("nvidia-open-dkms") != std::string::npos && ar.find("vulkan-radeon") != std::string::npos);
    CHECK(cmds(build_plan({gpu("nvidia", "0x1c82")}, arch, true, true, 0)).find("nvidia-open-dkms") == std::string::npos);
    CHECK(cmds(build_plan(hybrid, fedora, true, true, 0)).find("rpmfusion-free-release") != std::string::npos);
    CHECK(cmds(build_plan(hybrid, nobara, true, true, 0)).find("rpmfusion") == std::string::npos);
    CHECK(cmds(build_plan(hybrid, sb, true, true, 0)).find("rpm-ostree install") != std::string::npos);
    Plan gp = build_plan(hybrid, gentoo, true, true, 0);
    bool kopen = false;
    for (const auto& st : gp.steps) kopen = kopen || st.note.find("kernel-open") != std::string::npos;
    CHECK(cmds(gp).find("x11-drivers/nvidia-drivers") != std::string::npos && kopen);
    CHECK(cmds(build_plan(hybrid, arch, true, true, 0)).find("DynamicPowerManagement") != std::string::npos);
    CHECK(cmds(build_plan(hybrid, arch, true, false, 0)).find("DynamicPowerManagement") == std::string::npos);
    CHECK(cmds(build_plan({gpu("nvidia", "0x25a0")}, arch, true, true, 0)).find("DynamicPowerManagement") == std::string::npos);
    bool manual = false;
    for (const auto& st : build_plan(hybrid, fedora, true, true, 1).steps) manual = manual || (st.manual && st.note.find("mokutil") != std::string::npos);
    CHECK(manual);
    for (const auto& st : build_plan(hybrid, fedora, true, true, 0).steps) CHECK(!st.manual);
    Plan old = build_plan({gpu("nvidia", "0x1021")}, arch, true, true, 0);
    CHECK(old.steps.empty() && !old.warnings.empty());
    CHECK(build_plan({gpu("nvidia", "0x25a0", "nvidia")}, arch, true, true, 0).steps.empty());
    CHECK(!build_plan({gpu("nvidia", "0x25a0", "nvidia")}, arch, true, true, 0, true).steps.empty());
    Plan amd_only = build_plan({gpu("amd", "0x73bf")}, ubuntu, true, true, 0);
    CHECK(cmds(amd_only).find("mesa-vulkan-drivers") != std::string::npos && cmds(amd_only).find("nvidia") == std::string::npos && !amd_only.reboot);
    CHECK(build_plan(hybrid, detect_distro("ID=plan9", false), true, true, 0).steps.empty());
}

static uint32_t crc32(const std::vector<uint8_t>& d) {
    uint32_t c = 0xFFFFFFFFu;
    for (uint8_t b : d) { c ^= b; for (int k = 0; k < 8; ++k) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1))); }
    return ~c;
}

static void test_png(const char* config_dir) {
    if (!config_dir) return;
    std::string assets = std::string(config_dir) + "/../assets/";
    struct { int n; uint32_t crc; } icons[] = {{16, 142442694u}, {64, 3712288533u}, {256, 709720250u}};   // CRC32 of the RGBA bytes as decoded by Pillow
    for (auto& ic : icons) {
        int w = 0, h = 0;
        std::vector<uint8_t> px;
        bool ok = ui::load_png(assets + "icon-" + std::to_string(ic.n) + ".png", &w, &h, &px);
        CHECK(ok && w == ic.n && h == ic.n);
        CHECK(ok && crc32(px) == ic.crc);
    }
    int w, h;
    std::vector<uint8_t> junk{1, 2, 3, 4};
    CHECK(!ui::decode_png(junk, &w, &h, &junk));
}

static Json item_json(const char* check, const char* apply) {
    return Json::parse(std::string(R"({"Content":"t","Description":"d","category":"Test","check":")") + check + R"(","apply":[")" + apply + R"("],"undo":["true"]})");
}

static void test_engine_checks(const std::string& cfg) {
    const char* tmp = std::getenv("TT_TEST_TMP");
    std::string led = std::string(tmp ? tmp : "/tmp") + "/tt-test-ledger.json";
    ::unlink(led.c_str());
    setenv("TUXTHROTTLE_STATE", led.c_str(), 1);
    std::vector<std::string> lines;
    auto log = [&](const std::string& l) { lines.push_back(l); };

    setenv("PATH", "/usr/bin:/bin", 1);   // what pkexec / systemd leave behind
    // a passing check means applied, a failing one not applied
    engine::Item yes = engine::make_item("T1", item_json("true", "true"), "tweak", "nobody");
    engine::evaluate_item(yes, Json::obj());
    CHECK_EQ(yes.state, std::string("applied"));
    engine::Item no = engine::make_item("T2", item_json("false", "true"), "tweak", "nobody");
    engine::evaluate_item(no, Json::obj());
    CHECK_EQ(no.state, std::string("not_applied"));
    // command not found is an error, never "applied"
    engine::Item missing = engine::make_item("T3", item_json("tt-no-such-tool-xyz", "true"), "tweak", "nobody");
    engine::evaluate_item(missing, Json::obj());
    CHECK_EQ(missing.state, std::string("error"));
    // a tool in /usr/local/bin is found even when PATH was reset the way pkexec does
    engine::Item loc = engine::make_item("T4", item_json("test -x /usr/local/bin || true", "true"), "tweak", "nobody");
    engine::evaluate_item(loc, Json::obj());
    { const char* pp = std::getenv("PATH"); CHECK(pp && std::string(pp).find("/usr/local/bin") != std::string::npos); }

    // ledger says applied but the check fails now -> drifted, shown as "Not detected now"
    Json led_ok = Json::obj();
    led_ok.set("action", "apply"); led_ok.set("ok", true);
    engine::Item drift = engine::make_item("T5", item_json("false", "true"), "tweak", "nobody");
    Json all_ok = Json::obj(); all_ok.set("T5", led_ok);
    engine::evaluate_item(drift, all_ok);
    CHECK_EQ(drift.state, std::string("drifted"));
    CHECK_EQ(engine::state_label(drift), std::string("Not detected now"));
    // a failed apply is "failed", not drifted
    Json led_bad = Json::obj();
    led_bad.set("action", "apply"); led_bad.set("ok", false);
    engine::Item failed = engine::make_item("T6", item_json("false", "true"), "tweak", "nobody");
    Json all_bad = Json::obj(); all_bad.set("T6", led_bad);
    engine::evaluate_item(failed, all_bad);
    CHECK_EQ(failed.state, std::string("failed"));

    // apply is verified: steps that succeed but leave the check failing are reported as a failure
    engine::Item liar = engine::make_item("T7", item_json("false", "true"), "tweak", "nobody");
    CHECK(!engine::apply_item(liar, log));
    bool saw = false;
    for (const auto& l : lines) if (l.find("VERIFY FAILED") != std::string::npos) saw = true;
    CHECK(saw);
    CHECK(!engine::ledger_load()["T7"]["ok"].as_bool());
    // ... and a really applied item passes
    engine::Item good = engine::make_item("T8", item_json("true", "true"), "tweak", "nobody");
    CHECK(engine::apply_item(good, log));
    CHECK(engine::ledger_load()["T8"]["ok"].as_bool());
    // a failing step stops the run and says which step
    engine::Item broken = engine::make_item("T9", item_json("false", "false"), "tweak", "nobody");
    CHECK(!engine::apply_item(broken, log));
    CHECK(engine::ledger_load()["T9"]["note"].str().find("step 1/1") != std::string::npos);

    // every shipped item has an id-shaped key, a non-empty label and a check, and no placeholder survives substitution
    for (const char* f : {"tweaks.json", "apps.json"}) {
        Json data = read_json_file(cfg + "/" + f);
        for (const auto& kv : data.object()) {
            engine::Item it = engine::make_item(kv.first, kv.second, std::string(f) == "tweaks.json" ? "tweak" : "app", "nobody");
            CHECK(!it.content.empty());
            CHECK(!it.check_cmd.empty());
            std::string all = it.check_cmd + "\n";
            for (const auto& c : it.apply_cmds) all += c + "\n";
            for (const auto& c : it.undo_cmds) all += c + "\n";
            for (const char* ph : {"{TT}", "{TTD}", "{TOOLKIT_DIR}", "{USER}", "{PKG_INSTALL", "{PKG_CHECK", "{KARGS"}) {
                if (all.find(ph) != std::string::npos) { std::printf("  %s keeps %s\n", kv.first.c_str(), ph); ++g_fail; }
            }
            if (all.find("python3 ") != std::string::npos && std::string(f) == "tweaks.json" && all.find(".py") != std::string::npos
                && all.find("osu_lazer_tools") == std::string::npos) { std::printf("  %s still calls a Python helper\n", kv.first.c_str()); ++g_fail; }
        }
    }
    ::unlink(led.c_str());
    unsetenv("TUXTHROTTLE_STATE");
}

static void test_legacy_chmod() {
    std::string c = engine::substitute("chmod +x {TOOLKIT_DIR}/tuxthrottle_powerd.py && echo ok", "bob");
    CHECK(c.find(".py") == std::string::npos);
    CHECK(c.find("echo ok") != std::string::npos);
}

static void test_backends() {
    auto bs = sensors::backends();
    CHECK(bs.size() >= 8);
    bool control = false, kbd = false;
    for (const auto& b : bs) {
        CHECK(!b.area.empty() && !b.name.empty());
        if (b.area == "Control") control = true;
        if (b.area == "Keyboard lighting") kbd = true;
    }
    CHECK(control && kbd);
    (void)sensors::backend_summary();  // empty on hosts with no usable backend (CI runners), so only exercised for crashes
}

static void test_kbd_state() {
    const char* tmp = std::getenv("TT_TEST_TMP");
    std::string f = std::string(tmp ? tmp : "/tmp") + "/tt-kbd-state.json";
    setenv("TUXTHROTTLE_KBD_STATE", f.c_str(), 1);
    ::unlink(f.c_str());
    CHECK(!kbd::load_state().has_value());   // nothing saved: apply-saved must be a harmless no-op
    CHECK(kbd::apply_saved() == 0);
    CHECK_EQ(kbd::hexify("#aBc123"), std::string("ABC123"));
    CHECK(kbd::valid_hex("#00ff7f") && !kbd::valid_hex("12345") && !kbd::valid_hex("gg0000"));
    unsetenv("TUXTHROTTLE_KBD_STATE");
}

void run_extra(const char* config_dir) {
    test_engine_checks(config_dir);
    test_legacy_chmod();
    test_backends();
    test_kbd_state();
    test_png(config_dir);
    test_mhedit();
    test_rewrite();
    test_gpudrivers();
    test_compat();
    test_mangohud();
    test_formats();
    test_launchers();
    test_steam();
    test_drivehealth();
    test_steamperf();
}
