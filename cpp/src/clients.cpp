// Panel clients: the waybar module and the MangoHud exec line (ports of clients/waybar and clients/mangohud).
#include <cstdio>

#include "json.hpp"
#include "status.hpp"
#include "util.hpp"

namespace tt::clients {

static bool num(const Json& j) { return j.is_number(); }

int waybar_main(const std::vector<std::string>& a) {
    bool toggle = std::find(a.begin(), a.end(), "--toggle") != a.end();
    Json st = status::gather();
    if (toggle) {
        std::string cur = lower(st["platform_profile"].str("balanced"));
        std::string next = cur != "performance" ? "performance" : "balanced";
        auto r = privileged_ctl({"set", "power-profile", next});
        Json o = Json::obj();
        o.set("text", "\xEF\x80\x93 " + next);
        o.set("tooltip", r.second);
        std::puts(o.dump().c_str());
        return r.first ? 0 : 1;
    }
    Json o = Json::obj();
    if (st.object().empty()) {
        o.set("text", "  n/a"); o.set("tooltip", "tuxthrottlectl not reachable"); o.set("class", "critical");
        std::puts(o.dump().c_str());
        return 0;
    }
    const Json& cpu = st["cpu"]["temp_c"];
    const Json& dgpu = st["dgpu"]["temp_c"];
    std::string prof = st["platform_profile"].str("?");
    prof = prof.empty() ? "?" : std::string(1, static_cast<char>(std::toupper(prof[0])));
    double hot = 0;
    if (num(cpu)) hot = std::max(hot, cpu.as_number());
    if (num(dgpu)) hot = std::max(hot, dgpu.as_number());
    std::string cls = hot >= 95 ? "critical" : hot >= 85 ? "hot" : hot >= 70 ? "warm" : "cool";
    std::vector<std::string> parts;
    if (num(cpu)) parts.push_back("\xEF\x8B\x9B " + format_double(cpu.as_number(), 0) + "\xC2\xB0");
    if (num(dgpu)) parts.push_back("\xF3\xB0\xA2\xAE " + format_double(dgpu.as_number(), 0) + "\xC2\xB0");
    parts.push_back(prof);
    std::vector<std::string> tip{"profile: " + st["platform_profile"].str()};
    const Json& tdp = st["tdp"];
    if (num(tdp["stapm_limit"]) && tdp["stapm_limit"].as_number() > 0)
        tip.push_back("TDP: " + format_double(tdp["stapm_limit"].as_number(), 0) + "/" + format_double(tdp["fast_limit"].as_number(), 0) + "/" + format_double(tdp["slow_limit"].as_number(), 0) + " W");
    if (st["gamemode"]["active"].as_bool()) tip.push_back("Feral GameMode: active");
    std::vector<std::string> fans;
    for (const auto& f : st["fans"].array()) fans.push_back(f["label"].str() + " " + format_double(f["rpm"].as_number(), 0) + "rpm");
    if (!fans.empty()) tip.push_back("fans: " + join(fans, ", "));
    o.set("text", "  " + join(parts, "  "));
    o.set("class", cls);
    o.set("percentage", static_cast<int>(hot));
    o.set("tooltip", join(tip, "\n"));
    std::puts(o.dump().c_str());
    return 0;
}

int mangohud_line_main(const std::vector<std::string>&) {
    Json st = status::gather();
    if (st.object().empty()) { std::puts("TT n/a"); return 0; }
    static const std::map<std::string, std::string> abbr = {{"balanced", "bal"}, {"performance", "perf"}, {"power-saver", "save"}, {"quiet", "quiet"}, {"low-power", "save"}, {"custom", "cust"}};
    std::string prof = lower(st["platform_profile"].str("?"));
    auto it = abbr.find(prof);
    std::vector<std::string> seg{"TT " + (it != abbr.end() ? it->second : prof.substr(0, 4))};
    const Json& cpu = st["cpu"];
    if (num(cpu["power_w"])) seg.push_back(format_double(cpu["power_w"].as_number(), 0) + "W");
    if (num(cpu["temp_c"])) seg.push_back(format_double(cpu["temp_c"].as_number(), 0) + "C");
    std::string line = join(seg, "  ");
    if (num(st["dgpu"]["temp_c"])) line += "  | GPU " + format_double(st["dgpu"]["temp_c"].as_number(), 0) + "C";
    std::puts(line.c_str());
    return 0;
}

}  // namespace tt::clients
