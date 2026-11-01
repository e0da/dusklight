#include "live_debug.hpp"

#include "commands.hpp"
#include "d/d_com_inf_game.h"
#include "f_op/f_op_actor.h"
#include "game_clock.h"

#include <SDL3/SDL_timer.h>
#include <borealis/log.hpp>
#include <borealis/ws.hpp>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace dusk::live_debug {
namespace {

using Json = nlohmann::json;
constexpr borealis::Log Log{"live_debug"};
constexpr size_t kMaxMessageBytes = 16 * 1024;
constexpr size_t kMaxOutputBytes = 2048;
constexpr size_t kMaxOutputLines = 128;
constexpr unsigned kMaxEventsPerFrame = 4;

borealis::ws::Connection connection;
std::optional<borealis::ws::Options> connectionOptions;
uint64_t nextConnectNs = 0;
unsigned retrySeconds = 1;
CommandState commandState;
unsigned subscriptionHz = 0;
uint64_t sequence = 0;
uint64_t dropped = 0;
uint64_t lastPollNs = 0;
uint64_t nextTelemetryNs = 0;

void reset_session() {
    commandState = {};
    subscriptionHz = 0;
    sequence = 0;
    dropped = 0;
    lastPollNs = 0;
    nextTelemetryNs = 0;
}

void schedule_retry(uint64_t now) {
    connection.close(1000, "Session ended");
    connection = {};
    reset_session();
    nextConnectNs = now + retrySeconds * 1'000'000'000ULL;
    retrySeconds = std::min(retrySeconds * 2, 30u);
}

bool send_response(Json response) {
    const auto serialized = response.dump(-1, ' ', false, Json::error_handler_t::replace);
    if (serialized.size() <= kMaxMessageBytes && connection.send(borealis::ws::MessageKind::Text,
                                                     serialized) == borealis::ws::SendResult::Ok)
    {
        return true;
    }
    connection.close(1013, "Response queue exhausted");
    schedule_retry(SDL_GetTicksNS());
    return false;
}

void reply_error(const Json& id, std::string_view code, std::string_view message) {
    send_response(
        {{"type", "response"}, {"id", id}, {"error", {{"code", code}, {"message", message}}}});
}

void reply(const Json& id, Json result) {
    send_response({{"type", "response"}, {"id", id}, {"result", std::move(result)}});
}

void handle_request(const std::string& data, bool gameReady) {
    const auto request = Json::parse(data, nullptr, false);
    if (!request.is_object() || !request.contains("id") || !request["id"].is_string() ||
        request["id"].get_ref<const std::string&>().size() > 128)
    {
        reply_error(nullptr, "invalid_request", "A string id of at most 128 bytes is required");
        return;
    }
    const auto& id = request["id"];
    if (!request.contains("method") || !request["method"].is_string() ||
        (request.contains("params") && !request["params"].is_object()))
    {
        reply_error(id, "invalid_request", "Expected a method string and params object");
        return;
    }
    const auto& method = request["method"].get_ref<const std::string&>();
    const Json params = request.value("params", Json::object());
    if (method == "ping") {
        reply(id, {{"pong", true}});
    } else if (method == "subscribe") {
        double hz = 10;
        if (params.contains("hz")) {
            if (!params["hz"].is_number()) {
                reply_error(id, "invalid_params", "hz must be an integer from 1 to 10");
                return;
            }
            hz = params["hz"].get<double>();
        }
        if (!std::isfinite(hz) || hz < 1 || hz > 10 || std::floor(hz) != hz) {
            reply_error(id, "invalid_params", "hz must be an integer from 1 to 10");
            return;
        }
        subscriptionHz = static_cast<unsigned>(hz);
        nextTelemetryNs = 0;
        reply(id, {{"subscribed", true}, {"hz", subscriptionHz}});
    } else if (method == "unsubscribe") {
        subscriptionHz = 0;
        reply(id, {{"subscribed", false}});
    } else if (method == "command") {
        if (!gameReady) {
            reply_error(id, "not_ready", "The game has not launched");
            return;
        }
        if (!params.contains("line") || !params["line"].is_string() ||
            params["line"].get_ref<const std::string&>().size() > 4096)
        {
            reply_error(id, "invalid_params", "line must be a string of at most 4096 bytes");
            return;
        }
        std::vector<std::string> lines;
        size_t bytes = 0;
        bool truncated = false;
        runCommand(
            params["line"].get_ref<const std::string&>(), commandState, [&](std::string line) {
                if (truncated || lines.size() >= kMaxOutputLines ||
                    line.size() > kMaxOutputBytes - bytes)
                {
                    truncated = true;
                    return;
                }
                bytes += line.size();
                lines.push_back(std::move(line));
            });
        reply(id, {{"lines", lines}, {"truncated", truncated}, {"completed", true}});
    } else {
        reply_error(id, "unknown_method", "Unknown method");
    }
}

void send_telemetry(bool gameReady, uint64_t now, uint64_t elapsed) {
    Json data{{"timestampNs", now}, {"pollIntervalMs", elapsed / 1.0e6},
        {"pollFps", elapsed ? 1.0e9 / elapsed : 0.0}, {"gameReady", gameReady}};
    if (gameReady) {
        data["simRate"] = game_clock::get_sim_rate();
        if (const auto* player = dComIfGp_getPlayer(0)) {
            data["stage"] = dComIfGp_getStartStageName();
            data["room"] = player->current.roomNo;
            data["player"] = {{"x", player->current.pos.x}, {"y", player->current.pos.y},
                {"z", player->current.pos.z}};
        }
    }
    const auto serialized = Json{{"type", "event"}, {"event", "telemetry"}, {"seq", ++sequence},
        {"dropped", dropped},
        {"data",
            std::move(data)}}.dump();
    if (connection.send(borealis::ws::MessageKind::Text, serialized) !=
        borealis::ws::SendResult::Ok)
    {
        ++dropped;
    }
}

}  // namespace

