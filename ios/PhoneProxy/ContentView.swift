import SwiftUI
import UIKit

struct ContentView: View {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    @AppStorage("port") private var port = 1080
    @AppStorage("cellularOnly") private var cellularOnly = false
    @AppStorage("keepAliveAudio") private var keepAliveAudio = true
    @AppStorage("keepAliveLocation") private var keepAliveLocation = false
    @AppStorage("requireLogin") private var requireLogin = false
    @AppStorage("username") private var username = ""
    @AppStorage("password") private var password = ""

    @State private var copied: String?

    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        NavigationStack {
            Form {
                statusSection
                if model.isOn { addressSection }
                trafficSection
                settingsSection
                if !model.stats.recent.isEmpty { recentSection }
            }
            .navigationTitle("Phone Proxy")
        }
        .onReceive(ticker) { _ in model.refresh() }
        .onChange(of: scenePhase) { phase in
            if phase == .active {
                model.recoverIfNeeded()
                model.refresh()
            }
        }
    }

    // MARK: Sections

    private var statusSection: some View {
        Section {
            HStack(spacing: 10) {
                Circle().fill(statusColor).frame(width: 10, height: 10)
                Text(statusText)
                Spacer()
            }
            Button {
                if model.isOn { model.stop() } else { start() }
            } label: {
                Text(model.isOn ? "Stop proxy" : "Start proxy")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(model.isOn ? .red : .accentColor)
            .disabled(!model.isOn && !settingsValid)
        }
    }

    private var addressSection: some View {
        Section {
            if model.addresses.isEmpty {
                Text("No network yet. Turn on Personal Hotspot and connect your PC to it.")
                    .foregroundStyle(.secondary)
            }
            ForEach(model.addresses) { address in
                let value = "\(address.ip):\(port)"
                Button {
                    UIPasteboard.general.string = value
                    copied = value
                } label: {
                    LabeledContent(address.label) {
                        Text(copied == value ? "Copied" : value).font(.body.monospaced())
                    }
                }
                .foregroundStyle(.primary)
            }
        } header: {
            Text("Point your PC at")
        } footer: {
            Text("Works as a SOCKS5 proxy and as an HTTP proxy on the same port. Tap to copy.")
        }
    }

    private var trafficSection: some View {
        Section("Traffic") {
            LabeledContent("Open connections", value: "\(model.stats.active)")
            LabeledContent("Total connections", value: "\(model.stats.total)")
            LabeledContent("Uploaded", value: bytes(model.stats.up))
            LabeledContent("Downloaded", value: bytes(model.stats.down))
        }
    }

    private var settingsSection: some View {
        Section {
            LabeledContent("Port") {
                TextField("1080", value: $port, format: .number.grouping(.never))
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
            }
            Toggle("Only use cellular", isOn: $cellularOnly)
            Toggle("Keep alive: silent audio", isOn: $keepAliveAudio)
            Toggle("Keep alive: location", isOn: $keepAliveLocation)
            Toggle("Require login", isOn: $requireLogin)
            if requireLogin {
                TextField("Username", text: $username)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                SecureField("Password", text: $password)
            }
        } header: {
            Text("Settings")
        } footer: {
            Text("Stop the proxy to change settings. \"Only use cellular\" lets the PC share the phone's mobile data even when both are on the same Wi-Fi. Keep-alive stops iOS from pausing the proxy when you leave the app; location is the most reliable.")
        }
        .disabled(model.isOn)
    }

    private var recentSection: some View {
        Section("Recent") {
            ForEach(Array(model.stats.recent.prefix(30).enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.caption.monospaced())
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }

    // MARK: Helpers

    private var settingsValid: Bool {
        (1...65535).contains(port) && (!requireLogin || !username.isEmpty)
    }

    private func start() {
        copied = nil
        let creds = requireLogin ? ProxyConfig.Credentials(user: username, password: password) : nil
        let config = ProxyConfig(port: UInt16(port), cellularOnly: cellularOnly, credentials: creds)
        model.start(config, audio: keepAliveAudio, location: keepAliveLocation)
    }

    private var statusText: String {
        switch model.status {
        case .stopped: return "Stopped"
        case .starting: return "Starting…"
        case .running: return "Running on port \(port)"
        case .failed(let message): return "Error: \(message)"
        }
    }

    private var statusColor: Color {
        switch model.status {
        case .stopped: return .gray
        case .starting: return .orange
        case .running: return .green
        case .failed: return .red
        }
    }

    private func bytes(_ n: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: n, countStyle: .binary)
    }
}
