#include "debug_ui.hpp"

#include "ui/menu_bar.hpp"
#include "ui/ui.hpp"

#include <stdexcept>
#include <string>

namespace dusk::debug_ui {
nlohmann::json request(const nlohmann::json& params) {
    if (!params.contains("action") || !params["action"].is_string()) {
        throw std::invalid_argument("UI action must be menu, channel, close or status");
    }
    const auto action = params["action"].get<std::string>();
    if (action != "menu" && action != "channel" && action != "close" && action != "status") {
        throw std::invalid_argument("UI action must be menu, channel, close or status");
    }
    auto* menu = dynamic_cast<ui::MenuBar*>(ui::find_document(ui::DocumentScope::MenuBar));
    if (menu == nullptr) {
        throw std::runtime_error("Menu is not initialized");
    }
    if (action == "close") {
        ui::close_all_documents();
    } else if (action == "menu" || action == "channel") {
        ui::close_all_documents();
        menu->show();
        if (action == "channel") {
            menu->open_channel_menu();
        }
    }
    return {{"action", action}, {"menuVisible", menu->visible()},
        {"documentVisible", ui::any_document_visible()}};
}
}  // namespace dusk::debug_ui
