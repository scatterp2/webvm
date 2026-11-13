#!/usr/bin/env python3
"""Probe the WebVM's proxy while the browser loads a page through it.

The symptom: after the browser loads the panel, the WebVM stops answering
*everything* -- even a plain request from the host, which never touches the
browser's Tailscale client.  This script polls /health and prints a line per
attempt so the wedge can be seen happening and then verified as fixed.

Usage:
    python wedge_probe.py <ip> [port] [seconds]
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else ""
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28082
SECONDS = int(sys.argv[3]) if len(sys.argv) > 3 else 60

if not HOST:
    print("usage: wedge_probe.py <ip> [port] [seconds]")
    raise SystemExit(1)


def probe():
    s = socket.socket()
    s.settimeout(4)
    started = time.time()
    try:
        s.connect((HOST, PORT))
        s.sendall(("GET /health HTTP/1.0\r\nHost: %s:%d\r\n"
                    "Connection: close\r\n\r\n" % (HOST, PORT)).encode())
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                return "TIMEOUT", time.time() - started
            if not chunk:
                break
            data += chunk
        status = data.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        return status, time.time() - started
    except OSError as exc:
        return "ERROR %s" % exc, time.time() - started
    finally:
        s.close()


print("probing %s:%d every 2s for %ds" % (HOST, PORT, SECONDS), flush=True)
deadline = time.time() + SECONDS
while time.time() < deadline:
    status, elapsed = probe()
    print("%6.1fs  %-40s %.2fs" % (time.time() - (deadline - SECONDS),
                                     status, elapsed), flush=True)
    time.sleep(2)
print("done", flush=True)
