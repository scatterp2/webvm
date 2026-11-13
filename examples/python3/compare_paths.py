#!/usr/bin/env python3
"""Compare every path to the proxy, from the host.

  A  http://<ip>:28082/?target=...        (the VM's own proxy page)
  B  http://<ip>:28082/proxy/<url>        (plain http proxy)
  C  https://<name>:28443/proxy/<url>     (the https proxy, by name)
  D  https://<name>:28443/               (the https proxy root)
"""
import socket
import ssl
import sys
import urllib.request
from urllib.parse import quote

IP = sys.argv[1] if len(sys.argv) > 1 else "100.90.80.84"
NAME = "webvm-proxy.tailf987e.ts.net"
TARGET = "https://example.com/"
CTX = ssl.create_default_context()


def raw(url, context=None, label=""):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=context) as r:
            body = r.read()
            text = body.decode("utf-8", "replace")
            print("%-58s -> %s  %6d bytes  title=%s" % (
                label or url, r.status, len(body),
                (text.split("<title>")[1].split("</title>")[0][:34]
                 if "<title>" in text else "n/a")))
            return body
    except Exception as exc:
        print("%-58s -> FAILED %s: %s" % (label or url, type(exc).__name__, exc))
        return None


def raw_tcp(host, port, path, label):
    """Plain TCP, no TLS, no HTTP client: what a browser would open."""
    s = socket.socket()
    s.settimeout(25)
    try:
        s.connect((host, port))
        s.sendall(("GET %s HTTP/1.0\r\nHost: %s:%d\r\n"
                    "User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n"
                    % (path, host, port)).encode())
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        head = data.split(b"\r\n\r\n", 1)
        print("%-58s -> %s  %6d bytes" % (
            label, head[0].split(b"\r\n")[0].decode(),
            len(head[1]) if len(head) > 1 else 0))
    except Exception as exc:
        print("%-58s -> FAILED %s: %s" % (label, type(exc).__name__, exc))
    finally:
        s.close()


print("=== the VM's own proxy page (http) ===")
raw("http://%s:28082/?target=%s" % (IP, quote(TARGET, safe="")),
    label="A http proxy page")

print("\n=== plain http proxy ===")
raw("http://%s:28082/proxy/%s" % (IP, quote(TARGET, safe="")),
    label="B http /proxy/")

print("\n=== https proxy by name ===")
raw("https://%s:28443/proxy/%s" % (NAME, quote(TARGET, safe="")), CTX,
    label="C https /proxy/ (by name)")
raw("https://%s:28443/" % NAME, CTX, label="D https root (by name)")

print("\n=== raw TCP, no client-side TLS stack ===")
raw_tcp(IP, 28082, "/proxy/" + quote(TARGET, safe=""),
        "E raw TCP http :28082")
