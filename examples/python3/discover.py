#!/usr/bin/env python3
"""Discover the live WebVM node on the tailnet and probe its services.

Usage: python discover.py
"""
import re
import socket
import subprocess
import sys
import time

TAILSCALE = r"C:\Program Files\Tailscale\Tailscale.exe"
PORTS = (2323, 28080, 28081, 28082)


def tailnet_nodes():
    try:
        out = subprocess.run(
            [TAILSCALE, "status"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception as exc:
        print("tailscale status failed: {}".format(exc))
        return []
    nodes = []
    for line in out.splitlines():
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        ip, name, rest = m.group(1), m.group(2), m.group(3)
        offline = "offline" in rest
        nodes.append((ip, name, offline, rest.strip()))
    return nodes


def open_port(ip, port, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_get(ip, port, path, host_header, timeout=8.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
    except OSError as exc:
        return None, "connect: {}".format(exc)
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
            if len(data) > 300000:
                break
        return data, None
    finally:
        s.close()


def main():
    nodes = tailnet_nodes()
    print("=== tailnet nodes ===")
    for ip, name, offline, rest in nodes:
        if offline:
            continue
        print("  {}  {}  {}".format(ip, name, rest[:70]))

    print()
    print("=== probing online nodes for WebVM services ===")
    live = []
    for ip, name, offline, rest in nodes:
        if offline:
            continue
        hits = []
        for port in PORTS:
            if open_port(ip, port):
                hits.append(port)
        if hits:
            print("  {} ({}) -> open ports {}".format(ip, name, hits))
            live.append((ip, name, hits))

    if not live:
        print("  no WebVM services found")
        return 1

    print()
    for ip, name, hits in live:
        for port in hits:
            if port < 28080:
                continue
            for path in ("/health", "/proxy-status"):
                data, err = http_get(ip, port, path, "{}:{}".format(ip, port))
                if err:
                    print("  {}:{} {} -> {}".format(ip, port, path, err))
                    continue
                parts = data.split(b"\r\n\r\n", 1)
                status = parts[0].split(b"\r\n")[0].decode("utf-8", "replace")
                body = parts[1][:300].decode("utf-8", "replace") if len(parts) > 1 else ""
                print("  {}:{} {} -> {} | {}".format(ip, port, path, status, body.strip()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
