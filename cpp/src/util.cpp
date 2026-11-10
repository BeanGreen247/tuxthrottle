#include "util.hpp"

#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <sstream>

namespace tt {

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\n' || s[a] == '\r')) ++a;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\n' || s[b - 1] == '\r')) --b;
    return s.substr(a, b - a);
}

std::vector<std::string> split_ws(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream is(s);
    std::string w;
    while (is >> w) out.push_back(w);
    return out;
}

std::vector<std::string> split_lines(const std::string& s) {
    std::vector<std::string> out;
    size_t a = 0;
    while (a <= s.size()) {
        size_t b = s.find('\n', a);
        if (b == std::string::npos) b = s.size();
        std::string line = s.substr(a, b - a);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (!(b == s.size() && line.empty())) out.push_back(std::move(line));
        a = b + 1;
    }
    return out;
}

std::string join(const std::vector<std::string>& v, const std::string& sep) {
    std::string out;
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

bool starts_with(const std::string& s, const std::string& p) { return s.compare(0, p.size(), p) == 0; }
bool ends_with(const std::string& s, const std::string& p) {
    return s.size() >= p.size() && s.compare(s.size() - p.size(), p.size(), p) == 0;
}

std::string replace_all(std::string s, const std::string& from, const std::string& to) {
    if (from.empty()) return s;
    size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
    return s;
}

std::string lower(std::string s) {
    for (auto& c : s)
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + 32);
    return s;
}

std::string shell_quote(const std::string& s) {
    if (s.empty()) return "''";
    bool safe = true;
    for (unsigned char c : s) {
        if (!(std::isalnum(c) || std::strchr("@%+=:,./-_", c))) { safe = false; break; }
    }
    if (safe) return s;
    return "'" + replace_all(s, "'", "'\"'\"'") + "'";
}

std::optional<std::string> read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return std::nullopt;
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

bool write_file(const std::string& path, const std::string& data) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f << data;
    return static_cast<bool>(f);
}

bool path_exists(const std::string& path) {
    struct stat st;
    return ::stat(path.c_str(), &st) == 0;
}

RunResult run_shell(const std::string& cmd) {
    RunResult r;
    int fds[2];
    if (pipe(fds) != 0) return r;
    pid_t pid = fork();
    if (pid < 0) {
        close(fds[0]);
        close(fds[1]);
        return r;
    }
    if (pid == 0) {
        dup2(fds[1], 1);
        dup2(fds[1], 2);
        close(fds[0]);
        close(fds[1]);
        execl("/bin/bash", "bash", "-c", cmd.c_str(), static_cast<char*>(nullptr));
        _exit(127);
    }
    close(fds[1]);
    char buf[4096];
    ssize_t n;
    while ((n = read(fds[0], buf, sizeof buf)) > 0 || (n < 0 && errno == EINTR))
        if (n > 0) r.out.append(buf, static_cast<size_t>(n));
    close(fds[0]);
    int st = 0;
    waitpid(pid, &st, 0);
    r.code = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
    return r;
}


static RunResult run_argv_impl(const std::vector<std::string>& argv, int timeout_ms, bool merge_stderr) {
    RunResult r;
    if (argv.empty()) return r;
    int fds[2];
    if (pipe(fds) != 0) return r;
    pid_t pid = fork();
    if (pid < 0) {
        close(fds[0]);
        close(fds[1]);
        return r;
    }
    if (pid == 0) {
        dup2(fds[1], 1);
        if (merge_stderr) dup2(fds[1], 2);
        else {
            int nul = open("/dev/null", O_WRONLY);
            if (nul >= 0) { dup2(nul, 2); close(nul); }
        }
        close(fds[0]);
        close(fds[1]);
        std::vector<char*> av;
        for (const auto& a : argv) av.push_back(const_cast<char*>(a.c_str()));
        av.push_back(nullptr);
        execvp(av[0], av.data());
        _exit(127);
    }
    close(fds[1]);
    double deadline = timeout_ms > 0 ? now_seconds() + timeout_ms / 1000.0 : 0;
    bool timed_out = false;
    char buf[4096];
    while (true) {
        int wait_ms = -1;
        if (deadline > 0) {
            double left = deadline - now_seconds();
            if (left <= 0) { timed_out = true; break; }
            wait_ms = static_cast<int>(left * 1000) + 1;
        }
        struct pollfd pfd = {fds[0], POLLIN, 0};
        int pr = poll(&pfd, 1, wait_ms);
        if (pr < 0 && errno == EINTR) continue;
        if (pr == 0) { timed_out = true; break; }
        ssize_t n = read(fds[0], buf, sizeof buf);
        if (n > 0) r.out.append(buf, static_cast<size_t>(n));
        else if (n == 0) break;
        else if (errno != EINTR) break;
    }
    close(fds[0]);
    if (timed_out) kill(pid, SIGKILL);
    int st = 0;
    waitpid(pid, &st, 0);
    if (timed_out) r.code = 124;
    else r.code = WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
    return r;
}

