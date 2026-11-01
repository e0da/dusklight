#pragma once

namespace dusk::live_debug {

// All entry points run on the game thread, including during prelaunch.
void initialize();
void poll(bool gameReady);
void shutdown();

}  // namespace dusk::live_debug
