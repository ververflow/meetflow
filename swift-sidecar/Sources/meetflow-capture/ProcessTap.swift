import AudioToolbox
import CoreAudio
import Foundation
import Synchronization

// MeetFlow CoreAudio process-tap sidecar.
//
// Pipeline: a global process-tap (all apps' output = the far side, "them") feeds a private
// aggregate device; an IOProc (realtime) downmixes each callback to mono and pushes it into a
// lock-free ring buffer; a drain thread resamples native→target and streams a Float32 mono WAV
// to disk. The IOProc never allocates, locks, or logs. Every public failure surfaces as a thrown
// error / non-zero exit so the Python side degrades to a mic-only recording.

func logErr(_ msg: String) {
    FileHandle.standardError.write(Data("meetflow-capture: \(msg)\n".utf8))
}

enum CaptureError: Error, CustomStringConvertible {
    case noDefaultOutput(OSStatus)
    case tapCreate(OSStatus)
    case tapFormat(OSStatus)
    case aggregateCreate(OSStatus)
    case ioProcCreate(OSStatus)
    case deviceStart(OSStatus)
    case unsupportedFormat(String)
    case fileOpen(String)

    var description: String {
        switch self {
        case .noDefaultOutput(let s): return "could not read default output device (OSStatus \(s))"
        case .tapCreate(let s): return "AudioHardwareCreateProcessTap failed (OSStatus \(s))"
        case .tapFormat(let s): return "could not read tap format (OSStatus \(s))"
        case .aggregateCreate(let s): return "AudioHardwareCreateAggregateDevice failed (OSStatus \(s))"
        case .ioProcCreate(let s): return "AudioDeviceCreateIOProcIDWithBlock failed (OSStatus \(s))"
        case .deviceStart(let s): return "AudioDeviceStart failed (OSStatus \(s)) — likely missing audio-capture permission"
        case .unsupportedFormat(let m): return "unsupported tap format: \(m)"
        case .fileOpen(let p): return "could not open output file \(p)"
        }
    }
}

// A shareable stop signal (Atomic is ~Copyable, so it can't be captured by value).
final class Flag: @unchecked Sendable {
    private let a = Atomic<Bool>(false)
    func set() -> Bool { a.exchange(true, ordering: .acquiringAndReleasing) }  // returns previous
    var isSet: Bool { a.load(ordering: .acquiring) }
}

// MARK: - Lock-free single-producer/single-consumer ring buffer (mono Float32)

// @unchecked Sendable: a single producer (IOProc) and single consumer (drain thread)
// coordinated by atomic monotonic indices — safe to share across the two threads.
final class RingBuffer: @unchecked Sendable {
    private let storage: UnsafeMutablePointer<Float>
    private let capacity: Int
    private let writeIdx = Atomic<Int>(0)  // produced count (monotonic)
    private let readIdx = Atomic<Int>(0)   // consumed count (monotonic)
    let dropped = Atomic<Int>(0)

    init(capacity: Int) {
        self.capacity = capacity
        self.storage = UnsafeMutablePointer<Float>.allocate(capacity: capacity)
        self.storage.initialize(repeating: 0, count: capacity)
    }

    deinit {
        storage.deinitialize(count: capacity)
        storage.deallocate()
    }

    // Producer (IOProc). Drops on overflow rather than blocking.
    func push(_ src: UnsafePointer<Float>, _ count: Int) {
        let w = writeIdx.load(ordering: .relaxed)
        let r = readIdx.load(ordering: .acquiring)
        let free = capacity - (w - r)
        let n = min(count, free)
        if n < count { dropped.add(count - n, ordering: .relaxed) }
        if n <= 0 { return }
        for i in 0..<n { storage[(w + i) % capacity] = src[i] }
        writeIdx.store(w + n, ordering: .releasing)
    }

    // Consumer (drain thread). Returns frames copied into dst (up to maxCount).
    func pop(_ dst: UnsafeMutablePointer<Float>, _ maxCount: Int) -> Int {
        let r = readIdx.load(ordering: .relaxed)
        let w = writeIdx.load(ordering: .acquiring)
        let avail = w - r
        let n = min(maxCount, avail)
        if n <= 0 { return 0 }
        for i in 0..<n { dst[i] = storage[(r + i) % capacity] }
        readIdx.store(r + n, ordering: .releasing)
        return n
    }
}

