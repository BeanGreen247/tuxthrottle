// tuxthrottled - the power daemon (fan curve, AC switch, game profiles, schedule, thermal events, control socket).
#include <cstdio>
#include <string>
#include <vector>

#include "powerd.hpp"
#include "sensors.hpp"
#include "userenv.hpp"
#include "util.hpp"

int main(int argc, char** argv) {
    using namespace tt;
    std::string mode = "run", user, config;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "run" || a == "once") mode = a;
        else if (a == "--user" && i + 1 < argc) user = argv[++i];
        else if (a == "--config" && i + 1 < argc) config = argv[++i];
        else { std::fprintf(stderr, "usage: tuxthrottled [run|once] [--user NAME] [--config FILE]\n"); return 2; }
    }
    if (!is_root()) std::puts("[powerd] warning: not root - fan/profile writes will fail");
    if (user.empty()) user = env_or("SUDO_USER");
    sensors::set_session_user(user);
    if (config.empty()) config = (user.empty() ? config_dir() : config_dir(user)) + "/powerd.json";
    return powerd::run(config, user, mode == "once");
}
