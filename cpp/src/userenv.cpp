#include "userenv.hpp"

#include <pwd.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <ctime>

#include "util.hpp"

namespace tt {

std::string real_user(const std::string& explicit_user) {
    if (!explicit_user.empty()) return explicit_user;
    for (const char* e : {"SUDO_USER", "PKEXEC_USER"}) {
        std::string v = env_or(e);
        if (!v.empty()) return v;
    }
    for (const char* e : {"PKEXEC_UID", "SUDO_UID"}) {
        std::string v = env_or(e);
        if (v.empty()) continue;
        if (struct passwd* p = getpwuid(static_cast<uid_t>(std::atoi(v.c_str())))) return p->pw_name;
    }
    if (struct passwd* p = getpwuid(getuid())) return p->pw_name;
    return "";
}

std::string resolve_real_user() {
    for (const char* e : {"PKEXEC_UID", "SUDO_UID"}) {
        std::string v = env_or(e);
        if (v.empty()) continue;
        if (struct passwd* p = getpwuid(static_cast<uid_t>(std::atoi(v.c_str())))) return p->pw_name;
    }
    for (const char* e : {"SUDO_USER", "PKEXEC_USER"}) {
        std::string v = env_or(e);
        if (!v.empty()) return v;
    }
    if (struct passwd* p = getpwuid(getuid())) return p->pw_name;
    return "";
}

std::string home_of(const std::string& user) {
    if (struct passwd* p = getpwnam(user.c_str())) return p->pw_dir;
    return env_or("HOME", "/root");
}

std::string config_dir(const std::string& user) {
    std::string o = env_or("TUXTHROTTLE_CONFIG_DIR");  // tests / relocated configs
    if (!o.empty()) return o;
    return home_of(real_user(user)) + "/.config/tuxthrottle";
}

bool mkdir_p(const std::string& path) {
    if (path.empty()) return false;
    std::string cur;
    size_t i = 0;
    while (i <= path.size()) {
        size_t j = path.find('/', i);
        if (j == std::string::npos) j = path.size();
        cur = path.substr(0, j);
        if (!cur.empty() && ::mkdir(cur.c_str(), 0755) != 0 && errno != EEXIST) return false;
        i = j + 1;
    }
    return true;
}

void chown_to_user(const std::string& path, const std::string& user) {
    if (!is_root()) return;
    struct passwd* pw = getpwnam(real_user(user).c_str());
    if (!pw) return;
    std::string stop = dir_name(config_dir(user));  // ~/.config
    for (std::string p = path; p.size() >= stop.size() && starts_with(p, stop); p = dir_name(p)) {
        if (::chown(p.c_str(), pw->pw_uid, pw->pw_gid) != 0) break;
        if (p == stop) break;
    }
}

Json read_json_file(const std::string& path) {
    auto t = read_file(path);
    if (!t) return Json::obj();
    try {
        Json j = Json::parse(*t);
        return j.is_object() ? j : Json::obj();
    } catch (const std::exception&) {
        return Json::obj();
    }
}

bool write_json_file(const std::string& path, const Json& data, const std::string& user) {
    mkdir_p(dir_name(path));
    if (!write_file(path, data.dump(2, true))) return false;
    chown_to_user(path, user);
    return true;
}

std::string timestamp(const char* fmt) {
    std::time_t t = std::time(nullptr);
    std::tm tm;
    localtime_r(&t, &tm);
    char buf[64];
    std::strftime(buf, sizeof buf, fmt, &tm);
    return buf;
}

}  // namespace tt
