// Small, dependency-free readers/writers for the file formats the tool touches:
// Valve KeyValues (VDF), a YAML subset (Lutris configs), base64, POSIX shlex, and read-only SQLite.
#pragma once
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace tt {

std::string base64_decode(const std::string& in);   // throws std::invalid_argument on bad input
std::string base64_encode(const std::string& in);
// POSIX-ish shlex.split (quotes, backslash escapes). Throws std::invalid_argument on an unterminated quote.
std::vector<std::string> shlex_split(const std::string& s);

// ---- VDF (Valve KeyValues) ----------------------------------------------------------------
class Vdf {
public:
    using Children = std::vector<std::pair<std::string, Vdf>>;  // keeps order, allows duplicate keys

    Vdf() = default;
    explicit Vdf(std::string value) : is_block_(false), value_(std::move(value)) {}
    static Vdf block() { Vdf v; v.is_block_ = true; return v; }

    bool is_block() const { return is_block_; }
    const std::string& str() const { return value_; }
    void set_str(std::string v) { is_block_ = false; value_ = std::move(v); }
    Children& children() { return children_; }
    const Children& children() const { return children_; }

    // case-insensitive child lookup (Steam mixes Valve/valve); nullptr when absent
    Vdf* find_ci(const std::string& key);
    const Vdf* find_ci(const std::string& key) const;
    Vdf& set(const std::string& key, Vdf v);              // replace first match (exact key) or append
    bool erase_ci(const std::string& key);

    static Vdf parse(const std::string& text);            // top level becomes a block; throws std::runtime_error
    std::string dump() const;                              // tab-indented, Valve style

private:
    void dump_block(std::string& out, int depth) const;
    bool is_block_ = true;
    std::string value_;
    Children children_;
};

// ---- YAML subset ---------------------------------------------------------------------------
// Block mappings / sequences, plain + quoted scalars, empty {} and [], comments. Enough for
// PyYAML-written Lutris configs. Scalars keep their raw text plus a `quoted` flag so booleans,
// numbers and null survive a round trip.
struct Yaml {
    enum class Kind { Null, Scalar, Map, Seq };
    Kind kind = Kind::Null;
    std::string scalar;      // Kind::Scalar: the text; quoted = true for '...' / "..."
    bool quoted = false;
    std::vector<std::pair<std::string, Yaml>> map;   // order kept
    std::vector<Yaml> seq;

    static Yaml make_map() { Yaml y; y.kind = Kind::Map; return y; }
    static Yaml make_seq() { Yaml y; y.kind = Kind::Seq; return y; }
    static Yaml make_scalar(std::string s, bool q = false) { Yaml y; y.kind = Kind::Scalar; y.scalar = std::move(s); y.quoted = q; return y; }
    static Yaml make_bool(bool b) { return make_scalar(b ? "true" : "false"); }

    bool is_map() const { return kind == Kind::Map; }
    bool is_seq() const { return kind == Kind::Seq; }
    bool is_null() const { return kind == Kind::Null; }
    bool is_true() const { return kind == Kind::Scalar && !quoted && (scalar == "true" || scalar == "True" || scalar == "yes"); }
    bool truthy() const;                                  // YAML truthiness (non-empty / non-zero / true)
    Yaml* get(const std::string& key);
    const Yaml* get(const std::string& key) const;
    Yaml& set(const std::string& key, Yaml v);
    bool erase(const std::string& key);
    std::string str() const { return kind == Kind::Scalar ? scalar : ""; }

    static Yaml parse(const std::string& text);          // throws std::runtime_error
    std::string dump() const;                             // block style, like yaml.safe_dump(default_flow_style=False, sort_keys=False)
};

// ---- Read-only SQLite --------------------------------------------------------------------
struct SqlValue {
    std::variant<std::monostate, long long, double, std::string> v;
    bool is_null() const { return std::holds_alternative<std::monostate>(v); }
    std::string str() const;
    long long integer(long long def = 0) const;
};

class SqliteReader {
public:
    explicit SqliteReader(const std::string& path);
    bool ok() const { return ok_; }
    // All rows of a table with its column names; empty when the table is missing.
    struct Table { std::vector<std::string> columns; std::vector<std::vector<SqlValue>> rows; };
    Table read_table(const std::string& name) const;

private:
    struct Impl;
    std::shared_ptr<Impl> impl_;
    bool ok_ = false;
};

}  // namespace tt
