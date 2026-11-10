// G-key listener (evdev) and the terminal dashboard (TUI). Neither needs anything beyond libc.
#pragma once
#include <string>
#include <vector>

namespace tt::hotkey {
// Runs forever (systemd --user service). TUXTHROTTLE_HOTKEY_MODE=single|double, TUXTHROTTLE_HOTKEY_DOUBLE_MS.
int main(const std::vector<std::string>& args);
// Pure: decide whether a key-down at `ts` fires the toggle. state = last first-tap time (0 = none).
bool double_tap_fires(double ts, double* last_press, double window_s);
}  // namespace tt::hotkey

namespace tt::tui {
int main(const std::vector<std::string>& args);
}
