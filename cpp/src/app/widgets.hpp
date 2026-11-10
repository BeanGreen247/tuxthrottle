// Dashboard-style widgets built on the Ui toolkit.
#pragma once
#include <deque>
#include <string>
#include <vector>

#include "../ui/ui.hpp"

namespace tt::app {

struct Threshold { double value; ui::Color color; std::string label; };

// 270 degree ring gauge with value, unit and caption underneath.
void ring_gauge(ui::Ui& ui, const std::string& caption, const std::string& unit, double value, double maximum, ui::Color color, int decimals, int size);
// Rolling sparkline with title, current value, and optional reference lines.
void history_chart(ui::Ui& ui, const std::string& caption, const std::deque<double>& data, ui::Color color, const std::vector<Threshold>& thr, int w, int h);

// Swatch + hex field + RGB sliders. `hex` is "#rrggbb". Returns true when the colour changed.
bool color_edit(ui::Ui& ui, const std::string& key, std::string* hex);
ui::Color parse_hex(const std::string& s, ui::Color def = ui::Color(255, 255, 255));

}  // namespace tt::app