RunResult run_argv(const std::vector<std::string>& argv, int timeout_ms) { return run_argv_impl(argv, timeout_ms, true); }
RunResult run_argv_stdout(const std::vector<std::string>& argv, int timeout_ms) { return run_argv_impl(argv, timeout_ms, false); }

std::vector<std::string> glob_paths(const std::string& pattern) {
    std::vector<std::string> out;
    glob_t g;
    if (glob(pattern.c_str(), 0, nullptr, &g) == 0)
        for (size_t i = 0; i < g.gl_pathc; ++i) out.emplace_back(g.gl_pathv[i]);
    globfree(&g);
    return out;
}

std::string read_trim(const std::string& path) { return trim(read_file(path).value_or("")); }

std::optional<long long> read_int(const std::string& path) {
    auto t = read_file(path);
    if (!t) return std::nullopt;
    std::string s = trim(*t);
    if (s.empty()) return std::nullopt;
    char* end = nullptr;
    long long v = std::strtoll(s.c_str(), &end, 10);
    if (end == s.c_str() || *end != '\0') return std::nullopt;
    return v;
}

std::string real_path(const std::string& path) {
    char buf[4096];
    return ::realpath(path.c_str(), buf) ? std::string(buf) : path;
}

std::string base_name(const std::string& path) {
    std::string p = path;
    while (p.size() > 1 && p.back() == '/') p.pop_back();
    size_t i = p.rfind('/');
    return i == std::string::npos ? p : p.substr(i + 1);
}

std::string dir_name(const std::string& path) {
    size_t i = path.rfind('/');
    if (i == std::string::npos) return ".";
    return i == 0 ? "/" : path.substr(0, i);
}

bool write_text(const std::string& path, const std::string& text) {
    int fd = open(path.c_str(), O_WRONLY);
    if (fd < 0) return false;
    ssize_t n = write(fd, text.data(), text.size());
    int rc = close(fd);
    return n == static_cast<ssize_t>(text.size()) && rc == 0;
}

std::string which(const std::string& cmd) {
    static std::map<std::string, std::string> cache;
    auto it = cache.find(cmd);
    if (it != cache.end()) return it->second;
    std::string found;
    if (cmd.find('/') != std::string::npos) {
        if (access(cmd.c_str(), X_OK) == 0) found = cmd;
    } else {
        std::string path = env_or("PATH", "/usr/local/bin:/usr/bin:/bin");
        size_t a = 0;
        while (a <= path.size()) {
            size_t b = path.find(':', a);
            if (b == std::string::npos) b = path.size();
            std::string dir = path.substr(a, b - a);
            if (!dir.empty()) {
                std::string cand = dir + "/" + cmd;
                struct stat st;
                if (::stat(cand.c_str(), &st) == 0 && S_ISREG(st.st_mode) && access(cand.c_str(), X_OK) == 0) {
                    found = cand;
                    break;
                }
            }
            a = b + 1;
        }
    }
    cache[cmd] = found;
    return found;
}

std::string env_or(const char* name, const std::string& def) {
    const char* v = std::getenv(name);
    return v && *v ? std::string(v) : def;
}

double now_seconds() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

void sleep_ms(int ms) {
    struct timespec ts = {ms / 1000, (ms % 1000) * 1000000L};
    nanosleep(&ts, nullptr);
}

std::string format_double(double v, int decimals) {
    char b[64];
    std::snprintf(b, sizeof b, "%.*f", decimals, v);
    return b;
}

std::string regex_replace_fn(const std::string& text, const std::regex& re, const std::function<std::string(const std::smatch&)>& fn, bool first_only) {
    std::string out;
    size_t last = 0;
    for (auto it = std::sregex_iterator(text.begin(), text.end(), re); it != std::sregex_iterator(); ++it) {
        const std::smatch& m = *it;
        out += text.substr(last, static_cast<size_t>(m.position()) - last);
        out += fn(m);
        last = static_cast<size_t>(m.position()) + static_cast<size_t>(m.length());
        if (first_only) break;
    }
    out += text.substr(last);
    return out;
}

size_t utf8_len(const std::string& s) {
    size_t n = 0;
    for (unsigned char c : s) if ((c & 0xC0) != 0x80) ++n;
    return n;
}

std::string utf8_trunc(const std::string& s, size_t n) {
    size_t count = 0, i = 0;
    for (; i < s.size(); ++i) {
        if ((static_cast<unsigned char>(s[i]) & 0xC0) != 0x80) {
            if (count == n) break;
            ++count;
        }
    }
    return s.substr(0, i);
}

std::string utf8_pad(const std::string& s, size_t width) {
    size_t n = utf8_len(s);
    return n >= width ? s : s + std::string(width - n, ' ');
}

