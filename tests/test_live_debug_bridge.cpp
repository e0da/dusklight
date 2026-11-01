#include "dusk/commands.hpp"
#include "dusk/live_debug.hpp"

#include <SDL3/SDL_timer.h>
#include <borealis/log.hpp>
#include <borealis/ws.hpp>
#include <nlohmann/json.hpp>

#include <cassert>
#include <cstdlib>
#include <deque>
#include <string>
#include <utility>
#include <vector>

namespace {
unsigned connectCalls = 0;
bool connected = false;
bool openPending = false;
uint64_t nowNs = 1;
std::vector<std::string> sent;
std::deque<borealis::ws::Event> incoming;
unsigned commandCalls = 0;
bool sendQueueFull = false;
bool failConnect = false;
bool emptyConnect = false;

void enqueue_closed() {
    borealis::ws::Event event;
    event.kind = borealis::ws::Event::Kind::Closed;
    event.error = borealis::ws::Error::Timeout;
    event.status = 503;
    event.code = 1006;
    incoming.push_back(std::move(event));
}

void enqueue(std::string message) {
    borealis::ws::Event event;
    event.kind = borealis::ws::Event::Kind::Message;
    event.messageKind = borealis::ws::MessageKind::Text;
    event.data = std::move(message);
    incoming.push_back(std::move(event));
}

nlohmann::json request(std::string message, bool gameReady = false) {
    sent.clear();
    enqueue(std::move(message));
    dusk::live_debug::poll(gameReady);
    assert(!sent.empty());
    return nlohmann::json::parse(sent.front());
}

void expect_error(std::string message, const char* code, bool gameReady = false) {
    const auto response = request(std::move(message), gameReady);
    assert(response["type"] == "response");
    assert(response["error"]["code"] == code);
}
}  // namespace

Uint64 SDL_GetTicksNS() {
    return nowNs;
}

namespace borealis::ws {
Connection::Connection(Connection&&) noexcept = default;
Connection& Connection::operator=(Connection&&) noexcept = default;
Connection::~Connection() = default;
Connection::operator bool() const noexcept {
    return connected;
}
Connection::State Connection::state() const noexcept {
    return connected ? State::Open : State::Closed;
}
bool Connection::poll(Event& event) {
    if (std::exchange(openPending, false)) {
        event.kind = Event::Kind::Open;
        return true;
    }
    if (!incoming.empty()) {
        event = std::move(incoming.front());
        incoming.pop_front();
        return true;
    }
    return false;
}
SendResult Connection::send(MessageKind, std::string_view data) {
    if (sendQueueFull) {
        return SendResult::QueueFull;
    }
    assert(data.size() <= 16 * 1024);
    sent.emplace_back(data);
    return SendResult::Ok;
}
CloseResult Connection::close(uint16_t, std::string_view) {
    connected = false;
    openPending = false;
    incoming.clear();
    return CloseResult::Ok;
}
Connection connect(Options options) {
    assert(!connected);
    assert(options.url == "ws://127.0.0.1:9876");
    assert(options.headers.front().value == "Bearer test-token");
    ++connectCalls;
    if (emptyConnect) {
        return {};
    }
    connected = true;
    openPending = !failConnect;
    if (failConnect) {
        enqueue_closed();
    }
    return {};
}
}  // namespace borealis::ws

namespace borealis::log::detail {
bool enabled(borealis::LogLevel, std::string_view) noexcept {
    return false;
}
void write(borealis::LogLevel, std::string_view, std::string_view) {}
[[noreturn]] void write_fatal(std::string_view, std::string_view) {
    std::abort();
}
}  // namespace borealis::log::detail

