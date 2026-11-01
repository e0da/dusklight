# Selected versions and release channels

Play and Lab select releases explicitly. Integrating a change into `main` does not create a version, advance a channel, or deploy an app. Choose a version number when a particular commit is worth naming. Builds before that choice are unreleased candidates; staging a candidate does not change a release pointer.

There are two separate files:

- `config/channels.json` defines the Play and Lab contexts, their inherited collections, app identities, features, and tooling. Each commit carries its own catalog. The catalog at a release tag is the authority for that release.
- `releases/channels.json` selects a recorded version for each release channel. A `null` pointer means no version has been chosen. This repository starts with both pointers unset.

Runtime context selection and callbacks use the catalog. The release pointer file is bookkeeping for explicit version promotion; it does not alter a running app or replace the runtime catalog.

## Choose and record a version

1. Select an exact commit and a SemVer tag such as `midna/v0.2.0`. Version selection is a human decision, separate from integration into `main`.
2. Create an annotated Git tag pointing at that full commit. Graphite does not provide Git tag creation, so this step uses Git directly. Treat the tag as immutable; choose a new version instead of moving or replacing it.
3. Prepare the immutable version record after the tag exists.
4. Build the tagged source and verify that the artifact records that exact source commit.
5. Stage the candidate using the catalog from that same tag, and validate the artifact.
6. Explicitly promote the selected version in a reviewable promotion PR.
7. Deploy the approved artifact as a separate action.

These are command examples for the person selecting a version; the tooling never creates or pushes tags:

```sh
git tag -a midna/v0.2.0 FULL_COMMIT_SHA -m 'Midna 0.2.0'
python3 tools/release_channels.py prepare --tag midna/v0.2.0 --commit FULL_COMMIT_SHA
```

Replace `FULL_COMMIT_SHA` with the complete lowercase object ID, rather than a branch name or abbreviated hash. Tag publication follows the repository's human release workflow; neither `prepare` nor `promote` publishes anything.

`prepare` verifies that the existing tag resolves to that commit and that the commit contains a valid channel catalog. It writes `releases/versions/midna%2Fv0.2.0.json`, recording the tag object ID, commit ID, catalog Git blob ID, and optional artifact metadata. The filename encodes the slash in the version tag so records remain in one directory.

For optional metadata, pass a JSON object file with `--artifact-metadata FILE`. Keep credentials, copyrighted game data, and machine-specific paths out of tracked release records. Supplying the same tag and metadata again is idempotent; changed metadata is rejected. Decide what metadata belongs in the record before preparing it. Additional build evidence can accompany the candidate or promotion PR.

## Stage and promote

A version record identifies selected source; it does not prove that a build passed or that a staged binary matches it. Build from the tagged commit, stage with the release's catalog and version tag, and check the source metadata before promoting. See [tvOS channel staging](tvos-channels.md) for the packaging workflow. A candidate remains unreleased until its channel pointer is explicitly selected.

```sh
python3 tools/release_channels.py promote --channel lab --tag midna/v0.2.0
python3 tools/release_channels.py status
```

Review the changed `releases/channels.json` and the new version record in a promotion PR. Promoting Lab leaves Play unchanged. Play can select the same recorded version later, or select a different version after validation:

```sh
python3 tools/release_channels.py promote --channel play --tag midna/v0.2.0
```

`promote` requires an existing immutable record. It rechecks the tag object, resolved commit, and catalog blob, then confirms that the requested channel exists in the catalog at that commit. The current working tree's catalog does not determine whether an older release has a channel. Each pointer includes the tag, commit, and pinned catalog blob ID. A moved or reannotated tag, missing record, or mismatched catalog is rejected.

Writes are atomic. Version records are created without overwriting existing records. Channel pointers change only through an explicit promotion command; no version bump, build, signing, installation, tag creation, or push is performed. A rollback uses the same explicit promotion process to select a previously recorded and validated version.

## Validation

```sh
python3 -m unittest discover -s tests -p test_release_channels.py -v
```

Tests create isolated temporary repositories and fixture tags. They cover exact commit matching, immutable metadata, moved and reannotated tags, pinned release catalogs, selective promotion, malformed inputs, and unchanged pointers after preparation. They do not mutate this repository's tags or history.
