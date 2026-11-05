#!/usr/bin/env bash
# Run KiCad's PCB editor headless (Xvfb) with the IPC API, for automated plugin tests.
#   tools/headless-kicad.sh start <board.kicad_pcb>   -> starts pcbnew under Xvfb, waits for the API socket
#   tools/headless-kicad.sh stop
#   tools/headless-kicad.sh status
# State in $XDG_RUNTIME_DIR or /tmp: pid file + log. One instance per user (KiCad's socket is per user).
set -euo pipefail
RUN="${XDG_RUNTIME_DIR:-/tmp}/z2ems-headless"; mkdir -p "$RUN"
PIDF="$RUN/pcbnew.pid"; LOG="$RUN/pcbnew.log"; DISP="${Z2EMS_DISPLAY:-:97}"
SOCK="${KICAD_API_SOCKET:-/tmp/kicad/api.sock}"

case "${1:-status}" in
  start)
    BOARD="${2:?board path}"
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "already running (pid $(cat "$PIDF"))"; exit 0; fi
    rm -f "$SOCK"
    # xvfb-run picks its own display when -a is used; we fix one so a VNC/xrdp viewer could attach if needed
    nohup xvfb-run -n "${DISP#:}" -s "-screen 0 1600x1000x24" pcbnew "$BOARD" >"$LOG" 2>&1 &
    echo $! > "$PIDF"
    for i in $(seq 1 60); do [ -S "$SOCK" ] && { echo "API socket up after ${i}s: $SOCK (pid $(cat "$PIDF"))"; exit 0; }; sleep 1; done
    echo "socket not up after 60 s; log:"; tail -20 "$LOG"; exit 1;;
  stop)
    if [ -f "$PIDF" ]; then pkill -P "$(cat "$PIDF")" 2>/dev/null || true; kill "$(cat "$PIDF")" 2>/dev/null || true; rm -f "$PIDF"; fi
    pkill -u "$USER" -x pcbnew 2>/dev/null || true; pkill -u "$USER" -f "Xvfb $DISP" 2>/dev/null || true
    echo stopped;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "running pid $(cat "$PIDF"), socket: $([ -S "$SOCK" ] && echo up || echo DOWN)"; else echo "not running"; fi;;
  *) echo "usage: $0 start <board>|stop|status"; exit 2;;
esac
