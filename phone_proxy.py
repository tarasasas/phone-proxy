#!/usr/bin/env python3
"""
phone_proxy.py - a tiny SOCKS5 + HTTP proxy meant to run ON the iPhone.

Run it inside iSH, a-Shell, or Pythonista. Point your PC at the phone's IP
on the chosen port. The same port speaks both protocols:

  * SOCKS5  (browsers, curl --socks5-hostname, Proxifier, etc.)
  * HTTP    (Windows system proxy setting, most apps that honour it)

Only the Python standard library is used, and it works on Python 3.7+.

Usage:
    python3 phone_proxy.py                      # listen on 0.0.0.0:1080
    python3 phone_proxy.py --port 8888
    python3 phone_proxy.py --user me --password secret
"""

import argparse
import asyncio
import ipaddress
import socket
import struct
import sys
import time

BUF = 64 * 1024
CONNECT_TIMEOUT = 15

stats = {"active": 0, "total": 0, "up": 0, "down": 0}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #

async def pipe(reader, writer, counter):
    try:
        while True:
            data = await reader.read(BUF)
            if not data:
                break
            stats[counter] += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def relay(c_reader, c_writer, r_reader, r_writer):
    await asyncio.gather(
        pipe(c_reader, r_writer, "up"),
        pipe(r_reader, c_writer, "down"),
    )


async def open_remote(host, port):
    return await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
    )


# --------------------------------------------------------------------------- #
# SOCKS5 (RFC 1928 / RFC 1929)
# --------------------------------------------------------------------------- #

def socks_reply(code, bind_host="0.0.0.0", bind_port=0):
    try:
        ip = ipaddress.ip_address(bind_host)
    except ValueError:
        ip = ipaddress.ip_address("0.0.0.0")
    atyp = 0x01 if ip.version == 4 else 0x04
    return bytes([0x05, code, 0x00, atyp]) + ip.packed + struct.pack("!H", bind_port)


async def handle_socks5(reader, writer, creds):
    # Greeting: VER NMETHODS METHODS...  (VER byte already consumed)
    nmethods = (await reader.readexactly(1))[0]
    methods = await reader.readexactly(nmethods)

    if creds:
        if 0x02 not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(b"\x05\x02")
        await writer.drain()
        ver = (await reader.readexactly(1))[0]
        ulen = (await reader.readexactly(1))[0]
        user = (await reader.readexactly(ulen)).decode(errors="replace")
        plen = (await reader.readexactly(1))[0]
        pw = (await reader.readexactly(plen)).decode(errors="replace")
        if ver != 0x01 or (user, pw) != creds:
            writer.write(b"\x01\x01")
            return
        writer.write(b"\x01\x00")
    else:
        if 0x00 not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(b"\x05\x00")
    await writer.drain()

    # Request: VER CMD RSV ATYP DST.ADDR DST.PORT
    ver, cmd, _, atyp = await reader.readexactly(4)
    if atyp == 0x01:
        host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
    elif atyp == 0x03:
        n = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(n)).decode()
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        writer.write(socks_reply(0x08))
        return
    port = struct.unpack("!H", await reader.readexactly(2))[0]

    if cmd != 0x01:  # only CONNECT is supported
        writer.write(socks_reply(0x07))
        return

    try:
        r_reader, r_writer = await open_remote(host, port)
    except asyncio.TimeoutError:
        writer.write(socks_reply(0x04))
        return
    except socket.gaierror:
        writer.write(socks_reply(0x04))
        return
    except ConnectionRefusedError:
        writer.write(socks_reply(0x05))
        return
    except OSError:
        writer.write(socks_reply(0x01))
        return

    bind = r_writer.get_extra_info("sockname") or ("0.0.0.0", 0)
    writer.write(socks_reply(0x00, bind[0], bind[1]))
    await writer.drain()
    log("SOCKS  %s:%d" % (host, port))
    await relay(reader, writer, r_reader, r_writer)


