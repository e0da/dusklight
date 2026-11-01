#include "dusk/commands.hpp"
#include "dusk/live_debug.hpp"

#include <SDL3/SDL_timer.h>
#include <borealis/log.hpp>
#include <borealis/ws.hpp>
#include <nlohmann/json.hpp>

#include <cassert>
#include <cstdlib>
#include <string>
#include <utility>
#include <vector>

namespace {
unsigned connectCalls = 0;
bool connected = false;
bool openPending = false;
uint64_t nowNs = 1;
std::vector<std::string> sent;
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
    if (!std::exchange(openPending, false)) {
        return false;
    }
    event.kind = Event::Kind::Open;
    return true;
}
SendResult Connection::send(MessageKind, std::string_view data) {
    sent.emplace_back(data);
    return SendResult::Ok;
}
CloseResult Connection::close(uint16_t, std::string_view) {
    connected = false;
    openPending = false;
    return CloseResult::Ok;
}
Connection connect(Options options) {
    assert(options.url == "ws://127.0.0.1:9876");
    assert(options.headers.front().value == "Bearer test-token");
    ++connectCalls;
    connected = true;
    openPending = true;
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
void runCommand(std::string_view, CommandState&, const CommandOutput&) {
    std::abort();
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
    dusk::live_debug::shutdown();
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);

    unsetenv("DUSK_DEBUG_TOKEN");
    dusk::live_debug::initialize();
    dusk::live_debug::poll(false);
    assert(connectCalls == 1);
}
