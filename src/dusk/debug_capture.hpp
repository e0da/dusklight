#pragma once

#include <nlohmann/json_fwd.hpp>

namespace dusk::debug_capture {

nlohmann::json request(const nlohmann::json& params);
void reset();

}  // namespace dusk::debug_capture
