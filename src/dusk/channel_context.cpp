#include "channel_context.hpp"

#include "channel_catalog.hpp"
#include "config.hpp"
#include "data.hpp"
#include "logging.h"
#include "settings.h"
#if DUSK_ENABLE_LIVE_DEBUG
#include "live_debug.hpp"
#endif

#include <channel_context.h>
#include <SDL3/SDL_hints.h>
#include <nlohmann/json.hpp>

#include <cstdlib>
#include <fstream>
#include <memory>
#include <stdexcept>
#if defined(__APPLE__)
#include <TargetConditionals.h>
#endif

namespace dusk::channels {
namespace {
using Json = nlohmann::json;
using Runtime = std::unique_ptr<cc_runtime, decltype(&cc_runtime_destroy)>;
using Snapshot = std::unique_ptr<cc_snapshot, decltype(&cc_snapshot_destroy)>;
using Transition = std::unique_ptr<cc_transition, decltype(&cc_transition_destroy)>;

Runtime runtime{nullptr, cc_runtime_destroy};
Transition pending{nullptr, cc_transition_destroy};
bool active = false;

struct Features {
    bool liveControl;
    bool inputOverlay;
    bool gyroOverlay;
    bool gyroAim;
    bool fullControllersOnly;
};
Features applied{};

Features prepare(const Json& context) {
    const auto flag = [&](const char* path) {
        const auto& value = context.at(Json::json_pointer(path));
        if (!value.is_boolean()) {
            throw std::runtime_error(std::string("Expected boolean feature: ") + path);
        }
        return value.get<bool>();
    };
    Features result{flag("/features/diagnostics/liveControl/enabled"),
        flag("/features/diagnostics/inputOverlay/enabled"),
        flag("/features/diagnostics/inputOverlay/gyro"),
        flag("/features/input/gyroAim/enabled"),
        flag("/features/input/fullControllersOnly/enabled")};
#if !DUSK_ENABLE_LIVE_DEBUG
    if (result.liveControl) {
        throw std::runtime_error("This build lacks live-control capability");
    }
#endif
    if (active && result.fullControllersOnly != applied.fullControllersOnly) {
        throw std::runtime_error("Changing controller discovery policy requires an app restart");
    }
    return result;
}

void apply(const Features& next) {
    auto& settings = getSettings().game;
    settings.enableGyroAim.setOverrideValue(next.gyroAim);
    settings.showInputViewer.setOverrideValue(next.inputOverlay);
    settings.showInputViewerGyro.setOverrideValue(next.gyroOverlay);
#if DUSK_ENABLE_LIVE_DEBUG
    if (active && next.liveControl != applied.liveControl) {
        if (next.liveControl) {
            live_debug::initialize();
        } else {
            live_debug::shutdown();
        }
    }
#endif
    applied = next;
}
}  // namespace

void initialize(std::string_view requested) {
    shutdown();
    std::string selected(requested);
    if (selected.empty()) {
        if (const char* value = std::getenv("DUSK_CHANNEL")) {
            selected = value;
        }
    }
    if (selected.empty()) {
        std::ifstream source(data::base_path_relative("channel-output.json"));
        if (source) {
            Json output;
            source >> output;
            selected = output.at("channel").get<std::string>();
        }
    }
    runtime.reset(cc_runtime_create(kChannelCatalogJson, selected.empty() ? nullptr : selected.c_str()));
    if (!runtime) {
        throw std::runtime_error(cc_last_error());
    }
    Snapshot snapshot{cc_runtime_snapshot(runtime.get()), cc_snapshot_destroy};
    if (!snapshot) {
        throw std::runtime_error(cc_last_error());
    }
    const auto features = prepare(Json::parse(cc_snapshot_json(snapshot.get())));
#if defined(__APPLE__) && TARGET_OS_TV
    SDL_SetHint(SDL_HINT_TV_REMOTE_AS_JOYSTICK, features.fullControllersOnly ? "0" : "1");
#endif
    apply(features);
    DuskLog.info("Channel context initialized: {} generation {}", cc_snapshot_channel(snapshot.get()),
        cc_snapshot_generation(snapshot.get()));
}

void activate_runtime() {
    active = true;
#if DUSK_ENABLE_LIVE_DEBUG
    if (applied.liveControl) {
        live_debug::initialize();
    }
#endif
}

bool request(std::string_view channel, std::string& error) {
    if (!runtime || pending) {
        error = "Channel context is unavailable or a transition is already pending";
        return false;
    }
    const std::string selected(channel);
    Transition candidate{cc_runtime_prepare(runtime.get(), selected.empty() ? nullptr : selected.c_str()),
        cc_transition_destroy};
    if (!candidate) {
        error = cc_last_error();
        return false;
    }
    try {
        prepare(Json::parse(cc_transition_json(candidate.get())));
    } catch (const std::exception& exception) {
        error = exception.what();
        return false;
    }
    pending = std::move(candidate);
    return true;
}

void poll(bool gameReady) {
    if (pending) {
        const auto next = prepare(Json::parse(cc_transition_json(pending.get())));
        if (cc_runtime_commit(runtime.get(), pending.get())) {
            apply(next);
            DuskLog.info("Channel context committed: {} generation {} dirty={}",
                cc_transition_channel(pending.get()), cc_transition_generation(pending.get()),
                cc_transition_dirty_paths_json(pending.get()));
        } else {
            DuskLog.warn("Channel transition rejected: {}", cc_last_error());
        }
        pending.reset();
    }
#if DUSK_ENABLE_LIVE_DEBUG
    if (active && applied.liveControl) {
        live_debug::poll(gameReady);
    }
#endif
}

std::string status_json() {
    Snapshot snapshot{runtime ? cc_runtime_snapshot(runtime.get()) : nullptr, cc_snapshot_destroy};
    if (!snapshot) {
        return "{}";
    }
    auto context = Json::parse(cc_snapshot_json(snapshot.get()));
    context["generation"] = cc_snapshot_generation(snapshot.get());
    return context.dump();
}

std::vector<std::string> names() {
    std::vector<std::string> result;
    const auto catalog = Json::parse(kChannelCatalogJson);
    for (const auto& [name, value] : catalog.at("channels").items()) {
        result.push_back(name);
    }
    return result;
}

void shutdown() {
#if DUSK_ENABLE_LIVE_DEBUG
    if (active && applied.liveControl) {
        live_debug::shutdown();
    }
#endif
    active = false;
    pending.reset();
    runtime.reset();
    applied = {};
}
}  // namespace dusk::channels
