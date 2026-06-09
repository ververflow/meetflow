#!/usr/bin/env bash
# One-time: ask macOS for system-audio (kTCCServiceAudioCapture) permission.
# Launch via `open` (LaunchServices) so the app is its OWN responsible process and can
# raise the TCC prompt — a binary started as a child of an (embedded) terminal cannot.
APP=/Users/daniverver/code/tools/meetflow/swift-sidecar/MeetFlowCapture.app
open -n "$APP" --args --request-permission
echo "Opened MeetFlowCapture. A 'wants to record audio' dialog should appear — click Allow."
echo "Then verify with:  ~/vt"
