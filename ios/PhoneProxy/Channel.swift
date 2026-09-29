import Foundation
import Network

enum ProxyError: Error {
    case closed
    case timeout
    case protocolError(String)
}

extension NWParameters {
    /// TCP tuned for latency. A proxy relays lots of small writes (TLS
    /// handshakes, requests); with Nagle + delayed/stretched ACKs each one
    /// can stall for up to ~200 ms waiting on the previous segment's ACK.
    static func lowLatencyTCP() -> NWParameters {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.disableAckStretching = true
        return NWParameters(tls: nil, tcp: tcp)
    }
}

/// Fires exactly once, so a continuation is never resumed twice.
final class Once {
    private let lock = NSLock()
    private var done = false

    func fire() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if done { return false }
        done = true
        return true
    }
}

/// Buffered async reads/writes over an NWConnection.
final class Channel {
    let conn: NWConnection
    private var buffer = Data()
    private var eof = false

    init(_ conn: NWConnection) {
        self.conn = conn
    }

    // MARK: Reading

    /// Next chunk of bytes, or nil at end of stream.
    func read() async throws -> Data? {
        if !buffer.isEmpty {
            let out = buffer
            buffer = Data()
            return out
        }
        while true {
            guard let chunk = try await receive() else { return nil }
            if !chunk.isEmpty { return chunk }
        }
    }

    func readExactly(_ n: Int) async throws -> Data {
        while buffer.count < n {
            guard let chunk = try await receive() else { throw ProxyError.closed }
            buffer.append(chunk)
        }
        let out = Data(buffer.prefix(n))
        buffer.removeFirst(n)
        return out
    }

    func readByte() async throws -> UInt8 {
        try await readExactly(1)[0]
    }

    /// Reads up to and including `delimiter`.
    func readUntil(_ delimiter: Data, limit: Int = 64 * 1024) async throws -> Data {
        while true {
            if let r = buffer.range(of: delimiter) {
                let out = Data(buffer[buffer.startIndex..<r.upperBound])
                buffer.removeSubrange(buffer.startIndex..<r.upperBound)
                return out
            }
            if buffer.count > limit { throw ProxyError.protocolError("header too large") }
            guard let chunk = try await receive() else { throw ProxyError.closed }
            buffer.append(chunk)
        }
    }

    /// One raw receive. nil = peer finished sending; empty Data = nothing yet.
    private func receive() async throws -> Data? {
        if eof { return nil }
        let (data, complete): (Data?, Bool) = try await withCheckedThrowingContinuation { cont in
            conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, isComplete, error in
                if let error = error, data?.isEmpty ?? true {
                    cont.resume(throwing: error)
                } else {
                    cont.resume(returning: (data, isComplete))
                }
            }
        }
        if complete { eof = true }
        if let data = data, !data.isEmpty { return data }
        return complete ? nil : Data()
    }

    // MARK: Writing

    func write(_ data: Data) async throws {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            conn.send(content: data, completion: .contentProcessed { error in
                if let error = error { cont.resume(throwing: error) } else { cont.resume() }
            })
        }
    }

    func write(_ text: String) async throws {
        try await write(Data(text.utf8))
    }

    /// Half-close: tell the peer we're done sending.
    func finishWriting() {
        conn.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .idempotent)
    }

    func close() {
        conn.cancel()
    }

    // MARK: Outbound

    static func connect(to host: NWEndpoint.Host, port: UInt16, cellularOnly: Bool,
                        queue: DispatchQueue, timeout: TimeInterval = 15) async throws -> Channel {
        guard let nwPort = NWEndpoint.Port(rawValue: port), port != 0 else {
            throw ProxyError.protocolError("invalid port")
        }
        let params = NWParameters.lowLatencyTCP()
        if cellularOnly { params.requiredInterfaceType = .cellular }
        let conn = NWConnection(host: host, port: nwPort, using: params)

        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            let once = Once()
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if once.fire() { cont.resume() }
                case .failed(let error), .waiting(let error):
                    // .waiting means no route / DNS failure; for a proxy that's a failure.
                    if once.fire() { conn.cancel(); cont.resume(throwing: error) }
                case .cancelled:
                    if once.fire() { cont.resume(throwing: ProxyError.closed) }
                default:
                    break
                }
            }
            queue.asyncAfter(deadline: .now() + timeout) {
                if once.fire() { conn.cancel(); cont.resume(throwing: ProxyError.timeout) }
            }
            conn.start(queue: queue)
        }
        conn.stateUpdateHandler = nil
        return Channel(conn)
    }

    // MARK: Relay

    /// Copies bytes both ways until either side closes, then closes both.
    static func relay(_ client: Channel, _ remote: Channel, stats: ProxyStats) async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await pump(from: client, to: remote, count: stats.addUp) }
            group.addTask { await pump(from: remote, to: client, count: stats.addDown) }
        }
        client.close()
        remote.close()
    }

    private static func pump(from src: Channel, to dst: Channel, count: @escaping (Int) -> Void) async {
        do {
            while let data = try await src.read() {
                try await dst.write(data)
                count(data.count)
            }
            dst.finishWriting()
        } catch {
            src.close()
            dst.close()
        }
    }
}
