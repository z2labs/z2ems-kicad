# Z2Labs EMS for KiCad

Electromagnetic simulation of KiCad boards from inside the PCB editor. The plugin
reads the board, its stackup and its connectors, sets up ports, runs the
[Z2 EMS engine](https://github.com/z2labs/z2ems) (an openEMS fork with a CUDA
engine, conformal metal edges and a KiCad importer) and brings the results back
onto the board: S-parameters, line impedance, the FDTD mesh, and the currents and
voltages of the run as maps and lines on the board and in a 3D viewer.

Status: **0.3.** Two-port lines, launches and small RF structures are routine;
large boards work but the run-time estimate tells you when a board is beyond
what a laptop CPU can do in reasonable time.

## What it does

The **Z2Labs EMS** button opens one window next to the PCB editor (KiCad stays
usable while it is open):

- **Ports.** A port is a **+** pad on the RF copper and a **-** reference to
  ground. Select a connector, an IC pin, or a pad and its ground pads in the PCB
  editor and click *+ Port from selection*: a connector gives its signal pad as +
  and all its ground pads as -, an IC pin its neighbouring GND pins, a pad with no
  ground copper next to it the ground plane below (a vertical port). *Set + / Set -
  here* changes one end, *Show* selects the port's pads in the editor. Connector
  suggestions are listed but used only when you tick and add them; a port without
  a reference is shown as incomplete and not simulated.
- **Markers.** Every port is drawn on the `Z2EMS.Ports` user layer: rings with
  + and -, arrows from each ground pad to the signal pad (or a down arrow and the
  reference layer name), and a "P1 50 Ω" label. A user layer has no copper and no
  nets, so DRC, ERC, the BOM and the fabrication outputs are unaffected.
- **Board.** Stackup check and *Fab stackup...* (JLCPCB 2-layer 1.6 mm,
  JLC04161H-7628, JLC081611-2116, your own JSON or an imported JLC table), ground
  nets.
- **Simulation.** Frequency range, run-size estimate, *Mesh preview* (the exact
  engine mesh on a user layer), *Simulate* (one excitation per ticked port),
  *Fields on board...*, *3D viewer*, *Clear overlays*. A run goes to the local
  engine server and continues in the
  background: the window shows its progress, opens the result and the 3D viewer
  when it is done, and picks the run up again when it is reopened.

- **Fields.** Every run stores, for each driven port, the surface current on
  every copper layer, the E-field around the stack and the voltage of each copper
  layer against the next one, at 0.25, 0.5 and 0.9 x fmax, scaled to **1 W
  incident at the port** (a matched 50 Ω line carries 0.2 A and 10 V).
  *Fields on board...* draws them in the PCB editor: per copper layer a heat map
  (|Js| in dB, or the signed voltage) and **current lines** (width = current
  density, chevrons = direction) or **iso-voltage lines**, at the instant of the
  strongest field or a chosen phase, on its own `Z2EMS.Field` user layer. The
  *3D viewer* shows the same with arrows and lines, animated over the RF cycle,
  and a **cut plane** through the stack with the E-field lines from the trace to
  its reference plane.

Ports are saved in `<board>.z2ems/setup.json` next to the board. The 3D viewer
shows and simulates the same ports.

Two-pad R, L and C footprints become lumped elements in the model (value from the
footprint), so matching networks and series parts are simulated as placed.

## Requirements

