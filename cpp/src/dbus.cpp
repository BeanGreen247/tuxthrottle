#include "dbus.hpp"

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>

namespace tt::dbus {

static size_t type_align(char c) {
    switch (c) {
        case 'y': case 'g': case 'v': return 1;
        case 'n': case 'q': return 2;
        case 'b': case 'i': case 'u': case 's': case 'o': case 'a': return 4;
        case 'x': case 't': case 'd': case '(': case '{': return 8;
        default: return 1;
    }
}

static size_t type_end(const std::string& sig, size_t i) {
    if (i >= sig.size()) return i;
    char c = sig[i];
    if (c == 'a') return type_end(sig, i + 1);
    if (c == '(' || c == '{') {
        int depth = 0;
        while (i < sig.size()) { char t = sig[i++]; if (t == '(' || t == '{') ++depth; else if (t == ')' || t == '}') { if (--depth == 0) break; } }
        return i;
    }
    return i + 1;
}

void Reader::skip_value(const std::string& sig, size_t& i) {
    if (i >= sig.size() || err_) { err_ = true; return; }
    char c = sig[i];
    switch (c) {
        case 'y': ++i; u8(); break;
        case 'b': case 'i': case 'u': ++i; u32(); break;
        case 'n': case 'q': ++i; align(2); pos_ += 2; break;
        case 'x': case 't': case 'd': ++i; align(8); pos_ += 8; break;
        case 's': case 'o': ++i; str(); break;
        case 'g': ++i; this->sig(); break;
        case 'v': { ++i; std::string s = this->sig(); size_t j = 0; skip_value(s, j); break; }
        case 'a': {
            ++i;
            uint32_t len = u32();
            size_t elem = i, end_t = type_end(sig, i);
            align(type_align(sig[elem]));
            size_t end = pos_ + len;
            while (pos_ < end && !err_) { size_t k = elem; skip_value(sig, k); }
            i = end_t;
            break;
        }
        case '(': case '{': {
            ++i;
            align(8);
            while (i < sig.size() && sig[i] != ')' && sig[i] != '}') skip_value(sig, i);
            ++i;
            break;
        }
        default: err_ = true;
    }
    if (pos_ > n_) err_ = true;
}

// ---------------------------------------------------------------- encode / decode
std::vector<uint8_t> encode(const Message& m) {
    Writer h;
    h.u8('l'); h.u8(m.type); h.u8(m.flags); h.u8(1);
    h.u32(static_cast<uint32_t>(m.body.size()));
    h.u32(m.serial);
    auto fields = h.begin_array(8);
    auto field = [&](uint8_t code, char t, const std::string& s) { h.align(8); h.u8(code); h.sig(std::string(1, t)); if (t == 'g') h.sig(s); else h.str(s); };
    if (!m.path.empty()) field(1, 'o', m.path);
    if (!m.iface.empty()) field(2, 's', m.iface);
    if (!m.member.empty()) field(3, 's', m.member);
    if (!m.error.empty()) field(4, 's', m.error);
    if (m.type == kMethodReturn || m.type == kError) { h.align(8); h.u8(5); h.sig("u"); h.u32(m.reply_serial); }
    if (!m.dest.empty()) field(6, 's', m.dest);
    if (!m.signature.empty()) field(8, 'g', m.signature);
    h.end_array(fields);
    h.align(8);
    std::vector<uint8_t> out = h.data();
    out.insert(out.end(), m.body.begin(), m.body.end());
    return out;
}

long decode(const std::vector<uint8_t>& buf, Message* out) {
    if (buf.size() < 16) return 0;
    if (buf[0] != 'l') return -1;
    uint32_t blen, serial, flen;
    std::memcpy(&blen, &buf[4], 4);
    std::memcpy(&serial, &buf[8], 4);
    std::memcpy(&flen, &buf[12], 4);
    size_t hdr_end = 16 + flen;
    size_t body_at = (hdr_end + 7) & ~static_cast<size_t>(7);
    if (flen > (1u << 20) || blen > (1u << 27)) return -1;
    if (buf.size() < body_at + blen) return 0;
    Message m;
    m.type = buf[1]; m.flags = buf[2]; m.serial = serial;
    Reader r(buf.data(), hdr_end, 0);
    r.seek(16);
    while (r.pos() < hdr_end && r.ok()) {
        r.align(8);
        if (r.pos() >= hdr_end) break;
        uint8_t code = r.u8();
        std::string sg = r.sig();
        if (sg == "o" || sg == "s") { std::string v = r.str(); if (code == 1) m.path = v; else if (code == 2) m.iface = v; else if (code == 3) m.member = v; else if (code == 4) m.error = v; else if (code == 6) m.dest = v; else if (code == 7) m.sender = v; }
        else if (sg == "g") { std::string v = r.sig(); if (code == 8) m.signature = v; }
        else if (sg == "u") { uint32_t v = r.u32(); if (code == 5) m.reply_serial = v; }
        else { size_t i = 0; r.skip_value(sg, i); }
    }
    if (!r.ok()) return -1;
    m.body.assign(buf.begin() + static_cast<long>(body_at), buf.begin() + static_cast<long>(body_at + blen));
    *out = std::move(m);
    return static_cast<long>(body_at + blen);
}

// ---------------------------------------------------------------- connection
static std::string hex(const std::string& s) {
    static const char* d = "0123456789abcdef";
    std::string o;
    for (unsigned char c : s) { o += d[c >> 4]; o += d[c & 15]; }
    return o;
}

Connection::~Connection() { if (fd_ >= 0) ::close(fd_); }

bool Connection::write_all(const uint8_t* p, size_t n) {
    while (n) {
        ssize_t w = ::send(fd_, p, n, MSG_NOSIGNAL);
        if (w < 0) { if (errno == EINTR) continue; return false; }
        p += w; n -= static_cast<size_t>(w);
    }
    return true;
}

bool Connection::connect(const std::string& address_in) {
    std::string addr = address_in;
    if (addr.empty()) { const char* e = std::getenv("DBUS_SESSION_BUS_ADDRESS"); addr = e ? e : "unix:path=/run/user/" + std::to_string(getuid()) + "/bus"; }
    std::string path;
    bool abstract = false;
    size_t p = addr.find("path=");
    if (p == std::string::npos) { p = addr.find("abstract="); abstract = true; if (p != std::string::npos) p += 9; } else p += 5;
    if (p == std::string::npos) return false;
    size_t e = addr.find_first_of(",;", p);
    path = addr.substr(p, e == std::string::npos ? std::string::npos : e - p);
    fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd_ < 0) return false;
    sockaddr_un sa{};
    sa.sun_family = AF_UNIX;
    socklen_t len;
    if (abstract) { sa.sun_path[0] = 0; std::memcpy(sa.sun_path + 1, path.data(), std::min(path.size(), sizeof(sa.sun_path) - 2)); len = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + 1 + path.size()); }
    else { std::snprintf(sa.sun_path, sizeof sa.sun_path, "%s", path.c_str()); len = sizeof sa; }
    if (::connect(fd_, reinterpret_cast<sockaddr*>(&sa), len) != 0) { ::close(fd_); fd_ = -1; return false; }
    std::string auth = std::string(1, '\0') + "AUTH EXTERNAL " + hex(std::to_string(getuid())) + "\r\n";
    if (!write_all(reinterpret_cast<const uint8_t*>(auth.data()), auth.size())) return false;
    std::string resp;
    char c;
    while (resp.find("\r\n") == std::string::npos) { if (::read(fd_, &c, 1) != 1) return false; resp += c; }
    if (resp.rfind("OK", 0) != 0) return false;
    std::string begin = "BEGIN\r\n";
    if (!write_all(reinterpret_cast<const uint8_t*>(begin.data()), begin.size())) return false;
    Message hello, reply;
    hello.type = kMethodCall; hello.dest = "org.freedesktop.DBus"; hello.path = "/org/freedesktop/DBus"; hello.iface = "org.freedesktop.DBus"; hello.member = "Hello";
    if (!call(hello, &reply)) return false;
    Reader r(reply.body.data(), reply.body.size());
    name_ = r.str();
    return !name_.empty();
}

