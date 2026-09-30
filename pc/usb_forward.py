#!/usr/bin/env python3
"""
usb_forward.py - reach the Phone Proxy app over the USB cable, no hotspot.

Listens on 127.0.0.1:8180 on the PC and tunnels each connection through
Apple's USB multiplexer (the "Apple Mobile Device Service" that iTunes /
Apple Devices installs) to the proxy port on the iPhone.

With Personal Hotspot OFF, the PC has no route to the internet except
through this tunnel, so nothing can leak out as hotspot data.

Usage:
    python pc/usb_forward.py --system-proxy  # forward :8180 and manage the Windows proxy
    python pc/usb_forward.py --port 1080     # the port set in the app
    python pc/usb_forward.py                 # forward only; point apps at 127.0.0.1:8180
"""

import argparse
import asyncio
import json
import os
import plistlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

USBMUX = ("127.0.0.1", 27015)
BUF = 64 * 1024

CONNECT_RESULTS = {
    1: "bad command",
    2: "iPhone not connected",
    3: "connection refused - is the proxy started in the app, and on this port?",
    6: "bad usbmux protocol version",
}


class UsbmuxError(Exception):
    pass


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# usbmux (Apple Mobile Device Service)
# --------------------------------------------------------------------------- #

async def usbmux_request(reader, writer, message, tag):
    """Send one plist message to usbmuxd and return its plist reply."""
    body = plistlib.dumps(dict(message, ClientVersionString="usb_forward",
                               ProgName="usb_forward"))
    writer.write(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)
    await writer.drain()
    header = await reader.readexactly(16)
    length = struct.unpack("<I", header[:4])[0]
    return plistlib.loads(await reader.readexactly(length - 16))


async def find_device():
    try:
        reader, writer = await asyncio.open_connection(*USBMUX)
    except OSError:
        raise UsbmuxError("Apple Mobile Device Service isn't reachable. "
                          "Install iTunes or Apple Devices and make sure the service is running.")
    try:
        reply = await usbmux_request(reader, writer, {"MessageType": "ListDevices"}, 1)
    finally:
        writer.close()
    for device in reply.get("DeviceList", []):
        if device["Properties"].get("ConnectionType") == "USB":
            return device["DeviceID"], device["Properties"].get("SerialNumber", "?")
    raise UsbmuxError("No iPhone found over USB. Plug it in, unlock it and tap Trust.")


async def open_phone_port(device_id, port):
    """Open a raw TCP stream to `port` on the iPhone, over USB."""
    reader, writer = await asyncio.open_connection(*USBMUX)
    reply = await usbmux_request(reader, writer, {
        "MessageType": "Connect",
        "DeviceID": device_id,
        "PortNumber": socket.htons(port),  # usbmuxd wants network byte order
    }, 2)
    result = reply.get("Number", -1)
    if result != 0:
        writer.close()
        raise UsbmuxError(CONNECT_RESULTS.get(result, "usbmux error %d" % result))
    return reader, writer  # from here on it's a plain byte stream


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(BUF)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class Forwarder:
    """Accepts local connections and tunnels each one to the phone over USB."""

    def __init__(self, phone_port):
        self.phone_port = phone_port
        self.device_id = None
        self.serial = None
        self.app_up = False

    async def status(self):
        """'no-device', 'no-app' or 'ready'. Cheap enough to poll every few
        seconds: it only test-connects to the app when something changed."""
        try:
            device_id, self.serial = await find_device()
        except (UsbmuxError, OSError):
            self.device_id, self.app_up = None, False
            return "no-device"
        if device_id != self.device_id or not self.app_up:
            self.device_id = device_id
            try:
                _, w = await open_phone_port(device_id, self.phone_port)
                w.close()
                self.app_up = True
            except (UsbmuxError, OSError):
                self.app_up = False
        return "ready" if self.app_up else "no-app"

    async def handle(self, c_reader, c_writer):
        try:
            if self.device_id is None:
                raise UsbmuxError("iPhone not connected")
            p_reader, p_writer = await open_phone_port(self.device_id, self.phone_port)
        except (UsbmuxError, OSError):
            self.app_up = False  # the watcher re-checks and reports what's wrong
            c_writer.close()
            return
        await asyncio.gather(pipe(c_reader, p_writer), pipe(p_reader, c_writer))


# --------------------------------------------------------------------------- #
# Windows system proxy
# --------------------------------------------------------------------------- #

