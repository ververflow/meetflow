#!/usr/bin/env bash
# Build the MeetFlow CoreAudio capture sidecar and wrap it in a signed .app bundle.
#
# A bare command-line binary cannot surface the macOS system-audio (NSAudioCaptureUsageDescription)
# permission prompt and never appears in System Settings, so it silently captures zeros. Wrapping
# it in a signed .app with an Info.plist is what makes the prompt appear and the grant stick.
#
# Signs with the dedicated "MeetFlow Local Signing" identity in a separate keychain
# (~/Library/Keychains/meetflow-signing.keychain-db, password below). Because TCC keys the
# system-audio/mic grant on this CERTIFICATE, the grant survives rebuilds — you allow once.
# Falls back to ad-hoc if that keychain/identity is missing.
set -euo pipefail
cd "$(dirname "$0")"

APP="MeetFlowCapture.app"
BIN="MeetFlowCapture"
SIGN_KC="$HOME/Library/Keychains/meetflow-signing.keychain-db"
SIGN_KC_PW="meetflow-local"
SIGN_CN="MeetFlow Local Signing"

echo "==> swift build -c release"
swift build -c release

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp ".build/release/meetflow-capture" "$APP/Contents/MacOS/$BIN"
cp "MeetFlowCapture-Info.plist" "$APP/Contents/Info.plist"

SIGN_ID=""
if [ -f "$SIGN_KC" ]; then
  security unlock-keychain -p "$SIGN_KC_PW" "$SIGN_KC" 2>/dev/null || true
  SIGN_ID=$(security find-identity -p codesigning "$SIGN_KC" 2>/dev/null | awk -v cn="$SIGN_CN" '$0 ~ cn {print $2; exit}')
fi

if [ -n "$SIGN_ID" ]; then
  echo "==> codesign (stable identity: $SIGN_CN / $SIGN_ID)"
  codesign --force --keychain "$SIGN_KC" --sign "$SIGN_ID" "$APP"
else
  echo "==> codesign (ad-hoc — stable identity not found; grant will not persist)"
  codesign --force --sign - "$APP"
fi

echo "==> signature:"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "Identifier|Signature|Authority|flags" || true

echo "==> done: $PWD/$APP/Contents/MacOS/$BIN"
echo "    Point [capture].sidecar_path at that path in meetflow.local.toml."
