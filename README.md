# Z2Labs EMS for KiCad

Electromagnetic simulation of KiCad boards from inside the PCB editor. The plugin
reads the board, its stackup and its connectors, sets up ports, runs the
[Z2 EMS engine](https://github.com/z2labs/z2ems) (an openEMS fork with a CUDA
engine, conformal metal edges and a KiCad importer) and brings the results back
onto the board: S-parameters, line impedance, the FDTD mesh and current-density
maps as overlays.

Status: **0.1, first public release.** Two-port lines, launches and small RF
structures are routine; large boards work but the run-time estimate tells you
when a board is beyond what a laptop CPU can do in reasonable time.

## What it does

| Button / action | What happens |
|---|---|
| **Z2Labs EMS** (hub) | board report: copper layers, stackup check, suggested RF ports (connector footprints, net names, net classes), ground nets; writes `setup.json` next to the board. Mesh preview, field overlay and *Clear overlays* are in the hub. |
| **Fab stackup** | pick a manufacturer stackup (JLCPCB 2-layer 1.6 mm, JLC04161H-7628, JLC081611-2116, or your own JSON / imported JLC table) and write it into the board stackup |
| **Simulate** | starts the engine's local server, runs one excitation per port, shows S-parameters, Z0 and effective permittivity for a two-port line, and opens the 3D viewer |
| Mesh preview | draws the exact mesh the engine will use on a user layer, with cell count and run-time estimate |
| Field overlay | current-density maps of the last run as reference images on user layers |

Two-pad R, L and C footprints become lumped elements in the model (value from the
footprint), so matching networks and series parts are simulated as placed.

## Requirements

- KiCad **10** with *Preferences → Plugins → Enable IPC API server* ticked.
- The **Z2 EMS engine**: a release package from
  [z2labs/z2ems](https://github.com/z2labs/z2ems/releases) (Windows: unzip, nothing
  to install) or a source build. An NVIDIA GPU is optional; with one, the CUDA
  engine is used when you choose the `cuda` or `auto` variant.

## Install

Windows (PowerShell, KiCad closed):

    powershell -ExecutionPolicy Bypass -File tools\install-windows.ps1

Linux / macOS:

    tools/install-linux.sh        # or tools/install-macos.sh

The installers link the repository into KiCad's plugin folder, enable the API
server, add the `z2ems` symbol and footprint library (`Z2EMS_PORT`, an explicit
port footprint for places without a connector) and run the unit tests. Manual
install: copy or link this folder to `Documents\KiCad\10.0\plugins\kicad-z2ems`
(Windows) or `~/.local/share/kicad/10.0/plugins/kicad-z2ems` (Linux) and restart
KiCad; KiCad creates the plugin's Python environment from `requirements.txt`.

### Telling the plugin where the engine is

The engine package is found automatically in `%LOCALAPPDATA%\Z2EMS\engine`,
`C:\Z2EMS` or `~/z2ems` (Linux also `~/.local/share/z2ems/engine`). Anywhere else:

    set Z2EMS_ENGINE=D:\tools\z2ems-engine

or `~/.z2ems.json`:

```json
{ "engine": "D:/tools/z2ems-engine" }
```

Other keys / variables: `twin_py` / `Z2EMS_TWIN_PY` (Python with the CSXCAD and
openEMS bindings), `fork_root` / `Z2EMS_FORK_ROOT` (a z2ems source checkout),
`openems_exe` / `Z2EMS_OPENEMS` (engine executable), `Z2EMS_SERVER` (a running
engine server, default `http://127.0.0.1:8765`).

## Quick start

1. Open `examples/coupon_msl50.kicad_pcb` from the engine package (a 40 mm 50 Ω
   microstrip with two SMA pads) in the PCB editor.
2. **Fab stackup** → JLC04161H-7628 → Apply.
3. **Z2Labs EMS** → check that J1 and J2 are the suggested ports → *Mesh preview*.
4. **Simulate**. On a 4-core laptop the run takes about 5 minutes and should
   report Z0 ≈ 49.5 Ω and εeff ≈ 3.35 (`presets/refs/coupon_msl50.json`).
5. *Field overlay* shows where the current flows; *Clear overlays* removes
   everything the plugin drew.

## Layout

```
plugin.json          KiCad plugin manifest (actions and toolbar buttons)
z2ems/               plugin package (ipc, model, stackup, ports, mesh preview, simulate, overlays, ui)
library/             z2ems symbol + Z2EMS_PORT footprint
presets/stackups/    fab stackup presets (JSON)
presets/refs/        reference results used by the result dialog
spike0/              KiCad API capability check (run it if something does not work on your KiCad version)
tests/               pytest, no KiCad needed
tools/               installers, headless KiCad helper for CI
docs/DESIGN.md       design notes: data model, port and ground rules, overlays, roadmap
```

## Known limitations

- KiCad 10.0.x cannot write the board stackup through the API; the stackup
  preset is applied by saving, patching and reloading the board file (the plugin
  asks first and keeps a backup).
- Through-hole connectors without reference copper under the pad get their port
  moved along the track to the first point with copper below it; for anything
  else place a `Z2EMS_PORT` footprint.
- Very large boards (hundreds of millions of cells) need a GPU or a crop of the
  region of interest.

## Tests

    pip install -r requirements.txt pytest
    pytest tests

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). The engine is GPL-3.0 (openEMS) and
LGPL-3.0 (CSXCAD), © Thorsten Liebig and contributors, with the Z2 EMS additions.
