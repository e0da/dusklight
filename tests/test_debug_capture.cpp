#include "dusk/debug_capture.hpp"

#include <SDL3/SDL_timer.h>
#include <aurora/screenshot.hpp>
#include <nlohmann/json.hpp>
#include <png.h>

#include <cassert>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using Json = nlohmann::json;
uint64_t clockNs = 1;
unsigned cancelCalls = 0;
unsigned pollCalls = 0;
bool available = true;
bool ready = false;
aurora::screenshot::Result result;

template <typename Exception, typename Action>
void rejects(Action action) {
    bool rejected = false;
    try {
        action();
    } catch (const Exception&) {
        rejected = true;
    }
    assert(rejected);
}

Json start() {
    return dusk::debug_capture::request({{"action", "start"}});
}

Json read(const std::string& id, uint64_t offset = 0) {
    return dusk::debug_capture::request(
        {{"action", "read"}, {"captureId", id}, {"offset", offset}});
}

std::vector<uint8_t> decode(const std::string& encoded) {
    const std::string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::vector<uint8_t> decoded;
    uint32_t bits = 0;
    unsigned count = 0;
    for (char c : encoded) {
        if (c == '=') {
            break;
        }
        const auto position = alphabet.find(c);
        assert(position != std::string::npos);
        bits = (bits << 6) | static_cast<uint32_t>(position);
        count += 6;
        if (count >= 8) {
            count -= 8;
            decoded.push_back(static_cast<uint8_t>(bits >> count));
        }
    }
    return decoded;
}
}  // namespace

Uint64 SDL_GetTicksNS() {
    return clockNs;
}

namespace aurora::screenshot {
bool request() {
    return available;
}
bool poll(Result& output) {
    ++pollCalls;
    if (!ready) {
        return false;
    }
    output = std::move(result);
    ready = false;
    return true;
}
void cancel() {
    ++cancelCalls;
    ready = false;
}
}  // namespace aurora::screenshot

int main() {
    using dusk::debug_capture::request;
    using dusk::debug_capture::reset;
    rejects<std::invalid_argument>([] { request(Json::array()); });
    rejects<std::invalid_argument>([] { request({}); });
    rejects<std::invalid_argument>([] { request({{"action", "bad"}}); });
    rejects<std::invalid_argument>([] { read("missing"); });

    auto initial = start();
    const auto id = initial["captureId"].get<std::string>();
    assert(initial["state"] == "pending");
    rejects<std::runtime_error>([] { start(); });
    assert(read(id)["state"] == "pending");
    for (const Json& offset :
        {Json(-1), Json(0.5), Json(true), Json("0"), Json(64 * 1024 * 1024 + 1)})
    {
        rejects<std::invalid_argument>(
            [&] { request({{"action", "read"}, {"captureId", id}, {"offset", offset}}); });
    }
    rejects<std::invalid_argument>([&] { read(id + "stale"); });
    rejects<std::invalid_argument>([&] { request({{"action", "cancel"}, {"captureId", false}}); });

    result.width = 128;
    result.height = 128;
    result.rgba.resize(result.width * result.height * 4);
    uint32_t random = 12345;
    for (auto& byte : result.rgba) {
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        byte = random & 255;
    }
    const auto expected = result.rgba;
    ready = true;
    std::vector<uint8_t> png;
    unsigned chunks = 0;
    uint64_t size = 0;
    for (;;) {
        const auto part = read(id, png.size());
        assert(part["state"] == "ready");
        assert(part["format"] == "png");
        assert(part["width"] == 128 && part["height"] == 128);
        assert(part["offset"] == png.size());
        size = part["totalBytes"].get<uint64_t>();
        const auto bytes = decode(part["data"]);
        assert(bytes.size() <= 8192);
        png.insert(png.end(), bytes.begin(), bytes.end());
        ++chunks;
        if (part["done"].get<bool>()) {
            break;
        }
    }
    assert(chunks > 1 && png.size() == size);
    const auto pollsAfterReady = pollCalls;
    const auto eof = read(id, size);
    assert(eof["done"] == true && eof["data"] == "");
    assert(pollsAfterReady == pollCalls);
    rejects<std::invalid_argument>([&] { read(id, size + 1); });
    png_image image{};
    image.version = PNG_IMAGE_VERSION;
    assert(png_image_begin_read_from_memory(&image, png.data(), png.size()));
    image.format = PNG_FORMAT_RGBA;
    std::vector<uint8_t> pixels(PNG_IMAGE_SIZE(image));
    assert(png_image_finish_read(&image, nullptr, pixels.data(), 0, nullptr));
    assert(pixels == expected);
    png_image_free(&image);

    const auto second = start()["captureId"].get<std::string>();
    assert(second != id);
    rejects<std::invalid_argument>([&] { read(id); });
    const auto cancelled = request({{"action", "cancel"}, {"captureId", second}});
    assert(cancelled["cancelled"] == true);
    rejects<std::invalid_argument>([&] { read(second); });

    available = false;
    rejects<std::runtime_error>([] { start(); });
    available = true;
    const auto failed = start()["captureId"].get<std::string>();
    result = {};
    result.error = "GPU mapping failed";
    ready = true;
    rejects<std::runtime_error>([&] { read(failed); });
    rejects<std::invalid_argument>([&] { read(failed); });

    for (const auto dimensions : {std::pair{0u, 1u}, std::pair{4097u, 1u}, std::pair{2u, 2u}}) {
        const auto invalid = start()["captureId"].get<std::string>();
        result = {};
        result.width = dimensions.first;
        result.height = dimensions.second;
        result.rgba = {1, 2, 3, 4};
        ready = true;
        rejects<std::runtime_error>([&] { read(invalid); });
    }

    const auto expired = start()["captureId"].get<std::string>();
    clockNs += 30'000'000'000ULL;
    rejects<std::runtime_error>([&] { read(expired); });
    rejects<std::invalid_argument>([&] { read(expired); });
    const auto beforeReset = start()["captureId"].get<std::string>();
    const auto cancellations = cancelCalls;
    reset();
    assert(cancelCalls == cancellations + 1);
    rejects<std::invalid_argument>([&] { read(beforeReset); });
    assert(start()["captureId"] != beforeReset);
    reset();
}
