import Foundation

// meetflow-capture — CoreAudio system-audio ("them") tap + optional mic ("me") sidecar.
// Usage: meetflow-capture --out <them.wav> [--mic-out <me.wav>] [--aec auto|on|off] [--sample-rate 16000]
// With --mic-out it also records the mic (Voice-Processing AEC when on speakers / aec on), so
// "me" and "them" come from one process and stay aligned. Records until SIGTERM/SIGINT, then
// flushes the WAV(s) and exits 0. Unknown flags are ignored (forward-compat).

struct Args {
    var out: String?
    var micOut: String?
    var aec = "auto"
    var sampleRate = 16000
}

func parseArgs(_ argv: [String]) -> Args {
    var a = Args()
    var i = 1
    while i < argv.count {
        switch argv[i] {
        case "--out":
            if i + 1 < argv.count { a.out = argv[i + 1]; i += 1 }
        case "--mic-out":
            if i + 1 < argv.count { a.micOut = argv[i + 1]; i += 1 }
        case "--aec":
            if i + 1 < argv.count { a.aec = argv[i + 1]; i += 1 }
        case "--sample-rate":
            if i + 1 < argv.count, let v = Int(argv[i + 1]) { a.sampleRate = v; i += 1 }
        case "--help", "-h":
            print("usage: meetflow-capture --out <them.wav> [--mic-out <me.wav>] [--aec auto|on|off] [--sample-rate 16000]")
            exit(0)
        default:
            break  // ignore unknown flags (forward-compat)
        }
        i += 1
    }
    return a
}

// One-time interactive grant: trigger the macOS system-audio prompt and report the result.
// Run this once from a normal login session; the tap needs kTCCServiceAudioCapture, which has
// no Settings toggle and must be requested via the SPI.
// Diagnostic: print the detected output route (no capture / no permission needed).
if CommandLine.arguments.contains("--probe-route") {
    Route.probe()
    exit(0)
}

if CommandLine.arguments.contains("--request-permission") {
    logErr("audio-capture permission before request: \(AudioCapturePermission.statusString())")
    let granted = AudioCapturePermission.request()
    logErr("audio-capture permission after request: \(granted ? "authorized" : "NOT granted")")
    exit(granted ? 0 : 1)
}

let args = parseArgs(CommandLine.arguments)
guard let outPath = args.out else {
    logErr("missing required --out <path>")
    exit(2)
}

let tap = ProcessTap(outputPath: outPath, targetSampleRate: args.sampleRate)

// Optional mic ("me") capture in the same process. AEC: explicit on/off, or auto = on only
// for built-in speakers (where the mic hears the far side).
var mic: MicCapture?
if let micOut = args.micOut {
    let route = Route.detect()
    let useAEC: Bool
    switch args.aec {
    case "on": useAEC = true
    case "off": useAEC = false
    default: useAEC = route.wantsAEC
    }
    logErr("mic: route=\(route.rawValue) aec=\(args.aec) -> VPIO \(useAEC ? "ON" : "off")")
    do {
        mic = try MicCapture(outputPath: micOut, sampleRate: args.sampleRate, useAEC: useAEC)
    } catch {
        logErr("mic capture init failed (\(error)); 'me' channel will be empty")
    }
}

// Take over SIGTERM/SIGINT so the default handler can't kill us mid-flush.
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let stopHandler: () -> Void = {
    mic?.stop()
    tap.stop()
    exit(0)
}
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigTerm.setEventHandler(handler: stopHandler)
sigInt.setEventHandler(handler: stopHandler)
sigTerm.resume()
sigInt.resume()

logErr("audio-capture permission: \(AudioCapturePermission.statusString())")
do {
    try tap.start()
} catch {
    logErr("\(error)")
    tap.writeStatus(state: "error", reason: "\(error)")
    exit(1)
}
do {
    try mic?.start()
} catch {
    logErr("mic start failed (\(error)); 'me' channel will be empty")
}

tap.writeStatus(state: "recording", reason: "")
dispatchMain()
