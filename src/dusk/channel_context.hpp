#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace dusk::channels {
void initialize(std::string_view requested = {});
void activate_runtime();
void poll(bool gameReady);
void shutdown();
bool request(std::string_view channel, std::string& error);
std::string status_json();
std::vector<std::string> names();
}  // namespace dusk::channels
