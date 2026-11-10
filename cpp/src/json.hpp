// Minimal JSON value + parser + serializer. No external dependencies.
#pragma once
#include <memory>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace tt {

class Json {
public:
    using Array = std::vector<Json>;
    using Object = std::vector<std::pair<std::string, Json>>;  // keeps file order

    Json() = default;
    Json(std::nullptr_t) {}
    Json(bool b) : v_(b) {}
    Json(double d) : v_(d) {}
    Json(int i) : v_(static_cast<double>(i)) {}
    Json(const char* s) : v_(std::string(s)) {}
    Json(std::string s) : v_(std::move(s)) {}
    Json(Array a) : v_(std::move(a)) {}
    Json(Object o) : v_(std::move(o)) {}

    bool is_null() const { return std::holds_alternative<std::monostate>(v_); }
    bool is_bool() const { return std::holds_alternative<bool>(v_); }
    bool is_number() const { return std::holds_alternative<double>(v_); }
    bool is_string() const { return std::holds_alternative<std::string>(v_); }
    bool is_array() const { return std::holds_alternative<Array>(v_); }
    bool is_object() const { return std::holds_alternative<Object>(v_); }

    bool as_bool(bool def = false) const { return is_bool() ? std::get<bool>(v_) : def; }
    double as_number(double def = 0) const { return is_number() ? std::get<double>(v_) : def; }
    const std::string& as_string() const;           // "" when not a string
    std::string str(const std::string& def = "") const { return is_string() ? std::get<std::string>(v_) : def; }
    // On a temporary Json these return by value, so `for (x : f().array())` never dangles.
    const Array& array() const&;                     // empty when not an array
    Array array() &&;
    const Object& object() const&;                   // empty when not an object
    Object object() &&;

    // object lookup; returns a shared null Json when missing or not an object
    const Json& operator[](const std::string& key) const&;
    Json operator[](const std::string& key) &&;
    bool has(const std::string& key) const;
    Array& make_array();
    Object& make_object();

    // Mutation helpers (objects keep insertion order; set() replaces an existing key in place).
    Json& set(const std::string& key, Json value);
    void push(Json value) { make_array().push_back(std::move(value)); }
    bool erase(const std::string& key);
    static Json obj() { return Json(Object{}); }
    static Json arr() { return Json(Array{}); }

    // sort_keys orders every object's keys (like Python's json.dumps(sort_keys=True)).
    std::string dump(int indent = 0, bool sort_keys = false) const;

    // Throws std::runtime_error with a position on malformed input.
    static Json parse(const std::string& text);

private:
    void dump_to(std::string& out, int indent, int depth, bool sort_keys) const;
    std::variant<std::monostate, bool, double, std::string, Array, Object> v_;
};

}  // namespace tt
