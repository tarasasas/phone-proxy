import AVFoundation
import CoreLocation

/// iOS suspends apps shortly after they leave the screen, which would kill the
/// proxy. Two ways to stay awake, both declared in UIBackgroundModes:
///   * silent audio  – no permission prompt, but a phone call or another app
///                     taking exclusive audio can interrupt it (we resume after)
///   * location      – most reliable, shows the blue location pill
final class KeepAlive: NSObject, CLLocationManagerDelegate {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var audioOn = false
    private var locationManager: CLLocationManager?

    override init() {
        super.init()
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(audioInterrupted(_:)),
                           name: AVAudioSession.interruptionNotification, object: nil)
        center.addObserver(self, selector: #selector(audioConfigChanged(_:)),
                           name: .AVAudioEngineConfigurationChange, object: engine)
    }

    // MARK: Silent audio

    func startAudio() {
        audioOn = true
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, options: [.mixWithOthers])
        try? session.setActive(true)

        guard let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 44_100) else { return }
        buffer.frameLength = buffer.frameCapacity
        if let samples = buffer.floatChannelData {
            memset(samples[0], 0, Int(buffer.frameLength) * MemoryLayout<Float>.size)
        }

        if player.engine == nil {
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: format)
        }
        player.stop()
        player.scheduleBuffer(buffer, at: nil, options: .loops)
        do {
            try engine.start()
            player.play()
        } catch {
            print("KeepAlive: audio engine failed: \(error)")
        }
    }

    private func resumeAudio() {
        guard audioOn else { return }
        startAudio()
    }

    @objc private func audioInterrupted(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
        DispatchQueue.main.async { self.resumeAudio() }
    }

    @objc private func audioConfigChanged(_ note: Notification) {
        DispatchQueue.main.async { self.resumeAudio() }
    }

    // MARK: Location

    func startLocation() {
        let manager = CLLocationManager()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyThreeKilometers
        manager.distanceFilter = CLLocationDistanceMax
        manager.pausesLocationUpdatesAutomatically = false
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = true
        manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()
        locationManager = manager
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {}
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    // MARK: Stop

    func stop() {
        audioOn = false
        player.stop()
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)

        locationManager?.stopUpdatingLocation()
        locationManager?.allowsBackgroundLocationUpdates = false
        locationManager = nil
    }
}