class WindowsProxy:
    """Points the Windows system proxy at the forwarder, and puts the user's
    original settings back afterwards."""

    KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride")
    LOCAL = ["localhost", "127.*", "10.*", "192.168.*", "<local>"]

    def __init__(self, address, extra_bypass=()):
        import winreg
        self.winreg = winreg
        self.address = address
        self.bypass = ";".join(list(extra_bypass) + self.LOCAL)  # <local> must be last
        self.active = False
        self.lock = threading.Lock()
        self.saved = self._read()
        # A previous run that was killed may have left our setting behind;
        # don't "restore" that.
        if self.saved["ProxyServer"] and self.saved["ProxyServer"][0] == address:
            self.saved["ProxyEnable"] = (0, winreg.REG_DWORD)

    def _read(self):
        wr = self.winreg
        values = {}
        with wr.OpenKey(wr.HKEY_CURRENT_USER, self.KEY) as key:
            for name in self.NAMES:
                try:
                    values[name] = wr.QueryValueEx(key, name)
                except FileNotFoundError:
                    values[name] = None
        return values

    def _write(self, values):
        wr = self.winreg
        with wr.OpenKey(wr.HKEY_CURRENT_USER, self.KEY, 0, wr.KEY_SET_VALUE) as key:
            for name, value in values.items():
                if value is None:
                    try:
                        wr.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    wr.SetValueEx(key, name, 0, value[1], value[0])
        # Tell running apps the settings changed (SETTINGS_CHANGED, REFRESH).
        import ctypes
        ctypes.windll.wininet.InternetSetOptionW(None, 39, None, 0)
        ctypes.windll.wininet.InternetSetOptionW(None, 37, None, 0)

    def enable(self):
        wr = self.winreg
        with self.lock:
            if self.active:
                return
            self._write({
                "ProxyServer": (self.address, wr.REG_SZ),
                "ProxyOverride": (self.bypass, wr.REG_SZ),
                "ProxyEnable": (1, wr.REG_DWORD),
            })
            self.active = True

    def restore(self):
        with self.lock:
            if not self.active:
                return
            self._write(self.saved)
            self.active = False


