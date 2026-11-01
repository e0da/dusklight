# Apple TV trunk and dev builds

Keep two installations on the TV:

| Channel | Home screen name | Icon | Default bundle ID |
| --- | --- | --- | --- |
| Trunk | Dusklight Trunk | Gold, marked TRUNK | `dev.e0da.dusklight.trunk` |
| Dev | Dusklight Dev | Purple/cyan, marked DEV | `dev.e0da.dusklight.dev` |

Distinct bundle IDs give each app its own tvOS data container, including saves,
controller settings, and shader caches. Development updates target only Dev.
Keep the last validated Trunk installed while testing changes. These are local
development channels, not App Store release configurations.

## Build and stage

Build the desired source revision with the experimental `tvos-default` preset
as described in [building.md](building.md). Use a clean `main` checkout for Trunk;
Dev can use a working branch. Record the exact commit when compiling. Copy or
stage the completed app before rebuilding the shared CMake output for another
revision. Separate CMake build directories can also be selected with `-B`.

From the tooling checkout, stage an unsigned channel bundle:

```sh
python3 tools/tvos_channels.py stage --channel dev \
  --app build/tvos-default/Dusklight.app --output build/tvos-channels \
  --commit FULL_BUILD_COMMIT --source-ref YOUR_BUILD_BRANCH \
  --game-image /private/path/game.rvz
```

For Trunk, select `--channel trunk --source-ref main` and provide the actual main
build commit. The script requires a full hexadecimal commit ID. It records the
caller-provided source claim and the executable's SHA-256; it does not independently
prove which source produced an arbitrary executable. Compare the build's generated
`borealis/version.h` revision and retain the build log before promotion.

Staging invokes the native Swift icon generator and Xcode `actool`, merges the
compiled icon metadata, and writes `channel-manifest.json`. No extra image package
is needed. Output paths are `build/tvos-channels/{trunk,dev}/Dusklight.app`. Included
game images must remain private; repository output containing them must be ignored.

Existing output is rejected unless `--replace` is supplied. Replacement is limited
to the same channel identity, and failed staging preserves the previous output.
Neither command signs, installs, launches, or removes an app.

## Sign, install, and validate

Use a tvOS development profile matching the selected channel's bundle ID. Add the
profile and sign the staged bundle after packaging. Validate its signature and
bundle ID before installing with `devicectl`. Keep credentials, profiles, and game
images outside Git.

Each app needs its own readable game path and initial controller settings. For a
privately bundled image, resolve the installed bundle URL and launch with
`--dvd /DEVICE/BUNDLE/PATH/game.rvz`; startup records that path for subsequent
launches. Updates can change the bundle URL, so resolve it again after installing.
Copy initial saves and controller profiles explicitly with backups; there is no
live save synchronization between channels. tvOS cache storage remains evictable.

To promote Trunk, first validate the exact main candidate as Dev while the previous
Trunk remains installed. Check boot, controls, gyro, save access, and representative
gameplay. Then package that same raw main build as Trunk and retain its manifest.
A new commit on `main` alone is not a reason to replace the working Trunk.

## Checks and previews

```sh
python3 -m unittest discover -s tests -p test_tvos_channels.py -v
swift tools/tvos_icons.swift --channel trunk --output build/icon-preview/trunk
swift tools/tvos_icons.swift --channel dev --output build/icon-preview/dev
```

Each icon output includes `preview.png` and the layered tvOS asset catalog.
