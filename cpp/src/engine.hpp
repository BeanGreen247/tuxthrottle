// The tweak/app engine: Item model, config loading + gating, status evaluation, the apply ledger,
// and the apply / undo / preset runners (port of tuxthrottle_items.py + the worker logic of tuxthrottle.py).
#pragma once
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::engine {

using LogFn = std::function<void(const std::string&)>;

struct Item {
    std::string id, kind;  // kind: tweak | app
    std::string content, description, category, risk = "safe";
    bool recommended = false;
    std::string requires_vendor;
    std::vector<std::string> requires_models;
    bool hw_supported = true, hidden = false;
    std::string check_cmd, check_pending_cmd;
    std::vector<std::string> apply_cmds, undo_cmds;
    std::vector<std::string> reinstall_if;  // tweak option, raw (unexpanded) globs
    std::string state = "unknown";          // applied pending not_applied error drifted failed unsupported unknown
    std::optional<int> check_rc;
    std::string check_out;
    Json ledger;  // {action, ok, ts, note} or null

    bool applied() const { return state == "applied"; }
    bool pending() const { return state == "pending"; }
    bool done() const { return state == "applied" || state == "pending"; }
};

std::string config_dir();                          // <app>/config
std::string substitute(const std::string& cmd, const std::string& user);  // {USER} {TOOLKIT_DIR} {TT} + distro placeholders
Item make_item(const std::string& id, const Json& data, const std::string& kind, const std::string& user);
std::vector<Item> load_items(const std::string& user = "");   // tweaks + apps, fully gated
Json load_presets();

// ledger (~/.config/tuxthrottle/state.json)
Json ledger_load();
void ledger_record(const std::string& id, const std::string& action, bool ok, const std::string& note = "");

void evaluate_item(Item& it, const Json& ledger);
void evaluate_all(std::vector<Item>& items, int workers = 12);
std::string format_status_report(const std::vector<Item>& items);
std::string state_label(const Item& it);          // "Applied" / "Installed" / "Pending reboot" ...

// runners
bool apply_item(const Item& it, const LogFn& log);
bool undo_item(const Item& it, const LogFn& log);
void pre_risky_snapshot(const std::string& label, const LogFn& log);
std::optional<std::string> arm_watchdog_if_risky(const std::vector<const Item*>& items, int seconds, const std::string& user, const LogFn& log);

struct BatchResult { int applied = 0, skipped = 0, failed = 0; std::string watchdog_unit; };
// Applies the not-yet-done supported items in order, snapshotting first (the "Apply Selected" / preset worker).
BatchResult apply_batch(std::vector<Item>& items, const std::vector<std::string>& ids, const std::string& snapshot_label, const LogFn& log,
                        const std::string& user = "");
BatchResult undo_batch(std::vector<Item>& items, const std::vector<std::string>& ids, const LogFn& log, const std::string& user = "");
std::vector<const Item*> recommended(const std::vector<Item>& items, const std::string& category = "", bool pending_only = false);

// version / metadata helpers
std::string toolkit_version();
std::string dnf_metadata_age();
bool dnf_metadata_stale(double threshold_secs = 21600.0);

// CLI: tweaks list|status|report|apply|undo|apply-recommended, preset list|apply, apply-tweak
int cli_tweaks(const std::vector<std::string>& args);
int cli_preset(const std::vector<std::string>& args);
int cli_apply_tweak(const std::vector<std::string>& args);

}  // namespace tt::engine
