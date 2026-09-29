import Foundation

/// Thread-safe counters shared by all sessions; the UI polls `snapshot()`.
final class ProxyStats {
    struct Snapshot {
        var active = 0
        var total = 0
        var up: Int64 = 0
        var down: Int64 = 0
        var recent: [String] = []
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    private let lock = NSLock()
    private var s = Snapshot()

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func snapshot() -> Snapshot { locked { s } }

    func opened() { locked { s.active += 1; s.total += 1 } }
    func closed() { locked { s.active -= 1 } }
    func addUp(_ n: Int) { locked { s.up += Int64(n) } }
    func addDown(_ n: Int) { locked { s.down += Int64(n) } }

    func log(_ line: String) {
        let stamped = "\(Self.clock.string(from: Date()))  \(line)"
        locked {
            s.recent.insert(stamped, at: 0)
            if s.recent.count > 100 { s.recent.removeLast() }
        }
    }

    /// Clears totals and the log; live connections keep counting.
    func reset() {
        locked { s = Snapshot(active: s.active) }
    }
}