uint32_t Connection::send(Message m) {
    m.serial = serial_++;
    auto b = encode(m);
    write_all(b.data(), b.size());
    return m.serial;
}

bool Connection::parse_available() {
    while (true) {
        Message m;
        long n = decode(rx_, &m);
        if (n < 0) return false;
        if (n == 0) return true;
        rx_.erase(rx_.begin(), rx_.begin() + n);
        queue_.push_back(std::move(m));
    }
}

bool Connection::pump() {
    uint8_t buf[8192];
    while (true) {
        pollfd pf{fd_, POLLIN, 0};
        if (poll(&pf, 1, 0) <= 0) break;
        ssize_t n = ::read(fd_, buf, sizeof buf);
        if (n <= 0) return false;
        rx_.insert(rx_.end(), buf, buf + n);
    }
    return parse_available();
}

std::vector<Message> Connection::take() { std::vector<Message> v; v.swap(queue_); return v; }

bool Connection::call(Message m, Message* reply, int timeout_ms) {
    m.type = kMethodCall;
    uint32_t serial = send(m);
    int waited = 0;
    while (waited <= timeout_ms) {
        // check already-parsed messages first (hello happens before the queue is used)
        for (size_t i = 0; i < queue_.size(); ++i)
            if ((queue_[i].type == kMethodReturn || queue_[i].type == kError) && queue_[i].reply_serial == serial) {
                Message r = std::move(queue_[i]);
                queue_.erase(queue_.begin() + static_cast<long>(i));
                bool ok = r.type == kMethodReturn;
                if (reply) *reply = std::move(r);
                return ok;
            }
        pollfd pf{fd_, POLLIN, 0};
        int pr = poll(&pf, 1, 50);
        if (pr < 0 && errno != EINTR) return false;
        if (pr > 0) {
            uint8_t buf[8192];
            ssize_t n = ::read(fd_, buf, sizeof buf);
            if (n <= 0) return false;
            rx_.insert(rx_.end(), buf, buf + n);
            if (!parse_available()) return false;
        } else waited += 50;
    }
    return false;
}