// MARK: - Streaming linear resampler (mono), carry across chunks. Matches np.interp.

struct LinearResampler {
    private let ratio: Double  // src / dst
    private var pos: Double = 0  // next output position, in input samples (carry sits at -1)
    private var carry: Float = 0  // last input sample of the previous chunk

    init(from srcRate: Double, to dstRate: Double) {
        self.ratio = srcRate / dstRate
    }

    // Resamples `count` mono input samples into `out` (cap >= count for downsampling). Returns produced.
    mutating func process(_ input: UnsafePointer<Float>, _ count: Int, _ out: UnsafeMutablePointer<Float>) -> Int {
        if count == 0 { return 0 }
        @inline(__always) func sample(_ idx: Int) -> Float { idx < 0 ? carry : input[idx] }
        var produced = 0
        while true {
            let i0 = Int(floor(pos))
            if i0 + 1 > count - 1 { break }  // need a right neighbour to interpolate
            let frac = Float(pos - Double(i0))
            out[produced] = sample(i0) * (1 - frac) + sample(i0 + 1) * frac
            produced += 1
            pos += ratio
        }
        carry = input[count - 1]
        pos -= Double(count)  // shift origin: next chunk's index 0 == current index `count`
        return produced
    }
}

// MARK: - Minimal streaming WAV writer (mono, IEEE Float32)

// @unchecked Sendable: only the drain thread appends; main closes it after the drain thread
// has exited (ordered by `drainDone`), so there is never concurrent access.
final class WavWriter: @unchecked Sendable {
    private let handle: FileHandle
    private let sampleRate: Int
    private var sampleCount = 0

    init(path: String, sampleRate: Int) throws {
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let h = FileHandle(forWritingAtPath: path) else { throw CaptureError.fileOpen(path) }
        self.handle = h
        self.sampleRate = sampleRate
        try? handle.write(contentsOf: Self.header(sampleRate: sampleRate, sampleCount: 0))
    }

    func append(_ samples: UnsafePointer<Float>, _ count: Int) {
        if count <= 0 { return }
        let data = Data(bytes: samples, count: count * MemoryLayout<Float>.size)
        try? handle.write(contentsOf: data)
        sampleCount += count
    }

    // Backfill the header with final sizes and close.
    func close() {
        try? handle.seek(toOffset: 0)
        try? handle.write(contentsOf: Self.header(sampleRate: sampleRate, sampleCount: sampleCount))
        try? handle.close()
    }

    var seconds: Double { Double(sampleCount) / Double(sampleRate) }

    private static func header(sampleRate: Int, sampleCount: Int) -> Data {
        let channels: UInt16 = 1
        let bits: UInt16 = 32
        let blockAlign = UInt16(channels) * bits / 8
        let byteRate = UInt32(sampleRate) * UInt32(blockAlign)
        let dataSize = UInt32(sampleCount * Int(blockAlign))
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        func ascii(_ s: String) { d.append(Data(s.utf8)) }
        ascii("RIFF"); u32(36 + dataSize); ascii("WAVE")
        ascii("fmt "); u32(16); u16(3)  // 3 = IEEE float
        u16(channels); u32(UInt32(sampleRate)); u32(byteRate); u16(blockAlign); u16(bits)
        ascii("data"); u32(dataSize)
        return d
    }
}

// MARK: - CoreAudio property helpers

private func defaultOutputDeviceUID() -> (AudioObjectID, String)? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var devID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devID)
    if st != noErr || devID == kAudioObjectUnknown { return nil }

    var uidAddr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var uid: CFString = "" as CFString
    var uidSize = UInt32(MemoryLayout<CFString>.size)
    let st2 = withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(devID, &uidAddr, 0, nil, &uidSize, $0)
    }
    if st2 != noErr { return nil }
    return (devID, uid as String)
}

// Read a device's stream format on a given scope (input for what an IOProc receives).
private func deviceStreamFormat(_ devID: AudioObjectID, scope: AudioObjectPropertyScope) -> AudioStreamBasicDescription? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamFormat,
        mScope: scope,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let st = AudioObjectGetPropertyData(devID, &addr, 0, nil, &size, &asbd)
    return st == noErr ? asbd : nil
}

