# Submitting a release to the KiCad addon repository

The KiCad Plugin and Content Manager lists packages from
[gitlab.com/kicad/addons/metadata](https://gitlab.com/kicad/addons/metadata). A release is added
there with a merge request; the public repository updates itself from it.

1. Bump `__version__` in `z2ems/__init__.py` and add the version to `pcm/metadata.json`
   (`status`, `kicad_version` 10.0, `runtime` ipc). Keep the older versions in the list.
2. Tag `v<version>` on `main`. The *PCM package* workflow runs the tests, builds
   `z2ems-kicad-<version>-pcm.zip`, validates `metadata.json` against the PCM schema and
   publishes a GitHub release with the zip, `metadata.json` (with `download_url`,
   `download_sha256`, `download_size`, `install_size`) and `icon.png`.
3. Test the zip in KiCad: *Plugin and Content Manager → Install from File...*, restart the
   PCB editor, run the hub, *Engine...* and a coupon simulation.
4. In a fork of `kicad/addons/metadata`, put the release's `metadata.json` and `icon.png` into
   `packages/com.github.z2labs.z2ems/` and open a merge request against `main`:

   - title: `Add Z2Labs EMS (com.github.z2labs.z2ems) <version>` (later: `Update ...`)
   - description: what the plugin does, KiCad 10 / IPC runtime, that the solver is a separate,
     consent-based download (below), the license (GPL-3.0) and where issues go.

The repository's CI checks the metadata, the archive layout and the hash; a KiCad team member
reviews and merges. Updates are new merge requests that change `metadata.json`.

## Notes for the reviewers

- IPC API plugin (`"runtime": "ipc"`), KiCad 10.0 or later; dependencies come from
  `plugins/requirements.txt` (`kicad-python`) in KiCad's managed environment.
- The FDTD solver (Z2 EMS engine, GPL-3.0, ~160 MB) is not in the package. On Windows the plugin
  offers to download the latest engine release from github.com/z2labs/z2ems only when an action
  needs it and only after the user clicks *Download*; the dialog shows the file, size, source and
  target folder. The download is checked against GitHub's SHA-256 asset digest (or the release's
  `SHA256SUMS.txt`) and unpacked to `%LOCALAPPDATA%\Z2EMS\engine`. On Linux and macOS the user
  builds the engine from source and selects its folder.
- No telemetry, no accounts, no network access other than that download and the local engine
  server on 127.0.0.1.
