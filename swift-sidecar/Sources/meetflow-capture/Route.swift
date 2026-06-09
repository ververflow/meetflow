import CoreAudio
import Foundation

// Detects where system audio is going, to decide whether echo cancellation is worth it.
// AEC only helps when you're on the BUILT-IN SPEAKERS (the mic hears the far side out loud).
// On headphones / Bluetooth / external the mic doesn't pick up the far side, so AEC is skipped
// (it would only add latency and degrade the recording).

enum OutputRoute: String {
    case builtInSpeakers
    case builtInHeadphones
    case bluetooth
    case usb
    case external
    case unknown

    // The only case where Voice-Processing AEC earns its place.
    var wantsAEC: Bool { self == .builtInSpeakers }
}

enum Route {
    static func fourCC(_ v: UInt32) -> String {
        let bytes = [UInt8((v >> 24) & 0xff), UInt8((v >> 16) & 0xff), UInt8((v >> 8) & 0xff), UInt8(v & 0xff)]
        return String(bytes: bytes, encoding: .ascii) ?? "????"
    }

    private static func defaultOutputDevice() -> AudioObjectID? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var dev = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev)
        return (st == noErr && dev != kAudioObjectUnknown) ? dev : nil
    }

    private static func uint32(_ dev: AudioObjectID,
                               _ selector: AudioObjectPropertySelector,
                               scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectHasProperty(dev, &addr) else { return nil }
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let st = AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &value)
        return st == noErr ? value : nil
    }

    private static func dataSource(_ dev: AudioObjectID) -> UInt32? {
        uint32(dev, kAudioDevicePropertyDataSource, scope: kAudioObjectPropertyScopeOutput)
    }

    static func detect() -> OutputRoute {
        guard let dev = defaultOutputDevice() else { return .unknown }
        let transport = uint32(dev, kAudioDevicePropertyTransportType) ?? 0
        switch transport {
        case kAudioDeviceTransportTypeBuiltIn:
            // Built-in output exposes a data source: speaker vs headphone jack.
            if let ds = dataSource(dev) {
                switch fourCC(ds) {
                case "ispk": return .builtInSpeakers
                case "hdpn": return .builtInHeadphones
                default: break
                }
            }
            return .builtInSpeakers  // no data source reported → assume speakers
        case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE:
            return .bluetooth
        case kAudioDeviceTransportTypeUSB:
            return .usb
        default:
            return .external
        }
    }

    // --probe-route: print raw transport + data-source 4CCs so we can confirm the mapping
    // on this exact hardware (the ispk/hdpn codes are runtime values, not header constants).
    static func probe() {
        guard let dev = defaultOutputDevice() else {
            logErr("route probe: no default output device")
            return
        }
        let transport = uint32(dev, kAudioDevicePropertyTransportType) ?? 0
        let ds = dataSource(dev)
        logErr("route probe: transport=\(fourCC(transport)) dataSource=\(ds.map(fourCC) ?? "n/a") => \(detect().rawValue)")
    }
}
