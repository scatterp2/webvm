#!/usr/bin/env python3
"""Probe WebVM ports and exercise the proxy from the host machine.

Usage: python probe.py <host> [ports...]
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.71.2.31"
PORTS = [int(p) for p in sys.argv[2:]] or [2323, 28080, 28081, 28082]


def probe(port, host=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect((host or HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_get(port, path, host_header, read_timeout=8):
    """Send a raw HTTP/1.0 GET and return the raw response bytes."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(6)
    try:
        s.connect((HOST, port))
    except OSError as exc:
        return None, "connect failed: {}".format(exc)
    try:
        s.sendall(
            "GET {} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n".format(
                path, host_header
            ).encode()
        )
        data = b""
        while True:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if len(data) > 200000:
                break
        return data, None
    finally:
        s.close()


def main():
    print("=== TCP port probe: {} ===".format(HOST))
    for port in PORTS:
        print("  port {:>6}: {}".format(port, "OPEN" if probe(port) else "closed/timeout"))

    print()
    print("=== HTTP probes ===")
    for port in [p for p in PORTS if p >= 28080]:
        for path in ("/health", "/proxy-status"):
            data, err = http_get(port, path, "{}:{}".format(HOST, port))
            if err:
                print("  {}:{} {} -> {}".format(HOST, port, path, err))
                continue
            head = data.split(b"\r\n\r\n", 1)
            status = head[0].split(b"\r\n")[0].decode("utf-8", "replace")
            body = head[1][:200].decode("utf-8", "replace") if len(head) > 1 else ""
            print("  {}:{} {} -> {} | {}".format(HOST, port, path, status, body.strip()))

    print()
    print("=== Proxy fetch (real site through proxy) ===")
    for port in [p for p in PORTS if p >= 28080]:
        data, err = http_get(
            port, "/", "{}:{}".format(HOST, port), read_timeout=15
        )
        if err:
            print("  :{} / -> {}".format(port, err))
            continue
        print("  :{} / -> {} bytes, first line: {}".format(
            port, len(data), data.split(b"\r\n")[0].decode("utf-8", "replace")))


if __name__ == "__main__":
    main()
