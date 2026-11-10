// Dependency-free test runner: each CHECK prints on failure; exit status is the failure count.
#include <cmath>
#include <cstdio>
#include <string>

#include "bench.hpp"
#include "distro.hpp"
#include "json.hpp"
#include "control.hpp"
#include "kargs.hpp"
#include "powerd.hpp"
#include "profiles.hpp"
#include "userenv.hpp"
#include "util.hpp"

using namespace tt;

int g_fail = 0, g_run = 0;
void run_extra(const char* config_dir);
#define CHECK(cond)                                                              \
    do {                                                                         \
        ++g_run;                                                                 \
        if (!(cond)) { ++g_fail; std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
    } while (0)
#define CHECK_EQ(a, b) CHECK((a) == (b))

static Distro D(const char* os, bool ostree = false) { return detect_distro(os, ostree); }

static void test_json() {
    Json j = Json::parse(R"({"a":[1,2.5,true,null,"x\né"],"b":{"c":"d"},"e":-3e2})");
    CHECK(j["a"].array().size() == 5);
    CHECK(j["a"].array()[1].as_number() == 2.5);
    CHECK(j["a"].array()[2].as_bool());
    CHECK(j["a"].array()[3].is_null());
    CHECK_EQ(j["a"].array()[4].as_string(), std::string("x\n\xc3\xa9"));
    CHECK_EQ(j["b"]["c"].as_string(), std::string("d"));
    CHECK(j["e"].as_number() == -300);
    CHECK(j["missing"]["deeper"].is_null());
    Json round = Json::parse(j.dump(2));
    CHECK_EQ(round.dump(), j.dump());
    CHECK_EQ(Json::parse("[]").dump(), std::string("[]"));
    bool threw = false;
    try { Json::parse("{\"a\":"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
    threw = false;
    try { Json::parse("[1] x"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
}

static void test_config_files(const std::string& cfg) {
    for (const char* f : {"tweaks.json", "apps.json", "games.json", "presets.json", "pkgmap.json"}) {
        auto t = read_file(cfg + "/" + f);
        CHECK(t.has_value());
        if (!t) continue;
        bool ok = true;
        Json j;
        try { j = Json::parse(*t); } catch (const std::exception& e) { ok = false; std::printf("  %s: %s\n", f, e.what()); }
        CHECK(ok);
        CHECK(j.is_object());
    }
    auto tw = Json::parse(read_file(cfg + "/tweaks.json").value_or("{}"));
    CHECK(tw.object().size() > 50);
    CHECK(tw["AmdPstateActive"]["apply"].array().size() == 1);
}

static void test_distro() {
    Distro ubuntu = D("ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME=\"Ubuntu 24.04\"");
    Distro arch = D("ID=cachyos\nID_LIKE=arch");
    Distro gentoo = D("ID=gentoo");
    Distro fedora = D("ID=fedora");
    Distro nobara = D("ID=nobara\nID_LIKE=fedora");
    Distro sb = D("ID=fedora\nVARIANT_ID=silverblue");
    CHECK(ubuntu.family == "debian" && ubuntu.pkg == "apt");
    CHECK(arch.family == "arch" && arch.pkg == "pacman");
    CHECK(gentoo.pkg == "emerge");
    CHECK(nobara.family == "fedora" && !nobara.atomic);
    CHECK(sb.atomic && sb.pkg == "rpm-ostree");
    CHECK(D("ID=weirdos\nID_LIKE=\"rhel fedora\"").family == "fedora");
    CHECK(D("ID=plan9").family == "unknown");
    CHECK(D("ID=fedora", true).atomic);
    CHECK_EQ(ubuntu.name, std::string("Ubuntu 24.04"));

    Json::Object m;
    auto entry = [](std::initializer_list<std::pair<const char*, Json>> kv) {
        Json::Object o;
        for (auto& p : kv) o.emplace_back(p.first, p.second);
        return Json(std::move(o));
    };
    m.emplace_back("mangohud", entry({{"fedora", "mangohud"}, {"debian", "mangohud"}}));
    m.emplace_back("lm_sensors", entry({{"fedora", "lm_sensors"}, {"debian", "lm-sensors"}}));
    m.emplace_back("heroic", entry({{"fedora", "heroic-games-launcher"}, {"debian", Json()}, {"flatpak", "com.heroicgameslauncher.hgl"}}));
    m.emplace_back("gamemode", entry({{"fedora", "gamemode"}, {"debian", "gamemode"}, {"arch", "gamemode"}, {"gentoo", "games-utils/gamemode"}}));
    set_pkgmap(Json(std::move(m)));

    CHECK_EQ(*resolve_pkg("lm_sensors", ubuntu), std::string("lm-sensors"));
    CHECK(!resolve_pkg("heroic", ubuntu).has_value());
    CHECK_EQ(*resolve_pkg("not-in-map", arch), std::string("not-in-map"));
    CHECK_EQ(pkg_install({"gamemode"}, fedora), std::string("dnf install -y gamemode"));
    CHECK(pkg_install({"gamemode"}, ubuntu).find("apt-get install -y gamemode") != std::string::npos);
    CHECK(pkg_install({"gamemode"}, arch).rfind("pacman -S --needed", 0) == 0);
    CHECK(pkg_install({"gamemode"}, gentoo).find("games-utils/gamemode") != std::string::npos);
    CHECK(pkg_install({"gamemode"}, sb).find("rpm-ostree install") != std::string::npos);
    std::string fp = pkg_install({"heroic"}, ubuntu);
    CHECK(fp.find("flatpak install -y flathub com.heroicgameslauncher.hgl") != std::string::npos);
    CHECK(fp.find("apt-get") == std::string::npos);
    CHECK(expand("{PKG_CHECK:gamemode} && echo ok", arch).rfind("pacman -Qi gamemode", 0) == 0);
    CHECK_EQ(expand("{KARGS_ADD:amd_pstate=active}", fedora), std::string("grubby --update-kernel=ALL --args=amd_pstate=active"));
    CHECK(expand("{KARGS_ADD:a=1}", sb).find("rpm-ostree kargs --append-if-missing=a=1") != std::string::npos);
    CHECK(expand("{KARGS_ADD:a=1}", ubuntu, "ttctl kargs").find("ttctl kargs add") != std::string::npos);
    CHECK_EQ(expand("plain command ${HOME} {x}", ubuntu), std::string("plain command ${HOME} {x}"));
    CHECK_EQ(expand("{KARGS_CHECK:a=1 b}", fedora),
             std::string("grubby --info=DEFAULT 2>/dev/null | grep -qF -- a=1 && grubby --info=DEFAULT 2>/dev/null | grep -qF -- b"));

    CHECK(!portable({"dnf install -y x"}, ubuntu));
    CHECK(!portable({"grubby --update-kernel=ALL --args=x"}, arch));
    CHECK(portable({"systemctl enable foo"}, ubuntu));
    CHECK(portable({"dnf install -y x"}, fedora));
    CHECK(!portable({"dnf install -y x"}, sb));
    CHECK(!portable({"nobara-sync cli"}, arch));
    CHECK(portable({"echo mydnf"}, arch));
}

static void test_kargs() {
    using V = std::vector<std::string>;
    CHECK((kargs::edit_tokens({"quiet", "splash"}, {"amd_pstate=active"}, {}) == V{"quiet", "splash", "amd_pstate=active"}));
    CHECK((kargs::edit_tokens({"amd_pstate=passive"}, {"amd_pstate=active"}, {}) == V{"amd_pstate=active"}));
    CHECK((kargs::edit_tokens({"a=1", "a=2", "b"}, {}, {"a"}) == V{"b"}));
    CHECK((kargs::edit_tokens({"a=1", "a=2"}, {}, {"a=1"}) == V{"a=2"}));
    CHECK((kargs::edit_tokens({"quiet"}, {"nowatchdog"}, {}) == V{"quiet", "nowatchdog"}));
    CHECK(kargs::has_tokens({"a=1", "b"}, {"a=1", "b"}));
    CHECK(kargs::has_tokens({"a=1"}, {"a"}));
    CHECK(!kargs::has_tokens({"a=1"}, {"a=2"}));

    std::string text = "GRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX_DEFAULT=\"quiet splash\"\nGRUB_GFXMODE=auto\n";
    std::string out = kargs::grub_edit(text, {"mitigations=off"}, {"splash"});
    CHECK(out.find("GRUB_CMDLINE_LINUX_DEFAULT=\"quiet mitigations=off\"") != std::string::npos);
    CHECK(out.find("GRUB_TIMEOUT=5") != std::string::npos && out.find("GRUB_GFXMODE=auto") != std::string::npos);
    CHECK((kargs::grub_tokens(out) == V{"quiet", "mitigations=off"}));
    CHECK(kargs::grub_edit("GRUB_TIMEOUT=5\n", {"x=1"}, {}).find("GRUB_CMDLINE_LINUX_DEFAULT=\"x=1\"") != std::string::npos);
}

static std::string mklog(const std::vector<double>& ft) {
    std::string s = "os,cpu,gpu,ram,kernel,driver,cpuscheduler\nLinux,x,y,1,7.2,z,eevdf\n"
                    "fps,frametime,cpu_load,gpu_load,cpu_temp,gpu_temp,gpu_power,elapsed\n";
    int i = 0;
    for (double f : ft) s += std::to_string(1000 / f) + "," + std::to_string(f) + ",40,90,60,70,50," + std::to_string(i++) + "\n";
    return s;
}

static void test_bench() {
    auto st = bench::stats(bench::parse_log(mklog(std::vector<double>(1000, 16.67))));
    CHECK(std::fabs(st.avg_fps - 60) < 0.2);
    CHECK(std::fabs(st.low1_fps - 60) < 0.2);
    CHECK(st.stutters == 0);

    std::vector<double> ft(980, 10.0);
    ft.insert(ft.end(), 20, 50.0);
    auto sp = bench::stats(bench::parse_log(mklog(ft)));
    CHECK(std::fabs(sp.low1_fps - 20) < 0.6);
    CHECK(sp.stutters == 20);

    auto a = bench::stats(bench::parse_log(mklog(std::vector<double>(500, 20.0))));
    auto b = bench::stats(bench::parse_log(mklog(std::vector<double>(500, 10.0))));
    std::string avg, p99, cpu;
    for (const auto& d : bench::compare(a, b)) {
        if (d.metric == "Average FPS") avg = d.verdict;
        if (d.metric == "p99 frametime ms") p99 = d.verdict;
        if (d.metric == "CPU load %") cpu = d.verdict;
    }
    CHECK_EQ(avg, std::string("better"));
    CHECK_EQ(p99, std::string("better"));
    CHECK_EQ(cpu, std::string("same"));

    bool threw = false;
    try { bench::parse_log("nope\n"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
    CHECK(bench::mangohud_env("/x", 5, 60).find("autostart_log=5") != std::string::npos);
}

static void test_util() {
    CHECK_EQ(shell_quote("abc"), std::string("abc"));
    CHECK_EQ(shell_quote("a b"), std::string("'a b'"));
    CHECK_EQ(shell_quote("it's"), std::string("'it'\"'\"'s'"));
    CHECK_EQ(shell_quote(""), std::string("''"));
    CHECK_EQ(trim("  x \n"), std::string("x"));
    CHECK((split_ws(" a  b\tc ") == std::vector<std::string>{"a", "b", "c"}));
    auto r = run_shell("echo hi; exit 3");
    CHECK(r.code == 3 && r.out == "hi\n");
}

static void test_powerd() {
    using P = std::vector<std::pair<double, double>>;
    P pts = {{45, 0}, {60, 25}, {72, 55}, {82, 85}, {90, 100}};
    CHECK(powerd::interp(pts, 30) == 0);
    CHECK(powerd::interp(pts, 45) == 0);
    CHECK(powerd::interp(pts, 52.5) == 13);  // 12.5 rounds to 13 (half away from zero)
    CHECK(powerd::interp(pts, 66) == 40);
    CHECK(powerd::interp(pts, 95) == 100);
    CHECK(powerd::interp({}, 50) == 0);
    CHECK(powerd::interp({{90, 100}, {45, 0}}, 67.5) == 50);  // unsorted input
    int m = 0;
    CHECK(powerd::hhmm_to_min("22:30", &m) && m == 22 * 60 + 30);
    CHECK(!powerd::hhmm_to_min("nope", &m));
    CHECK(powerd::within_window(8 * 60, 17 * 60, 12 * 60));
    CHECK(!powerd::within_window(8 * 60, 17 * 60, 18 * 60));
    CHECK(powerd::within_window(22 * 60, 7 * 60, 23 * 60));   // overnight
    CHECK(powerd::within_window(22 * 60, 7 * 60, 3 * 60));
    CHECK(!powerd::within_window(22 * 60, 7 * 60, 12 * 60));
    auto q = powerd::tdp_preset("Performance");
    CHECK(q && q->stapm == 65 && q->fast == 80 && q->slow == 54);
    CHECK(!powerd::tdp_preset("Nope"));

    Json over = Json::parse(R"({"poll_s": 5, "fan_curve": {"enabled": true}, "extra": 1})");
    Json merged = powerd::merge_config(powerd::default_config(), over);
    CHECK(merged["poll_s"].as_number() == 5);
    CHECK(merged["fan_curve"]["enabled"].as_bool());
    CHECK(merged["fan_curve"]["hysteresis_c"].as_number() == 3);   // default kept
    CHECK(merged["fan_curve"]["points"].array().size() == 5);
    CHECK(merged["extra"].as_number() == 1);
    CHECK(!powerd::default_config()["fan_curve"]["enabled"].as_bool());
}

static void test_profiles_and_json() {
    CHECK_EQ(profiles::safe_name("../../etc/passwd"), std::string("etcpasswd"));
    CHECK_EQ(profiles::safe_name("My Game_1"), std::string("My Game_1"));
    CHECK_EQ(profiles::safe_name("..."), std::string("unnamed"));
    Json j = Json::parse(R"({"b":1,"a":{"z":1,"y":2}})");
    CHECK_EQ(j.dump(0, true), std::string(R"({"a":{"y":2,"z":1},"b":1})"));
    CHECK_EQ(j.dump(0, false), std::string(R"({"b":1,"a":{"z":1,"y":2}})"));
    j.set("b", 5);
    j.set("c", "x");
    CHECK(j["b"].as_number() == 5 && j.object().size() == 3);
    CHECK(j.erase("c") && !j.has("c"));

    std::string tmp = env_or("TT_TEST_TMP", ".") + "/profiles-test";
    run_shell("rm -rf " + shell_quote(tmp));
    setenv("TUXTHROTTLE_CONFIG_DIR", tmp.c_str(), 1);
    Json st = Json::obj();
    st.set("platform_profile", "balanced");
    profiles::save_profile("Gaming", st);
    auto names = profiles::list_profiles();
    CHECK(names.size() == 1 && names[0] == "Gaming");
    CHECK_EQ(profiles::load_profile("Gaming")["platform_profile"].as_string(), std::string("balanced"));
    std::string exp = tmp + "/out/g.json";
    profiles::export_profile("Gaming", exp);
    profiles::save_profile("Other", Json::obj());
    CHECK_EQ(profiles::import_profile(exp, "Imported"), std::string("Imported"));
    CHECK_EQ(profiles::load_profile("Imported")["platform_profile"].as_string(), std::string("balanced"));
    bool threw = false;
    try { profiles::import_profile(tmp + "/profiles/Gaming.json"); } catch (const std::exception&) { threw = true; }
    CHECK(threw);
    CHECK(profiles::delete_profile("Gaming") && !profiles::delete_profile("Gaming"));
    CHECK(profiles::rollback("last")[0].ok == false);   // no snapshots yet
    unsetenv("TUXTHROTTLE_CONFIG_DIR");
    run_shell("rm -rf " + shell_quote(tmp));
}

static void test_control() {
    std::string sock = env_or("TT_TEST_TMP", ".") + "/control-test.sock";
    control::Server srv(sock);
    srv.add("echo", [](const Json& p) { return p; });
    srv.add("boom", [](const Json&) -> Json { throw std::runtime_error("nope"); });
    CHECK(srv.start());
    CHECK(control::available(sock));
    auto pong = control::call("ping", Json::obj(), 3, sock);
    CHECK(pong && (*pong)["ok"].as_bool() && (*pong)["result"].has("pong"));
    Json p = Json::obj();
    p.set("x", 7);
    auto e = control::call("echo", p, 3, sock);
    CHECK(e && (*e)["result"]["x"].as_number() == 7);
    auto b = control::call("boom", Json::obj(), 3, sock);
    CHECK(b && !(*b)["ok"].as_bool() && (*b)["error"].as_string().find("nope") != std::string::npos);
    auto u = control::call("nothing", Json::obj(), 3, sock);
    CHECK(u && (*u)["error"].as_string().find("unknown method") != std::string::npos);
    srv.stop();
    CHECK(!control::available(sock));
    CHECK(!control::call("ping", Json::obj(), 1, sock));
}

int main(int argc, char** argv) {
    test_json();
    test_util();
    test_distro();
    test_kargs();
    test_bench();
    test_powerd();
    test_profiles_and_json();
    test_control();
    run_extra(argc > 1 ? argv[1] : nullptr);
    if (argc > 1) test_config_files(argv[1]);
    std::printf("%d checks, %d failed\n", g_run, g_fail);
    return g_fail == 0 ? 0 : 1;
}