bool Connection::request_name(const std::string& name) {
    Message m, r;
    m.dest = "org.freedesktop.DBus"; m.path = "/org/freedesktop/DBus"; m.iface = "org.freedesktop.DBus"; m.member = "RequestName";
    Writer w;
    w.str(name); w.u32(4);   // DBUS_NAME_FLAG_DO_NOT_QUEUE
    m.signature = "su"; m.body = w.data();
    if (!call(m, &r)) return false;
    Reader rd(r.body.data(), r.body.size());
    return rd.u32() == 1;
}

bool Connection::add_match(const std::string& rule) {
    Message m, r;
    m.dest = "org.freedesktop.DBus"; m.path = "/org/freedesktop/DBus"; m.iface = "org.freedesktop.DBus"; m.member = "AddMatch";
    Writer w;
    w.str(rule);
    m.signature = "s"; m.body = w.data();
    return call(m, &r);
}

void Connection::reply_ok(const Message& req, const std::string& sig, const std::vector<uint8_t>& body) {
    Message m;
    m.type = kMethodReturn; m.reply_serial = req.serial; m.dest = req.sender; m.signature = sig; m.body = body;
    m.flags = 1;
    send(m);
}

void Connection::reply_error(const Message& req, const std::string& name, const std::string& text) {
    Message m;
    m.type = kError; m.reply_serial = req.serial; m.dest = req.sender; m.error = name; m.signature = "s";
    Writer w;
    w.str(text);
    m.body = w.data();
    m.flags = 1;
    send(m);
}

void Connection::emit(const std::string& path, const std::string& iface, const std::string& member, const std::string& sig, const std::vector<uint8_t>& body) {
    Message m;
    m.type = kSignal; m.path = path; m.iface = iface; m.member = member; m.signature = sig; m.body = body; m.flags = 1;
    send(m);
}

}  // namespace tt::dbus
