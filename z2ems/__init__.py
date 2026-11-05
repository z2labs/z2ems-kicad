"""Z2Labs EMS - KiCad IPC plugin package.

Pre-processor for the Z2 EMS solver (z2labs/z2ems). See docs/DESIGN.md.
"""

__version__ = "0.2.0"

# Field names carried by the Z2EMS_PORT footprint / symbol (board truth for ports).
FIELD_PREFIX = "Z2EMS."
FIELD_ROLE = "Z2EMS.Role"
FIELD_Z0 = "Z2EMS.Z0"
FIELD_TYPE = "Z2EMS.Type"
FIELD_EXCITE = "Z2EMS.Excite"
FIELD_DIR = "Z2EMS.Dir"
FIELD_REFPLANE = "Z2EMS.RefPlane"
FIELD_LAYER = "Z2EMS.Layer"

PORT_FOOTPRINT_NAME = "Z2EMS_PORT"
PORT_LIB_NICKNAME = "z2ems"
PORT_TYPES = ("lumped", "msl", "cpw", "cpwg")

# Sidecar folder next to the board: <project>.z2ems/
SIDECAR_SUFFIX = ".z2ems"
SETUP_FILENAME = "setup.json"
MESH_FILENAME = "mesh.json"
RESULTS_DIRNAME = "results"
