#!/usr/bin/env bash
# Install / update the Z2Labs EMS plugin for KiCad 10 on macOS (per user). Idempotent.
#  - symlink ~/Documents/KiCad/<ver>/plugins/kicad-z2ems -> repo
#  - ~/Library/Preferences/kicad/<ver>/kicad_common.json: api.enable_server=true, interpreter_path=<KiCad python>
#  - add "z2ems" to the user's sym-lib-table / fp-lib-table
#  - pip install kicad-python (+pytest) into KiCad's bundled Python (user site)
# Usage: tools/install-macos.sh [--app /Applications/KiCad/KiCad.app]
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP="/Applications/KiCad/KiCad.app"
while [ $# -gt 0 ]; do case "$1" in --app) APP="$2"; shift 2;; *) echo "unknown arg $1"; exit 2;; esac; done
[ -d "$APP" ] || { echo "KiCad.app not found at $APP"; exit 1; }

CLI="$APP/Contents/MacOS/kicad-cli"
VER="$("$CLI" version | cut -d. -f1-2)"
PY="$(ls "$APP"/Contents/Frameworks/Python.framework/Versions/*/bin/python3 | head -1)"
CFG="$HOME/Library/Preferences/kicad/$VER"; PLUG="$HOME/Documents/KiCad/$VER/plugins"
mkdir -p "$CFG" "$PLUG"

"$PY" -m pip install -q --user kicad-python pytest 2>&1 | grep -v "already satisfied" || true

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
echo "Z2Labs EMS installed: $SRC -> $PLUG/kicad-z2ems ; python $PY ; KiCad $VER"
