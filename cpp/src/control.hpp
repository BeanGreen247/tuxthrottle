// Control plane: newline-delimited JSON RPC over /run/tuxthrottle/control.sock (port of tuxthrottle_control.py).
//   -> {"method": "status", "params": {}}      <- {"ok": true, "result": {...}} | {"ok": false, "error": "..."}
#pragma once
#include <atomic>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include "json.hpp"

namespace tt::control {

inline const char* kRunDir = "/run/tuxthrottle";
inline const char* kSocketPath = "/run/tuxthrottle/control.sock";

enum class Presence { Up, RootOnly, Down };
Presence presence(const std::string& path = kSocketPath);
bool available(const std::string& path = kSocketPath);
std::string presence_name(Presence p);

// nullopt when the socket can't be reached (the caller then acts directly).
std::optional<Json> call(const std::string& method, const Json& params = Json::obj(), double timeout_s = 5.0,
                         const std::string& path = kSocketPath);

using Handler = std::function<Json(const Json& params)>;  // throws to report an error

class Server {
public:
    explicit Server(std::string path = kSocketPath);
    ~Server();
    void add(const std::string& name, Handler fn);
    bool start();
    void stop();

private:
    void serve();
    void handle(int fd);
    std::string path_;
    std::map<std::string, Handler> methods_;
    int listen_fd_ = -1;
    std::atomic<bool> stop_{false};
    std::thread thread_;
};

}  // namespace tt::control
