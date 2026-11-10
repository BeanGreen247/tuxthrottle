#pragma once
#include <memory>
#include <string>

#include "app.hpp"

namespace tt::app {
std::unique_ptr<Page> make_dashboard();
std::unique_ptr<Page> make_about();
std::unique_ptr<Page> make_report();
std::unique_ptr<Page> make_category(const std::string& cat);
std::unique_ptr<Page> make_presets();
std::unique_ptr<Page> make_updates();
std::unique_ptr<Page> make_profiles();
std::unique_ptr<Page> make_drives();
std::unique_ptr<Page> make_fans();
std::unique_ptr<Page> make_games();
std::unique_ptr<Page> make_proton();
std::unique_ptr<Page> make_mangohud();
std::unique_ptr<Page> make_gametools();
std::unique_ptr<Page> make_keyboard();
std::unique_ptr<Page> make_vram();
std::unique_ptr<Page> make_power();
std::unique_ptr<Page> make_battery();
std::unique_ptr<Page> make_display();
std::unique_ptr<Page> make_touchpad();
}  // namespace tt::app
