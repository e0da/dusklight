#include "debug_capture.hpp"

#include <SDL3/SDL_timer.h>
#include <aurora/screenshot.hpp>
#include <nlohmann/json.hpp>
#include <png.h>

#include <algorithm>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace dusk::debug_capture {
namespace {
using Json = nlohmann::json;
constexpr uint64_t kMaxBytes = 64 * 1024 * 1024;
constexpr size_t kChunkBytes = 8192;
constexpr uint64_t kTimeoutNs = 30'000'000'000ULL;

struct Capture {
    std::string id;
    uint64_t startedNs;
    uint32_t width = 0;
    uint32_t height = 0;
    std::vector<uint8_t> png;
};
std::optional<Capture> capture;
uint64_t nextCaptureId = 0;

std::string base64(const uint8_t* bytes, size_t size) {
    constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string result;
    result.reserve((size + 2) / 3 * 4);
    for (size_t index = 0; index < size; index += 3) {
        const auto left = size - index;
        const uint32_t value = (static_cast<uint32_t>(bytes[index]) << 16) |
                               (left > 1 ? static_cast<uint32_t>(bytes[index + 1]) << 8 : 0) |
                               (left > 2 ? bytes[index + 2] : 0);
        result.push_back(alphabet[(value >> 18) & 63]);
        result.push_back(alphabet[(value >> 12) & 63]);
        result.push_back(left > 1 ? alphabet[(value >> 6) & 63] : '=');
        result.push_back(left > 2 ? alphabet[value & 63] : '=');
    }
    return result;
}

std::vector<uint8_t> encode_png(const aurora::screenshot::Result& result) {
    const auto rawBytes = static_cast<uint64_t>(result.width) * result.height * 4;
    if (!result.width || !result.height || result.width > 4096 || result.height > 4096 ||
        rawBytes > kMaxBytes || rawBytes != result.rgba.size())
    {
        throw std::runtime_error("Capture returned invalid image dimensions or pixels");
    }
    png_image image{};
    image.version = PNG_IMAGE_VERSION;
    image.width = result.width;
    image.height = result.height;
    image.format = PNG_FORMAT_RGBA;
    png_alloc_size_t bytes = 0;
    if (!png_image_write_to_memory(&image, nullptr, &bytes, 0, result.rgba.data(), 0, nullptr)) {
        const std::string error = image.message;
        png_image_free(&image);
        throw std::runtime_error("PNG sizing failed: " + error);
    }
    if (!bytes || bytes > kMaxBytes) {
        png_image_free(&image);
        throw std::runtime_error("Encoded screenshot exceeds the 64 MiB limit");
    }
    std::vector<uint8_t> png(bytes);
    if (!png_image_write_to_memory(&image, png.data(), &bytes, 0, result.rgba.data(), 0, nullptr)) {
        const std::string error = image.message;
        png_image_free(&image);
        throw std::runtime_error("PNG encoding failed: " + error);
    }
    png_image_free(&image);
    png.resize(bytes);
    return png;
}

void finish_capture() {
    if (!capture->png.empty()) {
        return;
    }
    try {
        if (SDL_GetTicksNS() - capture->startedNs >= kTimeoutNs) {
            throw std::runtime_error("Screenshot capture timed out");
        }
        aurora::screenshot::Result result;
        if (!aurora::screenshot::poll(result)) {
            return;
        }
        if (!result.error.empty()) {
            throw std::runtime_error(result.error);
        }
        capture->png = encode_png(result);
        capture->width = result.width;
        capture->height = result.height;
    } catch (...) {
        reset();
        throw;
    }
}
}  // namespace

void reset() {
    aurora::screenshot::cancel();
    capture.reset();
}

Json request(const Json& params) {
    if (!params.is_object() || !params.contains("action") || !params["action"].is_string()) {
        throw std::invalid_argument("Screenshot action must be start, read or cancel");
    }
    const auto action = params["action"].get<std::string>();
    if (action == "start") {
        if (capture && capture->png.empty()) {
            if (SDL_GetTicksNS() - capture->startedNs >= kTimeoutNs) {
                reset();
            } else {
                throw std::runtime_error("A screenshot capture is already pending");
            }
        }
        reset();
        if (!aurora::screenshot::request()) {
            throw std::runtime_error("Screenshot renderer is busy or unavailable");
        }
        capture = Capture{.id = std::to_string(++nextCaptureId), .startedNs = SDL_GetTicksNS()};
        return {{"captureId", capture->id}, {"state", "pending"}};
    }
    if (action != "read" && action != "cancel") {
        throw std::invalid_argument("Screenshot action must be start, read or cancel");
    }
    if (!capture || !params.contains("captureId") || !params["captureId"].is_string() ||
        params["captureId"].get_ref<const std::string&>() != capture->id)
    {
        throw std::invalid_argument("Unknown screenshot captureId");
    }
    if (action == "cancel") {
        reset();
        return {{"cancelled", true}};
    }
    if (!params.contains("offset") || !params["offset"].is_number_integer() ||
        (params["offset"].is_number_integer() && !params["offset"].is_number_unsigned() &&
            params["offset"].get<int64_t>() < 0))
    {
        throw std::invalid_argument("Screenshot offset must be a nonnegative integer");
    }
    const auto offset = params["offset"].get<uint64_t>();
    if (offset > kMaxBytes) {
        throw std::invalid_argument("Screenshot offset exceeds the 64 MiB limit");
    }
    finish_capture();
    if (capture->png.empty()) {
        return {{"captureId", capture->id}, {"state", "pending"}};
    }
    if (offset > capture->png.size()) {
        throw std::invalid_argument("Screenshot offset exceeds image size");
    }
    const auto size = std::min(kChunkBytes, capture->png.size() - static_cast<size_t>(offset));
    return {{"captureId", capture->id}, {"state", "ready"}, {"width", capture->width},
        {"height", capture->height}, {"format", "png"}, {"totalBytes", capture->png.size()},
        {"offset", offset}, {"data", base64(capture->png.data() + offset, size)},
        {"done", offset + size == capture->png.size()}};
}

}  // namespace dusk::debug_capture
