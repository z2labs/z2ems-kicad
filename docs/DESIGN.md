# Z2Labs EMS KiCad plugin — design

## 1. Overview

The plugin is the pre-processor of a three-part EM workflow. Only the first part lives in this repository.

| Part | Component | Responsibility |
|---|---|---|
| Pre-processor | **Z2Labs EMS KiCad plugin** (this repository) | Port placement, ground selection, stackup check and fab presets, simulation parameters, mesh preview, starting runs, field overlays |
| Solver | Z2 EMS engine ([z2labs/z2ems](https://github.com/z2labs/z2ems): openEMS fork with a CUDA engine) | FDTD run, on this computer or on a remote GPU box |
| Post-processor | Z2 EMS web viewer (part of the engine) | 3D geometry, currents, fields, S-parameter plots, run history |

KiCad already has the editor, snapping, property dialogs, the schematic link and DRC, so the setup happens on the
user's own board; the web viewer only shows results.

## 2. Platform

- **KiCad 10, IPC API** (`kicad-python` ≥ 0.8). Not the legacy SWIG action-plugin API.
- The plugin runs as a separate process started by KiCad; it talks to the engine over HTTP (the z2twin server), so a
  remote solver is no extra work.
- Interaction model: **select, then act**. The plugin reads the board and the current selection; it does not capture
  canvas mouse events. Ports are placed via the schematic (preferred, section 5) or with KiCad's own tools.

### 2.1 IPC API capabilities used (checked by `spike0/spike0_capabilities.py`)

| # | Capability | Status on KiCad 10 | How the plugin uses it |
|---|---|---|---|
| 1 | Footprints with custom fields, read/write | works | `Z2EMS_PORT` fields |
| 2 | Rename user layers / set colours | **no API** | layers stay `User.N`, the mapping is kept in `setup.json` |
| 3 | Segments → group → lock → delete | works | mesh preview lines |
| 4 | Reference image on a user layer | works, **no opacity property** | opacity baked into the PNG alpha channel |
| 5 | Stackup read; write | read works; `UpdateBoardStackup` has no handler in 10.0.x | presets are written by patching the `.kicad_pcb` and reverting the board |
| 6 | Nets, net classes, pads, tracks, zones, selection | works | ports, ground detection, suggestions |

Other API behaviour the plugin handles: `set_enabled_layers` is additive; items created on a disabled layer are
silently dropped; items created inside an open commit are not addressable until pushed; `get_layer_by_name` returns
`BL_UNDEFINED` instead of raising; right after start the API answers "not ready" / "busy" for several seconds
(`ipc.connect()` waits for a loaded board and retries); on Windows the API runs on a named pipe and
`KICAD_API_SOCKET` must be set in pcbnew's own environment; toolbar buttons that do not fit the window are dropped
without an overflow menu (hence three buttons plus the hub dialog); KiCad draws reference images under all copper.

## 3. Data model

### 3.1 Ports — footprint `Z2EMS_PORT` (board truth)

A real two-pad footprint, modelled on the lumped port of 3D EM solvers:

- **Pad 1 `SIG`** — small SMD pad, placed on the end of the signal conductor.
- **Pad 2 `REF`** — slightly larger SMD pad, placed on ground copper (plane or via).
- Same-net overlap with existing copper is DRC-clean; a REF pad not touching ground produces a KiCad *unconnected* marker → free sanity check.
- Graphics (arrow SIG→REF, port label) drawn on user layer `Z2EMS.Ports`. Excited ports use a distinct line style.
- Attributes: exclude from BOM, exclude from position files, DNP, field `Z2EMS.Role = port` ("simulation only", never manufactured).

Footprint fields (editable in KiCad's Properties dialog, no custom UI needed):

| Field | Values | Default |
|---|---|---|
| `Z2EMS.Z0` | ohms | 50 |
| `Z2EMS.Type` | `lumped`, `msl`, `cpw`, `cpwg` | `lumped` |
| `Z2EMS.Excite` | `yes` / `no` | `yes` |
| `Z2EMS.Dir` | `+x`, `-x`, `+y`, `-y`, `z` | auto |
| `Z2EMS.RefPlane` | mm, de-embed offset from pad | 0 |
| `Z2EMS.Layer` | copper layer of SIG | auto from pad |

Field prefix is `Z2EMS.*`, never `Sim.*` (reserved by KiCad's ngspice integration).

### 3.2 Everything else — sidecar folder

`<project>.z2ems/` next to the `.kicad_pcb`, git-friendly, keyed by KiCad UUIDs.

```
<project>.z2ems/
  setup.json        # user choices (see below)
  mesh.json         # last generated mesh (x/y/z lines) + policy used
  results/
    <run-id>/       # touchstone, field PNGs, log, solver config, board hash
```

`setup.json` (v1):

```json
{
  "version": 1,
  "board_hash": "sha256 of .kicad_pcb at last run",
  "stackup": {
    "source": "kicad | preset",
    "preset": "JLC04161H-7628",
    "validated": true,
    "issues": []
  },
  "nets": {
    "ground": ["GND"],
    "excluded": [],
    "signal_override": {}
  },
  "components": {
    "default": "remove",
    "overrides": { "<fp-uuid>": "keep3d | lumped | remove" }
  },
  "ports": {
    "<fp-uuid>": { "deembed_mm": 0.0, "note": "" }
  },
  "sim": {
    "f_start_hz": 1e8,
    "f_stop_hz": 6e9,
    "excite": ["P1", "P2"],
    "s_matrix_mask": "full | selected",
    "boundary": "PML_8",
    "mesh_policy": "auto | ml | manual",
    "accuracy": 2,
    "end_criteria_db": -40
  },
  "run": {
    "backend": "local | remote",
    "host": "gpu-box",
    "last_run": "<run-id>"
  }
}
```

Rules:

- Footprint fields are the truth for per-port electrical values; `setup.json` holds only extras (de-embed, notes) and caches.
- If a port footprint UUID in `setup.json` no longer exists on the board, the plugin lists **orphaned port settings** and offers re-link to a new port.
- If `board_hash` differs from the current board, results are shown as **stale**.

---

## 4. User layers

KiCad 9+ provides up to 45 renameable user layers with per-layer colors. The plugin creates/renames on first use:

| Layer | Content | Notes |
|---|---|---|
| `Z2EMS.Ports` | port footprint graphics | always on |
| `Z2EMS.Mesh` | x/y mesh lines as graphic segments | one locked group; cleared/regenerated as a unit; lets the user see cells per trace width |
| `Z2EMS.Field.<tag>` | **reference image (PNG)** heat-map of surface current or field | one layer per result view, e.g. `Z2EMS.Field.J_2G4_P1` |
| `Z2EMS.Notes` | warnings as text items (e.g. "trace 0.2 mm gets 2 cells") | optional |

### 4.1 Field overlay — PNG reference image (decided)

- The solver renders the requested quantity (|J|, |E|, |H| at frequency f, excitation Pk, layer L) to a PNG in **board coordinates** with known pixel pitch and origin.
- The plugin places it as a KiCad reference image on `Z2EMS.Field.<tag>`, scaled to board mm, with opacity ≈ 60 %. A small color-bar PNG is placed beside the board outline.
- Multiple overlays may coexist; the user toggles layers in the Appearance panel.
- KiCad draws reference images **under all copper**: after placing, the plugin activates the map layer and switches high contrast to *hide others*; Clear overlays in the Z2Labs EMS window restores the normal view.
- The KiCad overlay is a quick look. Full 3D / animated / cross-section field viewing stays in the WebGPU viewer.
- Fallback (only if reference images are unavailable via IPC): quantised contour polygons on 8 layers `Z2EMS.J.<band>`, each with its own color.

### 4.2 Z direction

Z mesh lines are not drawn on the board; they are shown in the plugin dialog next to the stackup table.

---

## 5. Schematic integration

Symbol `Z2EMS_PORT` (2 pins: `SIG`, `REF`), footprint `Z2EMS_PORT`, library `z2ems`.

Workflow:

1. Designer places `Z2EMS_PORT` on the RF net in the schematic, typically at the same node where a physical RF test port (u.FL / SMA) exists.
2. Annotation gives `P1 … Pn`; port numbering of the S-matrix is defined by the schematic, not by board order.
3. *Update PCB from Schematic* brings the port footprint to the board with the correct nets; the user drops it on the trace end / ground.
4. Measurement reference plane = simulation port location → VNA de-embedding and simulation agree by construction.

Symbol fields mirror the footprint fields (`Z2EMS.Z0`, `Z2EMS.Type`, `Z2EMS.Excite`), transferred to the board by the normal field sync.

---

## 6. Plugin dialog — pages

1. **Stackup** — read KiCad stackup; flag missing/implausible values; offer fab presets (JLCPCB, Eurocircuits, …); write chosen preset back to Board Setup on confirm.
2. **Ports** — table of existing `Z2EMS_PORT` footprints with DRC status; **suggestions** (never forced) derived from: SMA/u.FL footprint geometry and names, nets in 50 Ω net classes, RF trace end-points, existing test points. One-click "add port here" for suggestions.
3. **Ground & nets** — auto-detect ground (net-name heuristics + plane presence); ask only if ambiguous; list excluded nets.
4. **Components** — default: remove; per-footprint override keep-3D / lumped (R/L/C value from fields) / remove.
5. **Simulation** — frequency range, excited ports, S-matrix mask, boundary, accuracy slider, mesh policy (auto / ML / manual), optional memory-guard + auto re-run (default on).
6. **Mesh preview** — generate mesh, draw `Z2EMS.Mesh`, show cell statistics and warnings.
7. **Run** — local or remote backend; progress; on completion: S-parameter summary in dialog, buttons *Open in viewer*, *Overlay field*, *Export Touchstone*, *Export to QUCS-S*.

---

## 7. Solver interface

- Plugin exports: board geometry (via the fork's existing KiCad importer, using the saved `.kicad_pcb`), `setup.json`, port list.
- Backend is a small HTTP/WS service wrapping the fork (same service the WebGPU viewer uses). Local = `localhost`; remote = hostname on your VPN (e.g. Tailscale).
- Results returned: Touchstone `.sNp`, field PNGs per requested view, `run.json` (timings, mesh stats, warnings), link to viewer session.

---

## 8. Circuit co-simulation

- The EM result is an n-port Touchstone, port order `P1 … Pn` from the schematic.
- **QUCS-S export**: plugin writes a Qucs netlist with the layout as an S-parameter block and the remaining schematic components as circuit elements. Intended use: matching-network tuning, Smith chart, RF-centric analysis.
- **KiCad ngspice**: newer ngspice releases provide a Touchstone/S-parameter device; if available in KiCad 10's bundled ngspice, simple cases need no QUCS-S. To be verified in Spike 0b.

---

## 9. Roadmap

| Milestone | Deliverable | Status |
|---|---|---|
| M0 | IPC API capability check | done |
| M1 | `z2ems` library (symbol + footprint), user layers, `setup.json` | library and sidecar done; schematic round-trip to be tested |
| M2 | Minimal loop: coupon → S11/S21 in the dialog and the viewer | done (coupon_msl50: Z0 49.50 Ω, εeff 3.353) |
| M3 | Mesh preview layer, stackup presets | done |
| M4 | Field overlay, viewer hand-off | done |
| M5 | Full S-matrix, port suggestions, component handling | suggestions and lumped R/L/C done |
| M6 | QUCS-S export | open |
