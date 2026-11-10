#include "status.hpp"

#include <cmath>

#include "util.hpp"

namespace tt::status {

Json opt(const std::optional<double>& v) { return v ? Json(*v) : Json(); }
Json opt(const std::optional<int>& v) { return v ? Json(*v) : Json(); }

Json fans_json() {
    Json a = Json::arr();
    for (const auto& f : sensors::read_fans()) {
        Json o = Json::obj();
        o.set("index", f.index);
        o.set("label", f.label);
        o.set("rpm", f.rpm);
        o.set("max", f.max);
        o.set("boost", opt(f.boost));
        a.push(o);
    }
    return a;
}

Json ryzen_json() {
    auto r = sensors::read_ryzenadj_info();
    if (!r) return Json();
    Json o = Json::obj();
    auto put = [&](const char* k, const std::optional<double>& v) { if (v) o.set(k, *v); };
    put("stapm_limit", r->stapm_limit);
    put("fast_limit", r->fast_limit);
    put("slow_limit", r->slow_limit);
    put("tctl_limit", r->tctl_limit);
    put("stapm_value", r->stapm_value);
    put("fast_value", r->fast_value);
    put("slow_value", r->slow_value);
    put("tctl_value", r->tctl_value);
    return o;
}

Json charge_limit_json() {
    auto i = sensors::battery_charge_limit_info();
    Json o = Json::obj();
    o.set("supported", i.supported);
    o.set("method", i.method.empty() ? Json() : Json(i.method));
    o.set("current", opt(i.current));
    o.set("capacity", opt(i.capacity));
    o.set("ac_online", i.ac_online ? Json(*i.ac_online) : Json());
    o.set("dell_libsmbios_possible", i.dell_libsmbios_possible);
    return o;
}

Json nvpl_json() {
    auto n = sensors::nvidia_power_limit_info();
    if (!n) return Json();
    Json o = Json::obj();
    o.set("supported", n->supported);
    o.set("current", opt(n->current));
    o.set("min", n->min);
    o.set("max", n->max);
    o.set("default", opt(n->def));
    return o;
}

Json gamemode_json() {
    auto g = sensors::gamemode_status();
    Json o = Json::obj();
    o.set("installed", g.installed);
    o.set("active", g.active);
    o.set("clients", g.clients);
    return o;
}

bool ac_online(bool* known) {
    for (const auto& p : glob_paths("/sys/class/power_supply/A[CD]*/online"))
        if (auto v = read_int(p)) { *known = true; return *v != 0; }
    *known = false;
    return false;
}

static double r2(double v) { return std::round(v * 100) / 100; }

Json gather() {
    auto cpu = sensors::cpu_temp_c();
    auto ig = sensors::igpu_values();
    auto dg = sensors::dgpu_values();
    auto freq = sensors::cpu_freq_ghz();
    Json s = Json::obj();
    s.set("model", sensors::detect_model().product);
    s.set("platform_profile", sensors::get_platform_profile());
    auto gm = sensors::gpu_mode_get();
    s.set("gpu_mode", gm ? Json(*gm) : Json());
    s.set("game_mode", sensors::game_mode_state());
    {
        Json bj = Json::arr();
        for (const auto& b : sensors::backends()) { Json o = Json::obj(); o.set("area", b.area); o.set("name", b.name); o.set("ok", b.ok); bj.push(o); }
        s.set("backends", bj);
    }
    Json c = Json::obj();
    c.set("temp_c", opt(cpu));
    c.set("freq_ghz", r2(freq.value_or(0)));
    c.set("power_w", opt(sensors::cpu_power_watts()));
    s.set("cpu", c);
    Json i = Json::obj();
    i.set("clock_mhz", opt(ig.clock_mhz));
    i.set("temp_c", opt(ig.temp_c));
    s.set("igpu", i);
    Json d = Json::obj();
    d.set("clock_mhz", opt(dg.clock_mhz));
    d.set("temp_c", opt(dg.temp_c));
    d.set("util_pct", opt(dg.util_pct));
    d.set("power_w", opt(dg.power_w));
    d.set("awake", sensors::dgpu_is_awake());
    s.set("dgpu", d);
    s.set("tdp", ryzen_json());
    s.set("fans", fans_json());
    s.set("battery_charge_limit", charge_limit_json());
    s.set("nvidia_power_limit", nvpl_json());
    s.set("gamemode", gamemode_json());
    return s;
}

Json get(const std::string& what) {
    Json o = Json::obj();
    if (what == "power-profile") {
        o.set("platform_profile", sensors::get_platform_profile());
        Json ch = Json::arr();
        for (const auto& c : sensors::platform_profile_choices()) ch.push(c);
        o.set("choices", ch);
    } else if (what == "tdp") {
        Json t = ryzen_json();
        return t.is_null() ? Json::obj() : t;
    } else if (what == "fans") {
        o.set("fans", fans_json());
        Json b = Json::arr();
        for (int v : sensors::get_fan_boost()) b.push(v);
        o.set("boost", b);
    } else if (what == "battery") {
        return charge_limit_json();
    } else if (what == "nvpl") {
        Json n = nvpl_json();
        return n.is_null() ? Json::obj() : n;
    } else if (what == "gamemode") {
        return gamemode_json();
    } else if (what == "clocks") {
        o.set("cpu_ghz", r2(sensors::cpu_freq_ghz().value_or(0)));
        o.set("igpu_mhz", opt(sensors::igpu_values().clock_mhz));
        o.set("dgpu_mhz", opt(sensors::dgpu_values().clock_mhz));
    } else if (what == "gpumode") {
        auto m = sensors::gpu_mode_get();
        o.set("mode", m ? Json(*m) : Json());
    }
    return o;
}

}  // namespace tt::status