void initialize() {
    shutdown();
    const char* url = std::getenv("DUSK_DEBUG_URL");
    const char* token = std::getenv("DUSK_DEBUG_TOKEN");
    if (url == nullptr || *url == '\0' || token == nullptr || *token == '\0') {
        return;
    }
    const std::string_view endpoint{url};
    const std::string_view secret{token};
    if (endpoint.size() > 2048 ||
        (!endpoint.starts_with("ws://") && !endpoint.starts_with("wss://")) ||
        secret.size() > 1024 || secret.find_first_of("\r\n") != std::string_view::npos)
    {
        Log.warn("Invalid live debug endpoint or token");
        return;
    }
    borealis::ws::Options options;
    options.url = endpoint;
    options.headers.push_back({"Authorization", "Bearer " + std::string{secret}});
    options.allowPlaintext = endpoint.starts_with("ws://");
    options.maxMessageBytes = kMaxMessageBytes;
    options.maxQueuedBytes = 64 * 1024;
    options.maxSendQueueBytes = 128 * 1024;
    connectionOptions = std::move(options);
}

void poll(bool gameReady) {
    const uint64_t now = SDL_GetTicksNS();
    if (!connection) {
        if (!connectionOptions || now < nextConnectNs) {
            return;
        }
        reset_session();
        connection = borealis::ws::connect(*connectionOptions);
        if (!connection) {
            Log.warn("Live debug connection could not start");
            schedule_retry(now);
            return;
        }
    }
    const uint64_t elapsed = lastPollNs ? now - lastPollNs : 0;
    lastPollNs = now;
    borealis::ws::Event event;
    for (unsigned count = 0; count < kMaxEventsPerFrame && connection.poll(event); ++count) {
        if (event.kind == borealis::ws::Event::Kind::Closed) {
            Log.info("Live debug connection closed: error={} status={} code={}",
                static_cast<int>(event.error), event.status, event.code);
            schedule_retry(now);
            return;
        }
        if (event.kind == borealis::ws::Event::Kind::Open) {
            retrySeconds = 1;
            send_response({{"type", "hello"}, {"version", 1}});
        } else if (event.messageKind != borealis::ws::MessageKind::Text) {
            reply_error(nullptr, "invalid_request", "Text JSON messages are required");
        } else {
            handle_request(event.data, gameReady);
        }
        if (connection.state() != borealis::ws::Connection::State::Open) {
            return;
        }
    }
    if (subscriptionHz && connection.state() == borealis::ws::Connection::State::Open &&
        now >= nextTelemetryNs)
    {
        nextTelemetryNs = now + 1'000'000'000ULL / subscriptionHz;
        send_telemetry(gameReady, now, elapsed);
    }
}

void shutdown() {
    connectionOptions.reset();
    nextConnectNs = 0;
    retrySeconds = 1;
    connection.close(1000, "Shutdown");
    connection = {};
    reset_session();
}

}  // namespace dusk::live_debug
