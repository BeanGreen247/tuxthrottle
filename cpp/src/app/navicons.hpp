#pragma once
#include <string>

#include "../ui/ui.hpp"

namespace tt::app {
// Draws the sidebar icon for a page (vector shapes, no font dependency) inside `r`.
void draw_nav_icon(ui::Ui& ui, const std::string& title, ui::Rect r, bool active);
}  // namespace tt::app
