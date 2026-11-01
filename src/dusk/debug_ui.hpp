#pragma once

#include <nlohmann/json.hpp>

namespace dusk::debug_ui {
nlohmann::json request(const nlohmann::json& params);
}
