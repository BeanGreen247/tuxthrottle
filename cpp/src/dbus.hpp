// Minimal D-Bus session-bus client (SASL EXTERNAL, method calls, signals, object serving) - just enough for a
// StatusNotifierItem tray icon without libdbus. Little-endian only.
#pragma once
#include <cstdint>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <vector>

namespace tt::dbus {

class Writer {
public:
    void align(size_t n) { while (buf_.size() % n) buf_.push_back(0); }
    void u8(uint8_t v) { buf_.push_back(v); }
    void boolean(bool v) { u32(v ? 1 : 0); }
    void u16(uint16_t v) { align(2); put(&v, 2); }
    void i32(int32_t v) { align(4); put(&v, 4); }
    void u32(uint32_t v) { align(4); put(&v, 4); }
    void str(const std::string& s) { u32(static_cast<uint32_t>(s.size())); buf_.insert(buf_.end(), s.begin(), s.end()); buf_.push_back(0); }
    void obj(const std::string& s) { str(s); }
    void sig(const std::string& s) { u8(static_cast<uint8_t>(s.size())); buf_.insert(buf_.end(), s.begin(), s.end()); buf_.push_back(0); }
    void bytes(const std::vector<uint8_t>& b) { buf_.insert(buf_.end(), b.begin(), b.end()); }
    // arrays: begin_array writes the length slot + pads to the element alignment; end_array patches the length
    struct ArrayMark { size_t len_at, start; };
    ArrayMark begin_array(size_t elem_align) { align(4); size_t at = buf_.size(); u32(0); align(elem_align); return {at, buf_.size()}; }
    void end_array(ArrayMark m) { uint32_t n = static_cast<uint32_t>(buf_.size() - m.start); std::memcpy(&buf_[m.len_at], &n, 4); }
    void variant_str(const std::string& s) { sig("s"); str(s); }
    void variant_bool(bool b) { sig("b"); boolean(b); }
    void variant_u32(uint32_t v) { sig("u"); u32(v); }
    void variant_obj(const std::string& s) { sig("o"); obj(s); }
    const std::vector<uint8_t>& data() const { return buf_; }
    size_t size() const { return buf_.size(); }
    std::vector<uint8_t>& raw() { return buf_; }

private:
    void put(const void* p, size_t n) { const uint8_t* b = static_cast<const uint8_t*>(p); buf_.insert(buf_.end(), b, b + n); }
    std::vector<uint8_t> buf_;
};

class Reader {
public:
    Reader(const uint8_t* d, size_t n, size_t base = 0) : d_(d), n_(n), base_(base) {}
    bool ok() const { return !err_; }
    void align(size_t n) { while ((base_ + pos_) % n && pos_ < n_) ++pos_; }
    uint8_t u8() { return pos_ < n_ ? d_[pos_++] : (err_ = true, 0); }
    uint32_t u32() { align(4); return get<uint32_t>(); }
    int32_t i32() { align(4); return get<int32_t>(); }
    std::string str() { uint32_t l = u32(); if (pos_ + l + 1 > n_) { err_ = true; return ""; } std::string s(reinterpret_cast<const char*>(d_ + pos_), l); pos_ += l + 1; return s; }
    std::string sig() { uint8_t l = u8(); if (pos_ + l + 1u > n_) { err_ = true; return ""; } std::string s(reinterpret_cast<const char*>(d_ + pos_), l); pos_ += l + 1u; return s; }
    void skip_value(const std::string& sig, size_t& i);   // skips one complete type starting at sig[i]
    size_t pos() const { return pos_; }
    void seek(size_t p) { pos_ = p; }
    size_t left() const { return n_ - pos_; }

private:
    template <class T> T get() { if (pos_ + sizeof(T) > n_) { err_ = true; return T(); } T v; std::memcpy(&v, d_ + pos_, sizeof(T)); pos_ += sizeof(T); return v; }
    const uint8_t* d_;
    size_t n_, pos_ = 0, base_;
    bool err_ = false;
};

enum MsgType : uint8_t { kMethodCall = 1, kMethodReturn = 2, kError = 3, kSignal = 4 };

struct Message {
    uint8_t type = 0, flags = 0;
    uint32_t serial = 0, reply_serial = 0;
    std::string path, iface, member, dest, sender, error, signature;
    std::vector<uint8_t> body;
};

class Connection {
public:
    ~Connection();
    bool connect(const std::string& address = "");   // "" = $DBUS_SESSION_BUS_ADDRESS or /run/user/<uid>/bus
    int fd() const { return fd_; }
    const std::string& unique_name() const { return name_; }
    bool request_name(const std::string& name);
    uint32_t send(Message m);
    // Sends a method call and waits for its reply; other traffic received meanwhile is queued for dispatch().
    bool call(Message m, Message* reply, int timeout_ms = 3000);
    bool add_match(const std::string& rule);
    // Reads whatever is available (non-blocking) into queued messages. Returns false on disconnect.
    bool pump();
    std::vector<Message> take();
    void reply_ok(const Message& req, const std::string& sig, const std::vector<uint8_t>& body);
    void reply_error(const Message& req, const std::string& name, const std::string& text);
    void emit(const std::string& path, const std::string& iface, const std::string& member, const std::string& sig = "", const std::vector<uint8_t>& body = {});

private:
    bool write_all(const uint8_t* p, size_t n);
    bool parse_available();
    int fd_ = -1;
    uint32_t serial_ = 1;
    std::string name_;
    std::vector<uint8_t> rx_;
    std::vector<Message> queue_;
};

std::vector<uint8_t> encode(const Message& m);
// Decodes one message from the front of `buf`; returns bytes consumed, 0 if incomplete, -1 if malformed.
long decode(const std::vector<uint8_t>& buf, Message* out);

}  // namespace tt::dbus