- KiCad **10** with *Preferences → Plugins → Enable IPC API server* ticked.
- The **Z2 EMS engine** (~160 MB, separate from the plugin). On Windows the plugin
  offers to download and install it the first time an action needs it (see below);
  on Linux and macOS it is built from source ([z2labs/z2ems](https://github.com/z2labs/z2ems#install)).
  An NVIDIA GPU is optional; with one, the CUDA engine is used when you choose the
  `cuda` or `auto` variant.
- Windows: the Microsoft Visual C++ 2015-2022 x64 runtime (`vc_redist.x64.exe`). Most
  PCs have it; on a fresh Windows without it, the Python environment KiCad creates
  for the plugin stops with "VCRUNTIME140.dll was not found".

## Install

**From KiCad (Plugin and Content Manager):** *Tools → Plugin and Content Manager →
Plugins → Z2Labs EMS → Install*, then restart the PCB editor. KiCad creates the
plugin's Python environment itself.

**The engine:** the first time *Simulate* or *Mesh preview* needs it, or from the window's
*Engine...* button, the plugin shows what it would download (the latest
`Z2EMS_engine_*_win64.zip` from the [z2ems releases](https://github.com/z2labs/z2ems/releases),
its size and the target folder) and installs it only after you click *Download*. The file
is checked against the SHA-256 that GitHub publishes for it and unpacked to
`%LOCALAPPDATA%\Z2EMS\engine`; nothing else on the computer is changed. *Choose folder...*
points the plugin to an engine you already have (a source build on Linux/macOS, or a
zip you unpacked yourself). *Engine...* also updates the engine to a newer release.

**From this repository (development):**

    powershell -ExecutionPolicy Bypass -File tools\install-windows.ps1     # Windows, KiCad closed
    tools/install-linux.sh                                                # or tools/install-macos.sh

The scripts link the repository into KiCad's plugin folder, enable the API server, add
the `z2ems` symbol and footprint library (`Z2EMS_PORT`, an explicit port footprint for
places without a connector) and run the unit tests. With the PCM install the library
files are in the plugin folder (`library/`); add them to the symbol and footprint
library tables by hand if you want the port footprint. `tools/build_pcm.py` builds the
PCM package.

Your own stackup presets (imported JLC tables, hand-made JSON) are kept in
`~/.z2ems/stackups`, outside the plugin folder, so an update does not remove them.

### Telling the plugin where the engine is

The engine package is found automatically in `%LOCALAPPDATA%\Z2EMS\engine`,
`C:\Z2EMS` or `~/z2ems` (Linux also `~/.local/share/z2ems/engine`). Anywhere else:

    set Z2EMS_ENGINE=D:\tools\z2ems-engine

or `~/.z2ems.json`:

```json
{ "engine": "D:/tools/z2ems-engine" }
```

(*Choose folder...* in the engine dialog writes this file.) Other keys / variables: `twin_py` / `Z2EMS_TWIN_PY` (Python with the CSXCAD and
openEMS bindings), `fork_root` / `Z2EMS_FORK_ROOT` (a z2ems source checkout),
`openems_exe` / `Z2EMS_OPENEMS` (engine executable), `Z2EMS_SERVER` (a running
engine server, default `http://127.0.0.1:8765`).

## Quick start

1. Open `examples/coupon_msl50.kicad_pcb` from the engine folder (a 40 mm 50 Ω
   microstrip with two SMA pads) in the PCB editor.
2. **Fab stackup** → JLC04161H-7628 → Apply.
3. **Z2Labs EMS** → tick J1 and J2 under *Suggestions* → *Add ticked* (or select
   J1 in the editor → *+ Port from selection*, same for J2) → *Mesh preview*.
4. **Simulate**. On a 4-core laptop the run takes about 5 minutes and should
   report Z0 ≈ 49.5 Ω and εeff ≈ 3.35 (`presets/refs/coupon_msl50.json`).
5. *Fields on board...* → P1, the middle frequency, surface current, F.Cu and
   In1.Cu: the signal current on the strip and its return current in the plane
   under it. In the *3D viewer*, *Fields* → *Show fields* → *Animate*, and *E-field
   on a cut plane* for the cross-section. *Clear overlays* removes everything the
   plugin drew.

## Layout

```
plugin.json          KiCad plugin manifest (actions and toolbar buttons)
z2ems/               plugin package (ipc, model, stackup, ports, mesh preview, simulate, overlays, ui)
library/             z2ems symbol + Z2EMS_PORT footprint
presets/stackups/    fab stackup presets (JSON)
presets/refs/        reference results used by the result dialog
spike0/              KiCad API capability check for developers (run from a source checkout; not in the PCM package)
tests/               pytest, no KiCad needed
tools/               installers, PCM package builder (build_pcm.py), headless KiCad helper
pcm/                 PCM metadata template and 64x64 icon
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
