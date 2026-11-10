// Boot-argument editing (pure logic) plus the GRUB default-file transform.
#pragma once
#include <string>
#include <vector>

namespace tt::kargs {

// Adding `key=val` replaces any token with the same key; adding a bare word
// appends it if absent. Deleting `key` removes every `key[=...]`; deleting
// `key=val` removes only that exact token.
std::vector<std::string> edit_tokens(std::vector<std::string> tokens, const std::vector<std::string>& add,
                                     const std::vector<std::string>& del);

// True when every wanted entry is present (`key=val` exact, bare `key` any value).
bool has_tokens(const std::vector<std::string>& tokens, const std::vector<std::string>& want);

// /etc/default/grub GRUB_CMDLINE_LINUX_DEFAULT handling.
std::vector<std::string> grub_tokens(const std::string& text);
std::string grub_edit(const std::string& text, const std::vector<std::string>& add,
                      const std::vector<std::string>& del);

}  // namespace tt::kargs
