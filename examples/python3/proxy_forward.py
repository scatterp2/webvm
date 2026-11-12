#!/usr/bin/env python3
"""Host-side TCP forwarder: 127.0.0.1:<port> -> the live WebVM's proxy port.

Why this exists
---------------
The WebVM page is served over HTTPS (https://scatterp2.github.io/webvm/).
Chrome blocks an ``http://`` iframe on an HTTPS page as mixed content, so
the proxy cannot be embedded directly from the WebVM's Tailscale address.

``http://127.0.0.1`` (and ``http://localhost``) is a *potentially
trustworthy* origin, so Chrome allows it in an HTTPS page.  This forwarder
publishes the WebVM proxy on 127.0.0.1 so the WebVM page can embed it.

The WebVM's Tailscale address changes on every boot, so the forwarder
discovers the live node automatically (by reading ``tailscale status`` and
probing the proxy port) and re-resolves it whenever the cached target goes
away.

Usage:
    python proxy_forward.py [--listen 127.0.0.1:28443] [--target-port 28082]
                             [--target-ip 100.x.x.x]
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time

TAILSCALE = r"C:\Program Files\Tailscale\Tailscale.exe"

_state_lock = threading.Lock()
_target = {"ip": None, "checked": 0.0}
TARGET_TTL = 20.0


def log(message: str) -> None:
    print("[proxy_forward] " + message, flush=True)


def tailnet_candidates():
    """Online tailnet nodes that look like WebVM nodes."""
    try:
        out = subprocess.run(
            [TAILSCALE, "status", "--json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        data = json.loads(out)
    except Exception as exc:
        log("tailscale status failed: {}".format(exc))
        return []
    nodes = []
    for peer in (data.get("Peer") or {}).values():
        if not peer.get("Online"):
            continue
        ips = peer.get("TailscaleIPs") or []
        if not ips:
            continue
        nodes.append((ips[0], peer.get("HostName") or "?"))
    return nodes


def port_open(ip, port, timeout=1.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def resolve_target(target_port, pinned_ip=None):
    """Find the live WebVM that serves the proxy on target_port."""
    if pinned_ip:
        return pinned_ip
    now = time.time()
    with _state_lock:
        if _target["ip"] and now - _target["checked"] < TARGET_TTL:
            return _target["ip"]
    for ip, name in tailnet_candidates():
        if port_open(ip, target_port):
            with _state_lock:
                _target["ip"] = ip
                _target["checked"] = time.time()
            log("target resolved ip={} name={} port={}".format(ip, name, target_port))
            return ip
    with _state_lock:
        _target["ip"] = None
        _target["checked"] = time.time()
    return None


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client, listen_port, target_port, pinned_ip):
    upstream = None
    try:
        ip = resolve_target(target_port, pinned_ip)
        if not ip:
            log("no live WebVM found on port {}".format(target_port))
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 29\r\n"
                           b"Connection: close\r\n\r\nno live WebVM proxy found\r\n")
            return
        upstream = socket.create_connection((ip, target_port), timeout=15)
        upstream.settimeout(None)
        client.settimeout(None)
        threads = [
            threading.Thread(target=pump, args=(client, upstream), daemon=True),
            threading.Thread(target=pump, args=(upstream, client), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    except Exception as exc:
        log("connection failed: {}: {}".format(type(exc).__name__, exc))
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:28443")
    parser.add_argument("--target-port", type=int, default=28082)
    parser.add_argument("--target-ip", default=None)
    args = parser.parse_args()

    host, _, port = args.listen.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(32)
    log("forwarding http://{}:{} -> live WebVM :{}".format(
        host, port, args.target_port))
    log("the WebVM page can embed http://{}:{}/proxy/<url>".format(host, port))

    while True:
        try:
            client, addr = server.accept()
        except OSError:
            continue
        threading.Thread(
            target=handle,
            args=(client, port, args.target_port, args.target_ip),
            daemon=True,
        ).start()


if __name__ == "__main__":
    sys.exit(main())
