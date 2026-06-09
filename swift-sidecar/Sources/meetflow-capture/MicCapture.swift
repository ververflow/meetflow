import AVFoundation
import Foundation

// Microphone capture for the "me" channel, with optional Apple Voice-Processing AEC.
//
// On built-in speakers the mic picks up the far side out loud; Voice-Processing IO (VPIO)
// uses the system output as an echo reference and cancels it, leaving a clean "me" track.
// On headphones/external there is no echo, so AEC is skipped. Writes a 16 kHz mono Float32 WAV
// (same WavWriter as the tap), so "me" and "them" come from ONE process and stay time-aligned.
final class MicCapture: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let writer: WavWriter
    private let converter: AVAudioConverter
    private let outFormat: AVAudioFormat
    private(set) var aecEnabled = false

    init(outputPath: String, sampleRate: Int, useAEC: Bool) throws {
        self.writer = try WavWriter(path: outputPath, sampleRate: sampleRate)

        let input = engine.inputNode
        if useAEC {
            do {
                try input.setVoiceProcessingEnabled(true)
                aecEnabled = true
                input.isVoiceProcessingAGCEnabled = false  // no surprise auto-gain on "me"
            } catch {
                logErr("VPIO AEC enable failed (\(error)); using plain mic")
            }
        }

        // Read the hardware format AFTER toggling VPIO (VPIO changes it).
        let hwFormat = input.outputFormat(forBus: 0)
        guard let out = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                      sampleRate: Double(sampleRate), channels: 1, interleaved: false),
              let conv = AVAudioConverter(from: hwFormat, to: out) else {
            throw CaptureError.unsupportedFormat("mic \(hwFormat) -> 16k mono")
        }
        self.outFormat = out
        self.converter = conv

        let srcRate = hwFormat.sampleRate
        let writer = self.writer
        input.installTap(onBus: 0, bufferSize: 4096, format: hwFormat) { buffer, _ in
            let cap = AVAudioFrameCount(Double(buffer.frameLength) * Double(sampleRate) / srcRate) + 64
            guard cap > 0, let outBuf = AVAudioPCMBuffer(pcmFormat: out, frameCapacity: cap) else { return }
            var fed = false
            var err: NSError?
            conv.convert(to: outBuf, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true
                status.pointee = .haveData
                return buffer
            }
            if let ch = outBuf.floatChannelData, outBuf.frameLength > 0 {
                writer.append(ch[0], Int(outBuf.frameLength))
            }
        }
    }

    func start() throws {
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        writer.close()
    }

    var seconds: Double { writer.seconds }
}