bool is_root() { return geteuid() == 0; }

int run_shell_stream(const std::string& cmd, const std::function<void(const std::string&)>& on_line, int timeout_ms) {
    int fds[2];
    if (pipe(fds) != 0) return -1;
    pid_t pid = fork();
    if (pid < 0) { close(fds[0]); close(fds[1]); return -1; }
    if (pid == 0) {
        dup2(fds[1], 1);
        dup2(fds[1], 2);
        close(fds[0]);
        close(fds[1]);
        execl("/bin/bash", "bash", "-c", cmd.c_str(), static_cast<char*>(nullptr));
        _exit(127);
    }
    close(fds[1]);
    double deadline = timeout_ms > 0 ? now_seconds() + timeout_ms / 1000.0 : 0;
    bool timed_out = false;
    std::string pending;
    char buf[4096];
    while (true) {
        int wait_ms = -1;
        if (deadline > 0) {
            double left = deadline - now_seconds();
            if (left <= 0) { timed_out = true; break; }
            wait_ms = static_cast<int>(left * 1000) + 1;
        }
        struct pollfd pfd = {fds[0], POLLIN, 0};
        int pr = poll(&pfd, 1, wait_ms);
        if (pr < 0 && errno == EINTR) continue;
        if (pr == 0) { timed_out = true; break; }
        ssize_t n = read(fds[0], buf, sizeof buf);
        if (n <= 0) break;
        pending.append(buf, static_cast<size_t>(n));
        size_t nl;
        while ((nl = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, nl);
            pending.erase(0, nl + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            on_line(line);
        }
    }
    if (!pending.empty() && !timed_out) on_line(pending);
    close(fds[0]);
    if (timed_out) kill(pid, SIGKILL);
    int st = 0;
    waitpid(pid, &st, 0);
    if (timed_out) return 124;
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0);
}

std::string self_exe() { return real_path("/proc/self/exe"); }

std::string ctl_exe() {
    std::string me = self_exe();
    if (base_name(me) == "tuxthrottlectl") return me;
    std::string sib = dir_name(me) + "/tuxthrottlectl";
    if (path_exists(sib)) return sib;
    std::string w = which("tuxthrottlectl");
    return w.empty() ? me : w;
}

std::pair<bool, std::string> privileged_ctl(const std::vector<std::string>& args) {
    std::string exe = which("tuxthrottlectl");
    if (exe.empty()) exe = ctl_exe();
    std::string last;
    for (const auto& launcher : {std::vector<std::string>{"pkexec"}, std::vector<std::string>{"sudo", "-n"}, std::vector<std::string>{}}) {
        if (!launcher.empty() && which(launcher[0]).empty()) continue;
        std::vector<std::string> cmd = launcher;
        cmd.push_back(exe);
        cmd.insert(cmd.end(), args.begin(), args.end());
        auto r = run_argv(cmd, 20000);
        if (r.code == 0) return {true, trim(r.out)};
        last = trim(r.out);
    }
    return {false, last.empty() ? "no launcher found (need pkexec or passwordless sudo)" : last};
}

long spawn_quiet(const std::vector<std::string>& argv, const std::vector<std::string>& extra_env, bool new_session) {
    if (argv.empty()) return -1;
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        if (new_session) setsid();
        int nul = open("/dev/null", O_RDWR);
        if (nul >= 0) { dup2(nul, 0); dup2(nul, 1); dup2(nul, 2); if (nul > 2) close(nul); }
        for (const auto& e : extra_env) {
            size_t eq = e.find('=');
            if (eq != std::string::npos) setenv(e.substr(0, eq).c_str(), e.substr(eq + 1).c_str(), 1);
        }
        std::vector<char*> av;
        for (const auto& a : argv) av.push_back(const_cast<char*>(a.c_str()));
        av.push_back(nullptr);
        execvp(av[0], av.data());
        _exit(127);
    }
    return pid;
}

bool child_running(long pid, int* exit_code) {
    int st = 0;
    pid_t r = waitpid(static_cast<pid_t>(pid), &st, WNOHANG);
    if (r == 0) return true;
    if (exit_code) *exit_code = r < 0 ? -1 : (WIFEXITED(st) ? WEXITSTATUS(st) : 128 + (WIFSIGNALED(st) ? WTERMSIG(st) : 0));
    return false;
}

void child_stop(long pid, int wait_ms) {
    if (pid <= 0) return;
    kill(static_cast<pid_t>(pid), SIGTERM);
    double deadline = now_seconds() + wait_ms / 1000.0;
    int rc;
    while (child_running(pid, &rc)) {
        if (now_seconds() > deadline) { kill(static_cast<pid_t>(pid), SIGKILL); waitpid(static_cast<pid_t>(pid), nullptr, 0); return; }
        sleep_ms(50);
    }
}

}  // namespace tt
