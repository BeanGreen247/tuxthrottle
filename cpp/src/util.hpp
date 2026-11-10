// Small helpers shared by the core: strings, files, running commands.
#pragma once
#include <functional>
#include <optional>
#include <utility>
#include <regex>
#include <string>
#include <vector>

namespace tt {

std::string trim(const std::string& s);
std::vector<std::string> split_ws(const std::string& s);
std::vector<std::string> split_lines(const std::string& s);
std::string join(const std::vector<std::string>& v, const std::string& sep);
bool starts_with(const std::string& s, const std::string& p);
bool ends_with(const std::string& s, const std::string& p);
std::string replace_all(std::string s, const std::string& from, const std::string& to);
std::string lower(std::string s);

// POSIX single-quote quoting, like Python's shlex.quote.
std::string shell_quote(const std::string& s);

std::optional<std::string> read_file(const std::string& path);
bool write_file(const std::string& path, const std::string& data);
bool path_exists(const std::string& path);

struct RunResult {
    int code = -1;
    std::string out;  // stdout and stderr, interleaved
};
// Runs `bash -c cmd` and captures its output. No shell metacharacter filtering:
// callers pass trusted command strings (tweak configs) or quote with shell_quote.
RunResult run_shell(const std::string& cmd);

// Runs argv directly (no shell). stdout and stderr go into `out`; killed after timeout_ms (0 = none).
// code is -1 when the program could not start, 124 on timeout.
RunResult run_argv(const std::vector<std::string>& argv, int timeout_ms = 0);
// stdout only (stderr discarded) - for parsing tool output.
RunResult run_argv_stdout(const std::vector<std::string>& argv, int timeout_ms = 0);

// Filesystem helpers (glob(3) based, sorted).
std::vector<std::string> glob_paths(const std::string& pattern);
std::string read_trim(const std::string& path);            // "" when unreadable
std::optional<long long> read_int(const std::string& path);
std::string real_path(const std::string& path);
std::string base_name(const std::string& path);
std::string dir_name(const std::string& path);
bool write_text(const std::string& path, const std::string& text);  // open O_WRONLY (no create), sysfs-safe
std::string which(const std::string& cmd);                  // cached PATH lookup, "" when absent
std::string env_or(const char* name, const std::string& def = "");
double now_seconds();                                       // CLOCK_MONOTONIC
void sleep_ms(int ms);
std::string format_double(double v, int decimals);
// Regex replace where the replacement is computed from the match (avoids '$10'-style group ambiguity).
std::string regex_replace_fn(const std::string& text, const std::regex& re, const std::function<std::string(const std::smatch&)>& fn, bool first_only = false);
size_t utf8_len(const std::string& s);                      // code points
std::string utf8_trunc(const std::string& s, size_t n);     // first n code points
std::string utf8_pad(const std::string& s, size_t width);   // left-aligned, padded to `width` code points
bool is_root();

// Runs `bash -c cmd`, calling on_line for every output line (stdout+stderr). Returns the exit code
// (124 on timeout, -1 if it could not start).
int run_shell_stream(const std::string& cmd, const std::function<void(const std::string&)>& on_line, int timeout_ms = 0);
std::pair<bool, std::string> privileged_ctl(const std::vector<std::string>& args);   // tuxthrottlectl via pkexec, sudo -n, then direct
std::string ctl_exe();   // the tuxthrottlectl binary (self when running as ctl)
std::string self_exe();  // absolute path of the running binary

// Child process helpers (no capture: stdout/stderr go to /dev/null).
long spawn_quiet(const std::vector<std::string>& argv, const std::vector<std::string>& extra_env = {}, bool new_session = false);
bool child_running(long pid, int* exit_code);   // false once reaped
void child_stop(long pid, int wait_ms = 5000);  // SIGTERM, then SIGKILL after wait_ms

}  // namespace tt
