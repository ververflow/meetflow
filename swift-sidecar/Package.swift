// swift-tools-version:6.0
import PackageDescription

// MeetFlow CoreAudio capture sidecar (Phase 4).
// A tiny CLI that taps system output audio ("them") via a CoreAudio process-tap and
// writes a 16 kHz mono Float32 WAV. The Python daemon spawns it per meeting and reads
// the WAV back through LoopbackStream. macOS 15+ (Atomic / process-tap APIs).
let package = Package(
    name: "meetflow-capture",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(
            name: "meetflow-capture",
            path: "Sources/meetflow-capture"
        )
    ]
)