// Fallback: a device's nominal sample rate (when the stream format read is unavailable).
private func deviceNominalSampleRate(_ devID: AudioObjectID) -> Double? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var rate: Float64 = 0
    var size = UInt32(MemoryLayout<Float64>.size)
    let st = AudioObjectGetPropertyData(devID, &addr, 0, nil, &size, &rate)
    return (st == noErr && rate > 0) ? rate : nil
}

// MARK: - ProcessTap

final class ProcessTap {
    private let outputPath: String
    private let targetRate: Int

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let ioQueue = DispatchQueue(label: "com.ververflow.meetflow.tap.io")

    private var ring: RingBuffer!
    private var writer: WavWriter!
    private var ioScratch: UnsafeMutablePointer<Float>!
    private var ioScratchCap = 16384

    private var channelCount = 2
    private var isInterleaved = true
    private var nativeRate: Double = 48000

    private let stopRequested = Flag()
    private let drainDone = DispatchSemaphore(value: 0)
    private var started = false

    init(outputPath: String, targetSampleRate: Int) {
        self.outputPath = outputPath
        self.targetRate = targetSampleRate
    }

    func start() throws {
        guard let (_, outputUID) = defaultOutputDeviceUID() else {
            throw CaptureError.noDefaultOutput(0)
        }

        // 1) Global tap: all processes' output (exclude none) = the far side ("them").
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        desc.name = "MeetFlow Tap"
        desc.uuid = UUID()  // Swift-refined name (SDK aliases the ObjC `UUID`)
        desc.muteBehavior = .unmuted  // must NOT silence the call the user hears
        desc.isPrivate = true
        desc.isExclusive = true

        var st = AudioHardwareCreateProcessTap(desc, &tapID)
        guard st == noErr, tapID != kAudioObjectUnknown else { throw CaptureError.tapCreate(st) }

        // 2) Native tap format — never assume; read and log it (probe #2).
        var fmtAddr = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var asbdSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        st = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &asbdSize, &asbd)
        guard st == noErr else { throw CaptureError.tapFormat(st) }

        nativeRate = asbd.mSampleRate > 0 ? asbd.mSampleRate : 48000
        channelCount = max(1, Int(asbd.mChannelsPerFrame))
        isInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        logErr("tap format: \(nativeRate) Hz, \(channelCount) ch, interleaved=\(isInterleaved), float=\(isFloat), formatID=\(asbd.mFormatID)")
        guard isFloat, asbd.mBitsPerChannel == 32 else {
            throw CaptureError.unsupportedFormat("expected 32-bit float, got \(asbd.mBitsPerChannel)-bit (float=\(isFloat))")
        }

