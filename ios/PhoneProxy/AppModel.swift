import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum Status: Equatable {
        case stopped
        case starting
        case running
        case failed(String)
    }

    @Published private(set) var status: Status = .stopped
    @Published private(set) var stats = ProxyStats.Snapshot()
    @Published private(set) var addresses: [InterfaceAddress] = []

    private let server = ProxyServer()
    private let counters = ProxyStats()
    private let keepAlive = KeepAlive()
    private var config: ProxyConfig?

    /// True from Start until Stop, even if the listener dropped and needs recovery.
    var isOn: Bool { config != nil }

    func start(_ config: ProxyConfig, audio: Bool, location: Bool) {
        self.config = config
        counters.reset()
        launchServer()
        if audio { keepAlive.startAudio() }
        if location { keepAlive.startLocation() }
        refresh()
    }

    func stop() {
        config = nil
        server.stop()
        keepAlive.stop()
        status = .stopped
    }

    /// Called when the app comes back to the foreground: if iOS killed the
    /// listener while we were in the background, bring it back.
    func recoverIfNeeded() {
        guard config != nil, case .failed = status else { return }
        launchServer()
    }

    func refresh() {
        stats = counters.snapshot()
        let now = reachableAddresses()
        if now != addresses { addresses = now }
    }

    private func launchServer() {
        guard let config = config else { return }
        status = .starting
        do {
            try server.start(config: config, stats: counters) { [weak self] state in
                Task { @MainActor in self?.handle(state) }
            }
        } catch {
            status = .failed(String(describing: error))
        }
    }

    private func handle(_ state: ProxyServer.State) {
        guard config != nil else { return }
        switch state {
        case .running: status = .running
        case .failed(let message): status = .failed(message)
        }
    }
}
