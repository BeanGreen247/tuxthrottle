// Hardware tooling: model scaffold generator (modelgen) and the Curve Optimizer stress/auto-revert harness (co_stress).
#pragma once
#include <string>
#include <vector>

#include "json.hpp"

namespace tt::modelgen {
std::string slugify(const std::string& s);
Json build_scaffold(const std::string& slug = "");
}  // namespace tt::modelgen

namespace tt::co {
// Every command returns a process exit code. `user` selects whose co.json to use ("" = detect).
int apply(int offset, double minutes, bool no_gpu, const std::string& user);
int confirm(const std::string& user);
int revert(const std::string& user);
int reapply(const std::string& user);
int status(const std::string& user);
}  // namespace tt::co