# --------------------------------------------------------------------------- #
# HTTP proxy (CONNECT tunnels + plain http:// requests)
# --------------------------------------------------------------------------- #

def split_host_port(hostport, default_port):
    if hostport.startswith("["):  # [ipv6]:port
        host, _, rest = hostport[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port else default_port
    if hostport.count(":") == 1:
        host, port = hostport.split(":")
        return host, int(port)
    return hostport, default_port


async def handle_http(first_byte, reader, writer, creds):
    head = first_byte + await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    headers = [l for l in lines[1:] if l]

    if creds:
        import base64
        want = "Basic " + base64.b64encode(("%s:%s" % creds).encode()).decode()
        got = next((h.split(":", 1)[1].strip() for h in headers
                    if h.lower().startswith("proxy-authorization:")), None)
        if got != want:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                         b"Proxy-Authenticate: Basic realm=\"phone\"\r\n"
                         b"Content-Length: 0\r\n\r\n")
            return

    if method.upper() == "CONNECT":
        host, port = split_host_port(target, 443)
        try:
            r_reader, r_writer = await open_remote(host, port)
        except (OSError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        log("HTTPS  %s:%d" % (host, port))
        await relay(reader, writer, r_reader, r_writer)
        return

    # Plain HTTP: "GET http://host[:port]/path HTTP/1.1"
    if not target.lower().startswith("http://"):
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        return
    rest = target[7:]
    hostport, slash, path = rest.partition("/")
    host, port = split_host_port(hostport, 80)
    path = "/" + path if slash else "/"

    # Force one request per upstream connection so keep-alive can't send a
    # follow-up request for a different host down the wrong pipe.
    kept = [h for h in headers if h.split(":", 1)[0].strip().lower()
            not in ("proxy-connection", "proxy-authorization", "connection", "keep-alive")]
    new_head = "%s %s %s\r\n%s\r\nConnection: close\r\n\r\n" % (
        method, path, version, "\r\n".join(kept))

    try:
        r_reader, r_writer = await open_remote(host, port)
    except (OSError, asyncio.TimeoutError):
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        return
    r_writer.write(new_head.encode("latin-1"))
    await r_writer.drain()
    log("HTTP   %s:%d %s" % (host, port, method))
    await relay(reader, writer, r_reader, r_writer)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

def make_handler(creds):
    async def handle(reader, writer):
        stats["active"] += 1
        stats["total"] += 1
        try:
            first = await asyncio.wait_for(reader.readexactly(1), timeout=30)
            if first == b"\x05":
                await handle_socks5(reader, writer, creds)
            else:
                await handle_http(first, reader, writer, creds)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, ConnectionError, OSError, ValueError):
            pass
        except Exception as e:  # keep the server alive no matter what
            log("error: %r" % e)
        finally:
            stats["active"] -= 1
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
    return handle


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    for probe in ("8.8.8.8", "172.20.10.2"):  # internet route, hotspot subnet
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 9))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    ips.discard("127.0.0.1")
    ips.discard("0.0.0.0")
    return sorted(ips)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


async def report(interval):
    while True:
        await asyncio.sleep(interval)
        log("active=%d total=%d  up=%s down=%s" % (
            stats["active"], stats["total"], human(stats["up"]), human(stats["down"])))


async def main():
    ap = argparse.ArgumentParser(description="SOCKS5 + HTTP proxy for iPhone")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=1080)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--stats", type=int, default=60,
                    help="seconds between traffic reports (0 = off)")
    args = ap.parse_args()

    creds = (args.user, args.password or "") if args.user else None
    server = await asyncio.start_server(make_handler(creds), args.host, args.port,
                                        limit=BUF)

    log("Proxy listening on %s:%d (SOCKS5 + HTTP)%s" % (
        args.host, args.port, " with auth" if creds else ""))
    for ip in local_ips():
        hint = "  <- hotspot address" if ip.startswith("172.20.10.") else ""
        log("  point your PC at  %s:%d%s" % (ip, args.port, hint))

    if args.stats > 0:
        asyncio.ensure_future(report(args.stats))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
