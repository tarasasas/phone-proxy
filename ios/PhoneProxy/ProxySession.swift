import Foundation
import Network

struct ProxyConfig: Equatable {
    struct Credentials: Equatable {
        var user: String
        var password: String
    }

    var port: UInt16
    var cellularOnly: Bool
    var credentials: Credentials?
}

/// Handles one client connection. The first byte decides the protocol:
/// 0x05 = SOCKS5, anything else = HTTP proxy request.
final class ProxySession {
    private let client: Channel
    private let config: ProxyConfig
    private let stats: ProxyStats
    private let queue: DispatchQueue

    init(client: Channel, config: ProxyConfig, stats: ProxyStats, queue: DispatchQueue) {
        self.client = client
        self.config = config
        self.stats = stats
        self.queue = queue
    }

    func run() async {
        stats.opened()
        do {
            let first = try await client.readByte()
            if first == 0x05 {
                try await socks5()
            } else {
                try await http(firstByte: first)
            }
        } catch {
            // Client went away or sent garbage; nothing to report.
        }
        client.close()
        stats.closed()
    }

    private func connectRemote(_ host: NWEndpoint.Host, _ port: UInt16) async throws -> Channel {
        try await Channel.connect(to: host, port: port, cellularOnly: config.cellularOnly, queue: queue)
    }

    // MARK: - SOCKS5 (RFC 1928 / RFC 1929)

    private func socks5() async throws {
        let methodCount = Int(try await client.readByte())
        let methods = try await client.readExactly(methodCount)

        if let creds = config.credentials {
            guard methods.contains(0x02) else {
                try await client.write(Data([0x05, 0xFF]))
                return
            }
            try await client.write(Data([0x05, 0x02]))
            let version = try await client.readByte()
            let userLength = Int(try await client.readByte())
            let user = String(decoding: try await client.readExactly(userLength), as: UTF8.self)
            let passLength = Int(try await client.readByte())
            let pass = String(decoding: try await client.readExactly(passLength), as: UTF8.self)
            guard version == 0x01, user == creds.user, pass == creds.password else {
                try await client.write(Data([0x01, 0x01]))
                return
            }
            try await client.write(Data([0x01, 0x00]))
        } else {
            guard methods.contains(0x00) else {
                try await client.write(Data([0x05, 0xFF]))
                return
            }
            try await client.write(Data([0x05, 0x00]))
        }

        // VER CMD RSV ATYP DST.ADDR DST.PORT
        let request = try await client.readExactly(4)
        let command = request[1]
        let host: NWEndpoint.Host
        switch request[3] {
        case 0x01:
            guard let ip = IPv4Address(try await client.readExactly(4)) else { throw ProxyError.protocolError("bad IPv4") }
            host = .ipv4(ip)
        case 0x03:
            let length = Int(try await client.readByte())
            host = NWEndpoint.Host(String(decoding: try await client.readExactly(length), as: UTF8.self))
        case 0x04:
            guard let ip = IPv6Address(try await client.readExactly(16)) else { throw ProxyError.protocolError("bad IPv6") }
            host = .ipv6(ip)
        default:
            try await client.write(socksReply(0x08))
            return
        }
        let portBytes = try await client.readExactly(2)
        let port = UInt16(portBytes[0]) << 8 | UInt16(portBytes[1])

        guard command == 0x01 else {  // only CONNECT
            try await client.write(socksReply(0x07))
            return
        }

        let remote: Channel
        do {
            remote = try await connectRemote(host, port)
        } catch {
            stats.log("✗ \(host):\(port)")
            try await client.write(socksReply(socksCode(for: error)))
            return
        }
        do {
            try await client.write(socksReply(0x00))
        } catch {
            remote.close()
            throw error
        }
        stats.log("SOCKS  \(host):\(port)")
        await Channel.relay(client, remote, stats: stats)
    }

    private func socksReply(_ code: UInt8) -> Data {
        // Bound address 0.0.0.0:0 — clients don't use it for CONNECT.
        Data([0x05, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0])
    }

    private func socksCode(for error: Error) -> UInt8 {
        if case ProxyError.timeout? = error as? ProxyError { return 0x04 }
        guard let nwError = error as? NWError else { return 0x01 }
        switch nwError {
        case .dns:
            return 0x04
        case .posix(let code):
            switch code {
            case .ECONNREFUSED: return 0x05
            case .ENETUNREACH, .ENETDOWN: return 0x03
            case .EHOSTUNREACH, .EHOSTDOWN, .ETIMEDOUT: return 0x04
            default: return 0x01
            }
        default:
            return 0x01
        }
    }