namespace dusk {
void runCommand(std::string_view line, CommandState& state, const CommandOutput& output) {
    ++commandCalls;
    if (line == "state") {
        output(std::to_string(state.foundProcId++));
    } else if (line == "many") {
        for (unsigned index = 0; index < 200; ++index) {
            output("line");
        }
    } else if (line == "large") {
        output(std::string(2000, '\x01'));
        output(std::string(1000, 'x'));
    } else {
        assert(line == "test");
        output("first line");
        output("second line");
    }
}
namespace game_clock {
float get_sim_rate() {
    return 30.0f;
}
}  // namespace game_clock
}  // namespace dusk

class fopAc_ac_c;
fopAc_ac_c* dComIfGp_getPlayer(int) {
    return nullptr;
}
const char* dComIfGp_getStartStageName() {
    return "";
}

int main() {
    setenv("DUSK_DEBUG_URL", "ws://127.0.0.1:9876", 1);
    setenv("DUSK_DEBUG_TOKEN", "test-token", 1);

    dusk::live_debug::initialize();
    assert(connectCalls == 0);
    assert(sent.empty());
    dusk::live_debug::shutdown();
    dusk::live_debug::poll(false);
    assert(connectCalls == 0);

    dusk::live_debug::initialize();
    nowNs += 60'000'000'000ULL;
    assert(connectCalls == 0);
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);
    assert(sent.size() == 1);
    const auto hello = nlohmann::json::parse(sent.front());
    assert(hello["type"] == "hello");
    assert(hello["version"] == 1);
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);
    assert(sent.size() == 1);

    auto response = request(R"({"id":"ping-1","method":"ping"})");
    assert(response["id"] == "ping-1");
    assert(response["result"]["pong"] == true);
    expect_error("{", "invalid_request");
    expect_error(R"({"id":"bad","method":7})", "invalid_request");
    expect_error(R"({"id":"bad","method":"ping","params":[]})", "invalid_request");
    expect_error(R"({"id":"unknown","method":"missing"})", "unknown_method");
    expect_error(R"({"id":"command","method":"command","params":{"line":"test"}})", "not_ready");
    assert(commandCalls == 0);
    expect_error(
        R"({"id":"bad","method":"command","params":{"line":false}})", "invalid_params", true);
    response = request(R"({"id":"command-ok","method":"command","params":{"line":"test"}})", true);
    assert(response["id"] == "command-ok");
    assert(response["result"]["completed"] == true);
    assert(response["result"]["truncated"] == false);
    assert(response["result"]["lines"] == nlohmann::json::array({"first line", "second line"}));
    response = request(R"({"id":"many","method":"command","params":{"line":"many"}})", true);
    assert(response["result"]["truncated"] == true);
    assert(response["result"]["lines"].size() == 128);
    response = request(R"({"id":"large","method":"command","params":{"line":"large"}})", true);
    assert(response["result"]["truncated"] == true);
    assert(response["result"]["lines"].size() == 1);
    assert(commandCalls == 3);

    expect_error(R"({"id":"bad-rate","method":"subscribe","params":{"hz":11}})", "invalid_params");
    expect_error(R"({"id":"bad-rate","method":"subscribe","params":{"hz":0}})", "invalid_params");
    response = request(R"({"id":"sub","method":"subscribe","params":{"hz":10}})");
    assert(response["result"]["subscribed"] == true);
    assert(response["result"]["hz"] == 10);
    assert(sent.size() == 2);
    auto telemetry = nlohmann::json::parse(sent.back());
    assert(telemetry["event"] == "telemetry");
    assert(telemetry["data"]["gameReady"] == false);
    assert(!telemetry["data"].contains("player"));
    sent.clear();
    nowNs += 99'000'000;
    dusk::live_debug::poll(true);
    assert(sent.empty());
    nowNs += 1'000'000;
    dusk::live_debug::poll(true);
    assert(sent.size() == 1);
    telemetry = nlohmann::json::parse(sent.back());
    assert(telemetry["seq"] == 2);
    assert(telemetry["data"]["gameReady"] == true);
    assert(telemetry["data"]["simRate"] == 30);
    sendQueueFull = true;
    nowNs += 100'000'000;
    dusk::live_debug::poll(true);
    assert(connected);
    sendQueueFull = false;
    nowNs += 100'000'000;
    dusk::live_debug::poll(true);
    telemetry = nlohmann::json::parse(sent.back());
    assert(telemetry["seq"] == 4);
    assert(telemetry["dropped"] == 1);
    response = request(R"({"id":"unsub","method":"unsubscribe"})");
    assert(response["result"]["subscribed"] == false);
    sent.clear();
    nowNs += 1'000'000'000;
    dusk::live_debug::poll(false);
    assert(sent.empty());

    for (unsigned index = 0; index < 7; ++index) {
        enqueue(R"({"id":"queued","method":"ping"})");
    }
    dusk::live_debug::poll(false);
    assert(sent.size() == 4);
    assert(incoming.size() == 3);
    dusk::live_debug::poll(false);
    assert(sent.size() == 7);
    assert(incoming.empty());
    sendQueueFull = true;
    enqueue(R"({"id":"full","method":"ping"})");
    dusk::live_debug::poll(false);
    assert(!connected);
    sendQueueFull = false;
    dusk::live_debug::shutdown();
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);

    unsetenv("DUSK_DEBUG_TOKEN");
    dusk::live_debug::initialize();
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);

    setenv("DUSK_DEBUG_TOKEN", "test-token", 1);
    dusk::live_debug::initialize();
    dusk::live_debug::poll(true);
    assert(connectCalls == 2);
    response = request(R"({"id":"state","method":"command","params":{"line":"state"}})", true);
    assert(response["result"]["lines"][0] == "0");
    request(R"({"id":"sub","method":"subscribe"})", true);
    enqueue_closed();
    dusk::live_debug::poll(true);
    assert(!connected);
    nowNs += 999'999'999;
    dusk::live_debug::poll(true);
    assert(connectCalls == 2);
    sent.clear();
    nowNs += 1;
    dusk::live_debug::poll(true);
    assert(connectCalls == 3);
    assert(sent.size() == 1);
    assert(nlohmann::json::parse(sent.front())["type"] == "hello");
    sent.clear();
    nowNs += 1'000'000'000;
    dusk::live_debug::poll(true);
    assert(sent.empty());
    response = request(R"({"id":"state","method":"command","params":{"line":"state"}})", true);
    assert(response["result"]["lines"][0] == "0");

    failConnect = true;
    enqueue_closed();
    dusk::live_debug::poll(true);
    for (const unsigned seconds : {1u, 2u, 4u, 8u, 16u, 30u, 30u}) {
        const auto previousCalls = connectCalls;
        nowNs += seconds * 1'000'000'000ULL - 1;
        dusk::live_debug::poll(true);
        assert(connectCalls == previousCalls);
        nowNs += 1;
        dusk::live_debug::poll(true);
        assert(connectCalls == previousCalls + 1);
        assert(!connected);
    }
    failConnect = false;
    nowNs += 30'000'000'000ULL;
    dusk::live_debug::poll(true);
    assert(connected);
    const auto recoveredCalls = connectCalls;
    enqueue_closed();
    dusk::live_debug::poll(true);
    nowNs += 1'000'000'000;
    dusk::live_debug::poll(true);
    assert(connectCalls == recoveredCalls + 1);
    assert(connected);

    emptyConnect = true;
    enqueue_closed();
    dusk::live_debug::poll(true);
    nowNs += 1'000'000'000;
    dusk::live_debug::poll(true);
    assert(!connected);
    const auto failedStartCalls = connectCalls;
    nowNs += 1'999'999'999;
    dusk::live_debug::poll(true);
    assert(connectCalls == failedStartCalls);
    nowNs += 1;
    dusk::live_debug::poll(true);
    assert(connectCalls == failedStartCalls + 1);
    dusk::live_debug::shutdown();
    nowNs += 300'000'000'000ULL;
    dusk::live_debug::poll(true);
    assert(connectCalls == failedStartCalls + 1);
}
