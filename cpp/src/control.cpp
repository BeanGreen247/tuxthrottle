#include "control.hpp"

#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <ctime>
#include <exception>

#include "util.hpp"

namespace tt::control {

static int connect_unix(const std::string& path, int timeout_ms, int* err) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { *err = errno; return -1; }
    struct timeval tv = {timeout_ms / 1000, (timeout_ms % 1000) * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    struct sockaddr_un a;
    std::memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    if (path.size() >= sizeof a.sun_path) { close(fd); *err = ENAMETOOLONG; return -1; }
    std::strcpy(a.sun_path, path.c_str());
    if (connect(fd, reinterpret_cast<struct sockaddr*>(&a), sizeof a) != 0) {
        *err = errno;
        close(fd);
        return -1;
    }
    return fd;
}

Presence presence(const std::string& path) {
    if (!path_exists(path)) return Presence::Down;
    int err = 0;
    int fd = connect_unix(path, 500, &err);
    if (fd >= 0) { close(fd); return Presence::Up; }
    return err == EACCES || err == EPERM ? Presence::RootOnly : Presence::Down;
}

bool available(const std::string& path) { return presence(path) == Presence::Up; }

std::string presence_name(Presence p) { return p == Presence::Up ? "up" : p == Presence::RootOnly ? "root-only" : "down"; }

std::optional<Json> call(const std::string& method, const Json& params, double timeout_s, const std::string& path) {
    int err = 0;
    int fd = connect_unix(path, static_cast<int>(timeout_s * 1000), &err);
    if (fd < 0) return std::nullopt;
    Json req = Json::obj();
    req.set("method", method);
    req.set("params", params.is_object() ? params : Json::obj());
    std::string line = req.dump() + "\n";
    if (write(fd, line.data(), line.size()) != static_cast<ssize_t>(line.size())) { close(fd); return std::nullopt; }
    std::string buf;
    char tmp[65536];
    while (buf.find('\n') == std::string::npos) {
        ssize_t n = read(fd, tmp, sizeof tmp);
        if (n <= 0) break;
        buf.append(tmp, static_cast<size_t>(n));
    }
    close(fd);
    std::string resp = trim(buf.substr(0, buf.find('\n')));
    if (resp.empty()) return std::nullopt;
    try {
        return Json::parse(resp);
    } catch (const std::exception&) {
        Json j = Json::obj();
        j.set("ok", false);
        j.set("error", "bad response from daemon");
        return j;
    }
}

Server::Server(std::string path) : path_(std::move(path)) {
    add("ping", [](const Json&) {
        Json r = Json::obj();
        r.set("pong", static_cast<double>(std::time(nullptr)));
        return r;
    });
}

Server::~Server() { stop(); }

void Server::add(const std::string& name, Handler fn) { methods_[name] = std::move(fn); }

bool Server::start() {
    {
        std::string dir = dir_name(path_);
        std::string cur;
        for (size_t i = 1; i <= dir.size(); ++i)
            if (i == dir.size() || dir[i] == '/') {
                cur = dir.substr(0, i);
                ::mkdir(cur.c_str(), 0755);
            }
        ::chmod(dir.c_str(), 0755);
    }
    ::unlink(path_.c_str());
    listen_fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd_ < 0) return false;
    struct sockaddr_un a;
    std::memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    if (path_.size() >= sizeof a.sun_path) return false;
    std::strcpy(a.sun_path, path_.c_str());
    if (bind(listen_fd_, reinterpret_cast<struct sockaddr*>(&a), sizeof a) != 0 || listen(listen_fd_, 16) != 0) {
        close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    ::chmod(path_.c_str(), 0660);
    stop_ = false;
    thread_ = std::thread([this] { serve(); });
    return true;
}

void Server::serve() {
    while (!stop_) {
        struct pollfd pfd = {listen_fd_, POLLIN, 0};
        int pr = poll(&pfd, 1, 250);
        if (pr <= 0) continue;
        int fd = accept(listen_fd_, nullptr, nullptr);
        if (fd < 0) continue;
        std::thread([this, fd] { handle(fd); }).detach();
    }
}

void Server::handle(int fd) {
    struct timeval tv = {10, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    std::string buf;
    char tmp[4096];
    while (buf.find('\n') == std::string::npos && buf.size() < (1 << 20)) {
        ssize_t n = read(fd, tmp, sizeof tmp);
        if (n <= 0) break;
        buf.append(tmp, static_cast<size_t>(n));
    }
    auto reply = [&](const Json& j) {
        std::string s = j.dump() + "\n";
        if (write(fd, s.data(), s.size()) < 0) {}
    };
    auto fail = [&](const std::string& msg) {
        Json j = Json::obj();
        j.set("ok", false);
        j.set("error", msg);
        reply(j);
    };
    if (!buf.empty()) {
        try {
            Json msg = Json::parse(trim(buf.substr(0, buf.find('\n'))));
            std::string method = msg["method"].str();
            Json params = msg["params"].is_object() ? msg["params"] : Json::obj();
            auto it = methods_.find(method);
            if (it == methods_.end()) fail("unknown method: " + method);
            else {
                try {
                    Json j = Json::obj();
                    j.set("ok", true);
                    j.set("result", it->second(params));
                    reply(j);
                } catch (const std::exception& e) {
                    fail(std::string("Error: ") + e.what());
                }
            }
        } catch (const std::exception& e) {
            fail(std::string("bad request: ") + e.what());
        }
    }
    close(fd);
}

void Server::stop() {
    stop_ = true;
    if (thread_.joinable()) thread_.join();
    if (listen_fd_ >= 0) { close(listen_fd_); listen_fd_ = -1; ::unlink(path_.c_str()); }
}

}  // namespace tt::control