class ProxiFyre:
    """Runs ProxiFyre.exe (per-app SOCKS redirector) as a console child
    process while the phone is ready. Its Windows service times out on start
    on some PCs, but the console mode works, and this way it can never be
    left running without the phone. Needs an elevated (admin) launcher."""

    name = "ProxiFyre"
    DEFAULT_EXE = r"C:\Program Files\ProxiFyre\ProxiFyre.exe"

    def __init__(self, exe, port):
        self.exe = exe or self.DEFAULT_EXE
        self.port = port
        self.proc = None
        self.log_path = os.path.join(tempfile.gettempdir(), "proxifyre-console.log")
        self.available = os.path.exists(self.exe)
        if self.available:
            self._ensure_config()

    def _ensure_config(self):
        """ProxiFyre won't start without app-config.json next to its exe.
        Install ours (steam.exe -> this forwarder) the first time; never
        overwrite one the user edited, just warn if it points elsewhere."""
        target = os.path.join(os.path.dirname(self.exe), "app-config.json")
        endpoint = "127.0.0.1:%d" % self.port
        if os.path.exists(target):
            with open(target, encoding="utf-8-sig") as f:
                if endpoint not in f.read():
                    log("warning: %s doesn't point at %s; apps it redirects won't reach the phone."
                        % (target, endpoint))
            return
        template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxifyre-app-config.json")
        with open(template, encoding="utf-8") as f:
            config = json.load(f)
        for rule in config["proxies"]:
            rule["socks5ProxyEndpoint"] = endpoint
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            log("Installed ProxiFyre config: steam.exe -> %s" % endpoint)
        except PermissionError:
            log("warning: no permission to write %s (run as administrator)." % target)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def last_output(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f if l.strip()]
            return lines[-1] if lines else "no output"
        except OSError:
            return "no output"

    def set(self, on):
        """Start or stop ProxiFyre. Returns (changed, error message or None)."""
        if not self.available or self.running() == on:
            return False, None
        if on:
            out = open(self.log_path, "w", encoding="utf-8")
            # Own process group: our Ctrl+C doesn't hit it, but CTRL_BREAK can
            # stop it cleanly. Shares our console, so closing the window ends it.
            self.proc = subprocess.Popen(
                [self.exe], cwd=os.path.dirname(self.exe), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            out.close()
            try:
                self.proc.wait(timeout=4)  # a startup failure exits quickly
                self.proc = None
                return False, "exited at startup: %s (log: %s)" % (self.last_output(), self.log_path)
            except subprocess.TimeoutExpired:
                return True, None
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None
        return True, None

    def died(self):
        """True once if ProxiFyre exited on its own while it should be running."""
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            return True
        return False


def on_console_close(callback):
    """Run `callback` if the console window is closed or Windows logs off /
    shuts down. (Ctrl+C arrives as KeyboardInterrupt instead.)"""
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
            callback()
        return False

    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler  # caller must keep a reference, or it gets garbage-collected


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

class Switches:
    """Everything that should be on only while the phone is ready: the
    Windows proxy setting and, optionally, ProxiFyre."""

    def __init__(self, proxy, redirector):
        self.proxy = proxy
        self.redirector = redirector
        self.lock = threading.Lock()

    def set(self, on):
        with self.lock:
            if self.proxy:
                if on and not self.proxy.active:
                    self.proxy.enable()
                    log("Windows proxy ON  -> %s" % self.proxy.address)
                elif not on and self.proxy.active:
                    self.proxy.restore()
                    log("Windows proxy OFF (restored your previous settings)")
            if self.redirector:
                changed, error = self.redirector.set(on)
                if error:
                    log("couldn't %s %s: %s" % ("start" if on else "stop", self.redirector.name, error))
                elif changed:
                    log("%s %s" % (self.redirector.name,
                                   "ON  (apps it redirects now use the phone)" if on else "OFF"))

    def off(self):
        self.set(False)


async def watch(fwd, switches, interval=3):
    last = None
    while True:
        state = await fwd.status()
        if state != last:
            if state == "ready":
                log("Ready: iPhone (%s) is proxying on port %d." % (fwd.serial, fwd.phone_port))
            elif state == "no-app":
                log("iPhone connected, but Phone Proxy isn't answering on port %d. "
                    "Start it in the app." % fwd.phone_port)
            else:
                log("Waiting for the iPhone on USB (plug it in, unlock it, tap Trust)...")
            # sc.exe can take a moment; keep the event loop (and traffic) moving.
            await asyncio.to_thread(switches.set, state == "ready")
            last = state
        elif switches.redirector and switches.redirector.died():
            log("%s stopped unexpectedly: %s. Restart the launcher to try again."
                % (switches.redirector.name, switches.redirector.last_output()))
        await asyncio.sleep(interval)


def quiet_resets(loop, context):
    """Windows' proactor loop logs a traceback whenever it tidies up a socket
    the peer already reset (a closed tab, Steam dropping a connection). That's
    normal for a proxy; don't print it. Anything else is still reported."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return
    loop.default_exception_handler(context)


async def run(args):
    asyncio.get_running_loop().set_exception_handler(quiet_resets)
    local_port = args.local_port or args.port
    fwd = Forwarder(args.port)
    try:
        server = await asyncio.start_server(fwd.handle, args.listen, local_port, limit=BUF)
    except OSError as e:
        sys.exit("Can't listen on %s:%d (%s). Is another copy already running?"
                 % (args.listen, local_port, e.strerror or e))

    proxy = WindowsProxy("127.0.0.1:%d" % local_port, args.bypass) if args.system_proxy else None
    redirector = None
    if args.proxifyre is not None:
        redirector = ProxiFyre(args.proxifyre or None, local_port)
        if redirector.available:
            log("ProxiFyre found; it will run only while the phone is ready.")
        else:
            log("ProxiFyre not found at %s; skipping it." % redirector.exe)
            redirector = None
    switches = Switches(proxy, redirector)
    close_handler = on_console_close(switches.off) if (proxy or redirector) else None

    log("Forwarding %s:%d -> iPhone:%d over USB. Press Ctrl+C to stop."
        % (args.listen, local_port, args.port))
    if not proxy:
        log("Point your apps at 127.0.0.1:%d (SOCKS5 or HTTP)." % local_port)
    try:
        await watch(fwd, switches)
    finally:
        # Switch off first, and don't wait for open connections to drain:
        # browsers keep idle keep-alive connections open indefinitely.
        switches.off()
        server.close()
        del close_handler


def main():
    ap = argparse.ArgumentParser(description="Use Phone Proxy over USB, without the hotspot")
    ap.add_argument("--port", type=int, default=8180,
                    help="proxy port set in the iPhone app (default 8180)")
    ap.add_argument("--local-port", type=int, help="port on this PC (default: same as --port)")
    ap.add_argument("--listen", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--system-proxy", action="store_true",
                    help="turn the Windows system proxy on while the phone is ready, "
                         "and restore the previous settings on exit")
    ap.add_argument("--bypass", action="append", default=[], metavar="HOST",
                    help="extra host that skips the proxy, e.g. *.example.com (repeatable). "
                         "Local addresses always skip it.")
    ap.add_argument("--proxifyre", nargs="?", const="", metavar="EXE",
                    help="run ProxiFyre (per-app redirector, e.g. for steam.exe) only while the "
                         "phone is ready; optional path to ProxiFyre.exe. Needs admin.")
    args = ap.parse_args()
    if (args.system_proxy or args.proxifyre is not None) and sys.platform != "win32":
        sys.exit("--system-proxy and --proxifyre only work on Windows")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