        // 3) Private aggregate device anchored to the default output's clock, containing the tap.
        let aggUID = "com.ververflow.meetflow.tap-\(UUID().uuidString)"
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "MeetFlow Capture",
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: true,
                kAudioSubTapUIDKey: desc.uuid.uuidString,
            ]],
        ]
        st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
        guard st == noErr, aggID != kAudioObjectUnknown else { throw CaptureError.aggregateCreate(st) }

        // 3b) The IOProc reads from the AGGREGATE device, whose delivered rate can differ from
        // the tap's advertised rate: when the mic is captured in the same process, Voice-
        // Processing IO reconfigures the HAL graph so the aggregate runs at 16 kHz instead of
        // 48 kHz. Trusting the tap rate here was the AEC "3x-too-long, 0-transcript" bug. Read
        // the aggregate's ACTUAL input format and drive the ring/resampler/downmix from THAT.
        if let aggFmt = deviceStreamFormat(aggID, scope: kAudioObjectPropertyScopeInput), aggFmt.mSampleRate > 0 {
            nativeRate = aggFmt.mSampleRate
            channelCount = max(1, Int(aggFmt.mChannelsPerFrame))
            isInterleaved = (aggFmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        } else if let r = deviceNominalSampleRate(aggID) {
            nativeRate = r
        }
        logErr("aggregate input format: \(nativeRate) Hz, \(channelCount) ch, interleaved=\(isInterleaved)")

        // 4) Buffers + writer.
        ring = RingBuffer(capacity: Int(nativeRate) * 8)  // ~8s headroom
        ioScratch = UnsafeMutablePointer<Float>.allocate(capacity: ioScratchCap)
        writer = try WavWriter(path: outputPath, sampleRate: targetRate)

        // 5) IOProc on the aggregate — realtime: downmix to mono, push to ring, nothing else.
        let ring = self.ring!
        let scratch = self.ioScratch!
        let scratchCap = self.ioScratchCap
        let channels = self.channelCount
        let interleaved = self.isInterleaved
        st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) {
            _, inInputData, _, _, _ in
            let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
            if abl.count == 0 { return }
            if interleaved {
                let buf = abl[0]
                guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { return }
                let frames = Int(buf.mDataByteSize) / (MemoryLayout<Float>.size * channels)
                var f = 0
                while f < frames {
                    let take = min(scratchCap, frames - f)
                    for k in 0..<take {
                        let base = (f + k) * channels
                        var acc: Float = 0
                        for c in 0..<channels { acc += p[base + c] }
                        scratch[k] = acc / Float(channels)
                    }
                    ring.push(scratch, take)
                    f += take
                }
            } else {
                let frames = Int(abl[0].mDataByteSize) / MemoryLayout<Float>.size
                let nbuf = min(channels, abl.count)
                var f = 0
                while f < frames {
                    let take = min(scratchCap, frames - f)
                    for k in 0..<take {
                        var acc: Float = 0
                        for c in 0..<nbuf {
                            if let cp = abl[c].mData?.assumingMemoryBound(to: Float.self) { acc += cp[f + k] }
                        }
                        scratch[k] = acc / Float(nbuf)
                    }
                    ring.push(scratch, take)
                    f += take
                }
            }
        }
        guard st == noErr, procID != nil else { throw CaptureError.ioProcCreate(st) }

        startDrainThread()

        st = AudioDeviceStart(aggID, procID)  // TCC audio-capture is enforced here
        guard st == noErr else { throw CaptureError.deviceStart(st) }

        started = true
    }

    private func startDrainThread() {
        let ring = self.ring!
        let writer = self.writer!
        let stopRequested = self.stopRequested
        let drainDone = self.drainDone
        let srcRate = self.nativeRate
        let dstRate = self.targetRate

        let thread = Thread {
            var resampler = LinearResampler(from: srcRate, to: Double(dstRate))
            let cap = 8192
            let popBuf = UnsafeMutablePointer<Float>.allocate(capacity: cap)
            let outBuf = UnsafeMutablePointer<Float>.allocate(capacity: cap + 8)
            defer { popBuf.deallocate(); outBuf.deallocate() }

            func drainOnce() -> Bool {
                let got = ring.pop(popBuf, cap)
                if got <= 0 { return false }
                let produced = resampler.process(popBuf, got, outBuf)
                writer.append(outBuf, produced)
                return true
            }

            while !stopRequested.isSet {
                if !drainOnce() { usleep(15_000) }
            }
            while drainOnce() {}  // final flush of whatever the IOProc left
            drainDone.signal()
        }
        thread.stackSize = 512 * 1024
        thread.start()
    }

    // Ordered teardown (idempotent). Safe to call from the signal handler.
    func stop() {
        if stopRequested.set() { return }
        if started, procID != nil { AudioDeviceStop(aggID, procID) }
        _ = drainDone.wait(timeout: .now() + 12)
        if let p = procID { AudioDeviceDestroyIOProcID(aggID, p); procID = nil }
        if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID); aggID = kAudioObjectUnknown }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID); tapID = kAudioObjectUnknown }
        writer?.close()
        if let dropped = ring?.dropped.load(ordering: .relaxed), dropped > 0 {
            logErr("WARNING: dropped \(dropped) frames on ring overflow")
        }
        logErr("captured \(String(format: "%.1f", writer?.seconds ?? 0))s → \(outputPath)")
        writeStatus(state: "stopped", reason: "")
    }

    func writeStatus(state: String, reason: String) {
        let dir = (outputPath as NSString).deletingLastPathComponent
        let path = (dir as NSString).appendingPathComponent("capture-status.json")
        let payload: [String: Any] = [
            "state": state,
            "reason": reason,
            "pid": ProcessInfo.processInfo.processIdentifier,
            "tap_format": ["sr": nativeRate, "ch": channelCount],
        ]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: URL(fileURLWithPath: path))
        }
    }
}
