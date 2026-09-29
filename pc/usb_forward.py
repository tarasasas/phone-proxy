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
import plistlib
import socket
import struct
import sys
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
    BYPASS = "localhost;127.*;10.*;192.168.*;<local>"

    def __init__(self, address):
        import winreg
        self.winreg = winreg
        self.address = address
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
                "ProxyOverride": (self.BYPASS, wr.REG_SZ),
                "ProxyEnable": (1, wr.REG_DWORD),
            })
            self.active = True

    def restore(self):
        with self.lock:
            if not self.active:
                return
            self._write(self.saved)
            self.active = False


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

async def watch(fwd, proxy, interval=3):
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
            if proxy:
                if state == "ready":
                    proxy.enable()
                    log("Windows proxy ON  -> %s" % proxy.address)
                elif proxy.active:
                    proxy.restore()
                    log("Windows proxy OFF (restored your previous settings)")
            last = state
        await asyncio.sleep(interval)


async def run(args):
    local_port = args.local_port or args.port
    fwd = Forwarder(args.port)
    try:
        server = await asyncio.start_server(fwd.handle, args.listen, local_port, limit=BUF)
    except OSError as e:
        sys.exit("Can't listen on %s:%d (%s). Is another copy already running?"
                 % (args.listen, local_port, e.strerror or e))

    proxy = None
    close_handler = None
    if args.system_proxy:
        proxy = WindowsProxy("127.0.0.1:%d" % local_port)
        close_handler = on_console_close(proxy.restore)

    log("Forwarding %s:%d -> iPhone:%d over USB. Press Ctrl+C to stop."
        % (args.listen, local_port, args.port))
    if not proxy:
        log("Point your apps at 127.0.0.1:%d (SOCKS5 or HTTP)." % local_port)
    try:
        await watch(fwd, proxy)
    finally:
        # Restore first, and don't wait for open connections to drain:
        # browsers keep idle keep-alive connections open indefinitely.
        if proxy and proxy.active:
            proxy.restore()
            log("Windows proxy OFF (restored your previous settings)")
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
    args = ap.parse_args()
    if args.system_proxy and sys.platform != "win32":
        sys.exit("--system-proxy only works on Windows")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