    // MARK: - HTTP proxy (CONNECT tunnels + plain http:// requests)

    private func http(firstByte: UInt8) async throws {
        var head = Data([firstByte])
        head.append(try await client.readUntil(Data("\r\n\r\n".utf8)))
        guard let text = String(data: head, encoding: .isoLatin1) else { return }

        var lines = text.components(separatedBy: "\r\n").filter { !$0.isEmpty }
        guard !lines.isEmpty else { return }
        let parts = lines.removeFirst().split(separator: " ", maxSplits: 2).map(String.init)
        guard parts.count == 3 else {
            try await respond("400 Bad Request")
            return
        }
        let (method, target, version) = (parts[0], parts[1], parts[2])
        let headers = lines

        if let creds = config.credentials {
            let expected = "Basic " + Data("\(creds.user):\(creds.password)".utf8).base64EncodedString()
            let given = headers
                .first { line in line.lowercased().hasPrefix("proxy-authorization:") }
                .map { line in String(line.drop { ch in ch != ":" }.dropFirst()).trimmingCharacters(in: .whitespaces) }
            guard given == expected else {
                try await respond("407 Proxy Authentication Required",
                                  extra: "Proxy-Authenticate: Basic realm=\"Phone Proxy\"\r\n")
                return
            }
        }

        if method.uppercased() == "CONNECT" {
            let (host, port) = splitHostPort(target, defaultPort: 443)
            let remote: Channel
            do {
                remote = try await connectRemote(NWEndpoint.Host(host), port)
            } catch {
                stats.log("✗ \(host):\(port)")
                try await respond("502 Bad Gateway")
                return
            }
            do {
                try await client.write("HTTP/1.1 200 Connection Established\r\n\r\n")
            } catch {
                remote.close()
                throw error
            }
            stats.log("HTTPS  \(host):\(port)")
            await Channel.relay(client, remote, stats: stats)
            return
        }

        // Plain HTTP: "GET http://host[:port]/path HTTP/1.1"
        guard target.lowercased().hasPrefix("http://") else {
            try await respond("400 Bad Request")
            return
        }
        let rest = target.dropFirst("http://".count)
        let hostPort = String(rest.prefix { $0 != "/" })
        let path = rest.dropFirst(hostPort.count)
        let (host, port) = splitHostPort(hostPort, defaultPort: 80)

        // One request per upstream connection, so keep-alive can't send a
        // follow-up request for a different host down the wrong pipe.
        let dropped: Set<String> = ["proxy-connection", "proxy-authorization", "connection", "keep-alive"]
        let kept = headers.filter { line in
            let name = line.prefix { $0 != ":" }.trimmingCharacters(in: .whitespaces).lowercased()
            return !dropped.contains(name)
        }
        var rewritten = "\(method) \(path.isEmpty ? "/" : String(path)) \(version)\r\n"
        for line in kept { rewritten += line + "\r\n" }
        rewritten += "Connection: close\r\n\r\n"

        let remote: Channel
        do {
            remote = try await connectRemote(NWEndpoint.Host(host), port)
        } catch {
            stats.log("✗ \(host):\(port)")
            try await respond("502 Bad Gateway")
            return
        }
        do {
            try await remote.write(rewritten.data(using: .isoLatin1) ?? Data(rewritten.utf8))
        } catch {
            remote.close()
            throw error
        }
        stats.log("HTTP   \(host):\(port) \(method)")
        await Channel.relay(client, remote, stats: stats)
    }

    private func respond(_ status: String, extra: String = "") async throws {
        try await client.write("HTTP/1.1 \(status)\r\n\(extra)Content-Length: 0\r\nConnection: close\r\n\r\n")
    }

    private func splitHostPort(_ value: String, defaultPort: UInt16) -> (String, UInt16) {
        if value.hasPrefix("[") {  // [ipv6]:port
            let inside = value.dropFirst().prefix { $0 != "]" }
            let after = value.dropFirst(inside.count + 2)
            let port = after.hasPrefix(":") ? UInt16(after.dropFirst()) : nil
            return (String(inside), port ?? defaultPort)
        }
        let pieces = value.split(separator: ":")
        if pieces.count == 2, let port = UInt16(pieces[1]) {
            return (String(pieces[0]), port)
        }
        return (value, defaultPort)
    }
}
