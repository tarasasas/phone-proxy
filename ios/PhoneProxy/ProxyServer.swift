import Foundation
import Network

/// Listens on a TCP port and hands each client to a ProxySession.
final class ProxyServer {
    enum State {
        case running
        case failed(String)
    }

    private let queue = DispatchQueue(label: "PhoneProxy.server", qos: .userInitiated)
    private let lock = NSLock()
    private var listener: NWListener?
    private var clients: [ObjectIdentifier: NWConnection] = [:]

    func start(config: ProxyConfig, stats: ProxyStats, onState: @escaping (State) -> Void) throws {
        stop()
        guard config.port != 0, let port = NWEndpoint.Port(rawValue: config.port) else {
            throw ProxyError.protocolError("Invalid port")
        }
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        let listener = try NWListener(using: params, on: port)

        listener.stateUpdateHandler = { [weak listener] state in
            switch state {
            case .ready:
                onState(.running)
            case .failed(let error), .waiting(let error):
                onState(.failed(Self.describe(error, port: config.port)))
                listener?.cancel()
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] conn in
            guard let self = self else {
                conn.cancel()
                return
            }
            self.track(conn)
            conn.start(queue: self.queue)
            let session = ProxySession(client: Channel(conn), config: config, stats: stats, queue: self.queue)
            Task {
                await session.run()
                self.untrack(conn)
            }
        }
        listener.start(queue: queue)

        lock.lock()
        self.listener = listener
        lock.unlock()
    }

    func stop() {
        lock.lock()
        let listener = self.listener
        let open = Array(clients.values)
        self.listener = nil
        clients.removeAll()
        lock.unlock()

        listener?.cancel()
        open.forEach { $0.cancel() }  // sessions notice and close their remote side
    }

    private static func describe(_ error: NWError, port: UInt16) -> String {
        if case .posix(.EADDRINUSE) = error {
            return "Port \(port) is already used by another app (often a VPN or proxy app). Pick a different port."
        }
        return error.localizedDescription
    }

    private func track(_ conn: NWConnection) {
        lock.lock()
        clients[ObjectIdentifier(conn)] = conn
        lock.unlock()
    }

    private func untrack(_ conn: NWConnection) {
        lock.lock()
        clients[ObjectIdentifier(conn)] = nil
        lock.unlock()
    }
}
