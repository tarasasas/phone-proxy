#!/usr/bin/env python3
"""
usb_forward.py - reach the Phone Proxy app over the USB cable, no hotspot.

Listens on 127.0.0.1:1080 on the PC and tunnels each connection through
Apple's USB multiplexer (the "Apple Mobile Device Service" that iTunes /
Apple Devices installs) to the proxy port on the iPhone.

With Personal Hotspot OFF, the PC has no route to the internet except
through this tunnel, so nothing can leak out as hotspot data.

Usage:
    python pc/usb_forward.py                 # PC 127.0.0.1:1080 -> phone :1080
    python pc/usb_forward.py --port 8888     # the port set in the app
    python pc/usb_forward.py --local-port 1081
"""

import argparse
import asyncio
import plistlib
import socket
import struct
import sys
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
    def __init__(self, phone_port):
        self.phone_port = phone_port
        self.device_id = None
        self.last_error = None

    async def connect(self):
        if self.device_id is None:
            self.device_id, _ = await find_device()
        try:
            return await open_phone_port(self.device_id, self.phone_port)
        except UsbmuxError:
            # The phone may have been re-plugged and got a new device ID.
            self.device_id, _ = await find_device()
            return await open_phone_port(self.device_id, self.phone_port)

    async def handle(self, c_reader, c_writer):
        try:
            p_reader, p_writer = await self.connect()
        except (UsbmuxError, OSError) as e:
            if str(e) != self.last_error:  # don't spam the same error per connection
                log("can't reach phone: %s" % e)
                self.last_error = str(e)
            c_writer.close()
            return
        if self.last_error:
            log("phone reachable again")
            self.last_error = None
        await asyncio.gather(pipe(c_reader, p_writer), pipe(p_reader, c_writer))


async def main():
    ap = argparse.ArgumentParser(description="Forward a PC port to Phone Proxy over USB")
    ap.add_argument("--port", type=int, default=1080, help="proxy port set in the iPhone app")
    ap.add_argument("--local-port", type=int, help="port on this PC (default: same as --port)")
    ap.add_argument("--listen", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    args = ap.parse_args()
    local_port = args.local_port or args.port

    try:
        device_id, serial = await find_device()
    except UsbmuxError as e:
        sys.exit(str(e))
    log("iPhone found over USB (%s)" % serial)

    try:
        _, w = await open_phone_port(device_id, args.port)
        w.close()
        log("Phone Proxy is answering on port %d" % args.port)
    except UsbmuxError as e:
        log("warning: %s" % e)
        log("starting anyway; connections will work once the app's proxy is running")

    fwd = Forwarder(args.port)
    fwd.device_id = device_id
    server = await asyncio.start_server(fwd.handle, args.listen, local_port, limit=BUF)
    log("Forwarding %s:%d -> iPhone:%d over USB. Ctrl+C to stop."
        % (args.listen, local_port, args.port))
    log("Point the PC at %s:%d  (e.g. .\\pc\\set-proxy.ps1 -Address 127.0.0.1 -Port %d)"
        % ("127.0.0.1" if args.listen in ("0.0.0.0", "127.0.0.1") else args.listen,
           local_port, local_port))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
