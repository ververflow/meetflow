#!/usr/bin/env bash
# Captures BOTH channels via the sidecar: them (system tap) + me (mic, AEC on speakers).
set -u
cd /Users/daniverver/code/tools/meetflow
pkill -9 -f "MeetFlowCapture" 2>/dev/null; sleep 0.3   # clean slate
APP="swift-sidecar/MeetFlowCapture.app"
T="$(mktemp -d)"; THEM="$T/them.wav"; ME="$T/me.wav"; ST="$T/capture-status.json"

open -n "$APP" --args --out "$THEM" --mic-out "$ME" --aec auto --sample-rate 16000
PID=""
for _ in $(seq 1 40); do
  [ -f "$ST" ] && PID=$(/usr/bin/python3 -c "import json;print(json.load(open('$ST')).get('pid',''))" 2>/dev/null)
  [ -n "$PID" ] && break; sleep 0.1
done
( sleep 15; pkill -9 -f "MeetFlowCapture" 2>/dev/null ) &   # watchdog
WD=$!
echo "sidecar pid: ${PID:-<none>}"
echo ">>> praat nu een paar seconden (test van de microfoon) <<<"
sleep 0.5
afplay /System/Library/Sounds/Submarine.aiff 2>/dev/null || true
afplay /System/Library/Sounds/Glass.aiff 2>/dev/null || true
sleep 2
[ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null
sleep 1.5
kill -9 "$WD" 2>/dev/null
.venv/bin/python - "$ME" "$THEM" <<'PY'
import sys, os, numpy as np, soundfile as sf
def info(p):
    if not os.path.exists(p): return "no file"
    d, sr = sf.read(p, dtype="float32")
    rms = float(np.sqrt(np.mean(d**2))) if len(d) else 0.0
    return f"sec={len(d)/sr:.2f} rms={rms:.6f}"
print("me   (mic):", info(sys.argv[1]))
print("them (tap):", info(sys.argv[2]))
PY
rm -rf "$T"
