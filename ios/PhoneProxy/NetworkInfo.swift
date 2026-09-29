import Foundation

struct InterfaceAddress: Identifiable, Equatable {
    var id: String { name + ip }
    let name: String
    let ip: String
    let label: String
}

/// IPv4 addresses a PC could use to reach this phone.
func reachableAddresses() -> [InterfaceAddress] {
    var result: [InterfaceAddress] = []
    var list: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&list) == 0, let first = list else { return [] }
    defer { freeifaddrs(list) }

    for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
        let ifa = ptr.pointee
        guard let addr = ifa.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) else { continue }

        let name = String(cString: ifa.ifa_name)
        let label: String
        if name.hasPrefix("bridge") {
            label = "Hotspot (Wi-Fi / USB)"
        } else if name == "en0" {
            label = "Same Wi-Fi network"
        } else {
            continue  // loopback, cellular, VPN tunnels: not reachable from the PC
        }

        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        guard getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count),
                          nil, 0, NI_NUMERICHOST) == 0 else { continue }
        let ip = String(cString: host)
        if ip.hasPrefix("169.254.") { continue }  // link-local, no DHCP
        result.append(InterfaceAddress(name: name, ip: ip, label: label))
    }
    // Hotspot first: it's the usual setup.
    return result.sorted { a, b in a.name.hasPrefix("bridge") && !b.name.hasPrefix("bridge") }
}
