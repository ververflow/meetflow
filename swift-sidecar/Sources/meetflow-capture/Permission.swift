import Foundation

// System-audio recording permission via the private TCC SPI, mirroring insidegui/AudioCap.
//
// A CoreAudio process tap is gated by the `kTCCServiceAudioCapture` service. This is SEPARATE
// from Screen Recording — toggling the app under "Screen & System Audio Recording" does NOT
// grant it. There is no public API to check or request it, so we dlopen TCC.framework and call
// TCCAccessPreflight / TCCAccessRequest directly (the only known way; what every system-audio
// tool does). Without the grant the tap simply delivers silence.
enum AudioCapturePermission {
    private static var service: CFString { "kTCCServiceAudioCapture" as CFString }

    private typealias PreflightFn = @convention(c) (CFString, CFDictionary?) -> Int
    private typealias RequestFn = @convention(c) (CFString, CFDictionary?, @escaping (Bool) -> Void) -> Void

    // Immutable dlopen handle, opened once — safe to share.
    private nonisolated(unsafe) static let handle: UnsafeMutableRawPointer? =
        dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)

    private static func symbol<T>(_ name: String, as type: T.Type) -> T? {
        guard let handle, let sym = dlsym(handle, name) else { return nil }
        return unsafeBitCast(sym, to: T.self)
    }

    // 0 = authorized, 1 = denied, anything else = unknown/not-yet-asked.
    static func preflight() -> Int {
        guard let fn = symbol("TCCAccessPreflight", as: PreflightFn.self) else { return -1 }
        return fn(service, nil)
    }

    static func statusString() -> String {
        switch preflight() {
        case 0: return "authorized"
        case 1: return "denied"
        default: return "unknown"
        }
    }

    // Triggers the system prompt. Must run on the main thread of an interactive login session.
    // Spins the run loop (rather than blocking) so TCC's main-queue completion can fire.
    static func request(timeout: TimeInterval = 120) -> Bool {
        guard let fn = symbol("TCCAccessRequest", as: RequestFn.self) else {
            logErr("TCCAccessRequest SPI unavailable")
            return false
        }
        var granted = false
        var done = false
        fn(service, nil) { ok in
            granted = ok
            done = true
        }
        let deadline = Date().addingTimeInterval(timeout)
        while !done && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        if !done {
            logErr("permission request timed out")
            return false
        }
        return granted
    }
}
