#!/usr/bin/env bash
# Install / update the Z2Labs EMS plugin for KiCad 10 on Linux (per user). Idempotent.
#  - symlink ~/.local/share/kicad/10.0/plugins/kicad-z2ems -> repo
#  - kicad_common.json: api.enable_server=true, interpreter_path=<python>
#  - add "z2ems" to the user's sym-lib-table / fp-lib-table
#  - pip install kicad-python (+pytest) into VENV (default /scratch/z2ems-venv or ~/.venvs/z2ems)
# Usage: tools/install-linux.sh [--venv PATH]
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-}"
while [ $# -gt 0 ]; do case "$1" in --venv) VENV="$2"; shift 2;; *) echo "unknown arg $1"; exit 2;; esac; done
[ -n "$VENV" ] || { [ -d /scratch ] && VENV=/scratch/z2ems-venv || VENV="$HOME/.venvs/z2ems"; }

command -v kicad >/dev/null || { echo "kicad not found"; exit 1; }
VER="$(kicad-cli version | cut -d. -f1-2)"
CFG="$HOME/.config/kicad/$VER"; PLUG="$HOME/.local/share/kicad/$VER/plugins"
mkdir -p "$CFG" "$PLUG"

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q kicad-python pytest
PY="$VENV/bin/python"

[ -e "$PLUG/kicad-z2ems" ] || ln -s "$SRC" "$PLUG/kicad-z2ems"

C="$CFG/kicad_common.json"
if [ -f "$C" ]; then cp -n "$C" "$C.bak-z2ems" || true; else echo '{}' > "$C"; fi
"$PY" - "$C" "$PY" <<'EOF'
import json, sys
p, py = sys.argv[1], sys.argv[2]
j = json.load(open(p))
j.setdefault("api", {})["enable_server"] = True
j["api"]["interpreter_path"] = py
json.dump(j, open(p, "w"), indent=2)
EOF

for t in sym-lib-table fp-lib-table; do
  f="$CFG/$t"
  if [ "$t" = sym-lib-table ]; then uri="$SRC/library/z2ems.kicad_sym"; head="(sym_lib_table"; else uri="$SRC/library/z2ems.pretty"; head="(fp_lib_table"; fi
  [ -f "$f" ] || printf '%s\n  (version 7)\n)\n' "$head" > "$f"
  if ! grep -q '"z2ems"' "$f"; then
    cp -n "$f" "$f.bak-z2ems" || true
    "$PY" - "$f" "$uri" <<'EOF'
import sys
f, uri = sys.argv[1], sys.argv[2]
t = open(f).read()
i = t.rstrip().rfind(")")
line = f'  (lib (name "z2ems")(type "KiCad")(uri "{uri}")(options "")(descr "Z2Labs EMS simulation ports"))\n'
open(f, "w").write(t[:i] + line + ")\n")
EOF
  fi
done

(cd "$SRC" && "$PY" -m pytest -q tests)
echo "Z2Labs EMS installed: $SRC -> $PLUG/kicad-z2ems ; venv $VENV ; KiCad $VER"
