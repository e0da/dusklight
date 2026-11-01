# Repository Guidelines

## Project Structure & Module Organization

Dusklight is a C/C++ reimplementation of Twilight Princess. Game code lives in `src/`, with corresponding headers in `include/`; actors are under `src/d/actor/`, and port-specific features are under `src/dusk/`. Supporting libraries live in `libs/` and `extern/`, platform integration in `platforms/`, and mod examples in `mods/`. `res/` holds application resources; `assets/` includes documentation images. Consult `docs/building.md` and `docs/code-conventions.md` before changing code.

## Build, Test, and Development Commands

Install CMake 3.25+, Python 3, Ninja, and the platform dependencies listed in `docs/building.md`.

- `git submodule update --init --recursive`: initialize bundled dependencies.
- `cmake --preset macos-default-relwithdebinfo`: configure a macOS build.
- `cmake --build --preset macos-default-relwithdebinfo`: compile that configuration.
- For Linux or Windows, substitute `linux-default-relwithdebinfo` or `windows-msvc-relwithdebinfo` in both commands.
- `build/macos-default-relwithdebinfo/Dusklight.app/Contents/MacOS/Dusklight --dvd /path/to/game.iso`: launch on macOS. Other desktop builds use `build/{preset}/dusklight --dvd /path/to/game.iso`.

## Coding Style & Naming Conventions

Follow nearby naming patterns, including existing actor names such as `d_a_*.cpp`. Use `.clang-format`: four spaces, no tabs, and a 100-column limit. Format changed C/C++ files with `clang-format -i <file>`. Python lint configuration lives in `.flake8`.

Preserve original game code when adding port behavior: delineate changes with `#if TARGET_PC`; use `#if AVOID_UB` for undefined-behavior fixes. Follow existing `JKR_NEW` and `JKR_DELETE` heap-allocation conventions. Prefer upstreaming fixes that also apply to the original decompilation.

## Testing Guidelines

No dedicated project test suite or coverage threshold is documented. Build the affected platform and manually exercise changed behavior with a legally obtained game dump. Use a `*-debug-asan` preset when investigating memory errors. Record reproduction steps, platform, preset, and results in the PR.

## Commit & Pull Request Guidelines

Use Graphite branches rather than changing `main` directly. History mixes descriptive subjects with `fix:` and `feat:` prefixes; keep subjects short and specific. Describe the problem, changed behavior, and validation; link relevant issues and include screenshots for visible changes. The README states that primarily AI-generated contributions are not accepted.

## Security & Configuration

Never commit credentials, personal configuration, or copyrighted game dumps and extracted assets. Contributors must supply their own original game copy.
