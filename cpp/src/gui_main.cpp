#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "app/app.hpp"
#include "diag.hpp"
#include "engine.hpp"
#include "hotkey.hpp"

int main(int argc, char** argv) {
    // pkexec resets PATH to /usr/sbin:/usr/bin:/sbin:/bin; tools installed to /usr/local/bin (strangle, our own ctl) must stay visible to check commands
    {
        std::string p = std::getenv("PATH") ? std::getenv("PATH") : "";
        if (p.find("/usr/local/bin") == std::string::npos) setenv("PATH", ("/usr/local/bin:/usr/local/sbin" + (p.empty() ? std::string() : ":" + p)).c_str(), 1);
    }
    std::vector<std::string> a(argv + 1, argv + argc);
    auto has = [&](const char* f) { for (const auto& s : a) if (s == f) return true; return false; };
    if (has("--version")) { std::printf("tuxthrottle %s\n", tt::engine::toolkit_version().c_str()); return 0; }
    if (has("--help") || has("-h")) {
        std::puts("tuxthrottle [--report | --debug | --collect [dir] [--no-redact] | --tui | --version]\n"
                  "  (no flag)   open the window (re-runs itself elevated through pkexec/sudo)\n"
                  "  --report    print the tweak/app status table\n"
                  "  --debug     print the full hardware / OS / toolkit debug report\n"
                  "  --collect   write the hardware dump bundle (.tar.gz)\n"
                  "  --tui       terminal dashboard\n"
                  "  --no-elevate / --page NAME / --shot FILE / --scroll N / --size WxH / --script FILE   development helpers");
        return 0;
    }
    if (has("--report")) return tt::engine::cli_tweaks({"report"});
    if (has("--debug") || has("--diag")) return tt::diag::main({"--debug"});
    if (has("--collect") || has("--hw-bundle")) {
        std::vector<std::string> d{"--collect"};
        for (const auto& s : a) if (s.rfind("-", 0) != 0) d.push_back(s);
        if (has("--no-redact")) d.push_back("--no-redact");
        return tt::diag::main(d);
    }
    if (has("--tui")) return tt::tui::main({});
    tt::app::App app;
    return app.run(argc, argv);
}
