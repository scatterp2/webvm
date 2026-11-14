#!/usr/bin/env python3
"""Is the truncation in the TLS layer or in the socket?

The same response over plain HTTP and over TLS says which one drops the
bytes.  A small response is included as a control.
"""
import socket
import ssl
import time
from urllib.parse import quote

PLAIN_PORT = 28082
TLS_PORT = 28443
HOST = "webvm-proxy.tailf987e.ts.net"
CTX = ssl.create_default_context()

LARGE = ("https://raw.githubusercontent.com/scatterp2/webvm/main/"
         "examples/python3/web_adjuster/launcher.py")
SMALL = "https://example.com/"


def fetch(port, path, use_tls, timeout=120):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((HOST, port))
    if use_tls:
        s = CTX.wrap_socket(s, server_hostname=HOST)
    s.sendall(("GET %s HTTP/1.0\r\nHost: %s:%d\r\n"
               "User-Agent: Mozilla/5.0\r\nAccept: */*\r\n"
               "Connection: close\r\n\r\n" % (path, HOST, port)).encode())
    data = b""
    err = ""
    while True:
        try:
            chunk = s.recv(65536)
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            break
        if not chunk:
            break
        data += chunk
    s.close()
    return data, err


def report(label, data, err):
    head, _, body = data.partition(b"\r\n\r\n")
    length = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    status = head.split(b"\r\n")[0].decode("utf-8", "replace")
    verdict = "?"
    if length is not None:
        verdict = ("intact" if len(body) == length
                   else "TRUNCATED (short by %d)" % (length - len(body)))
    print("%-34s %-14s body=%-7d content-length=%-7s %s %s"
          % (label, status, len(body), length, verdict, err))


print("=== plain HTTP (port 28082) ===")
for label, target in (("small (example.com)", SMALL), ("large (launcher.py)", LARGE)):
    data, err = fetch(PLAIN_PORT, "/proxy/" + quote(target, safe=""), False)
    report(label, data, err)

print()
print("=== TLS (port 28443) ===")
for label, target in (("small (example.com)", SMALL), ("large (launcher.py)", LARGE)):
    data, err = fetch(TLS_PORT, "/proxy/" + quote(target, safe=""), True)
    report(label, data, err)
