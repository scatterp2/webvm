#!/usr/bin/env python3
"""Sweep the socket write settings and find one that transfers a large
response intact.

CheerpX's socket accepts bytes into a fixed buffer and drops the excess
rather than applying backpressure, so a response larger than that buffer
is truncated in transit.  Writing in smaller pieces with a pause in
between gives the buffer time to drain towards the client.
"""
import socket
import ssl
import sys
import time
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
TLS_PORT = 28443
PLAIN_PORT = 28082
CTX = ssl.create_default_context()

TARGET = ("https://raw.githubusercontent.com/scatterp2/webvm/main/"
          "examples/python3/web_adjuster/launcher.py")
PATH = "/proxy/" + quote(TARGET, safe="")


def tune(chunk, delay):
    s = socket.socket()
    s.settimeout(60)
    s.connect((HOST, PLAIN_PORT))
    s.sendall(("GET /proxy-tune?chunk=%d&delay=%s HTTP/1.0\r\nHost: h\r\n"
               "Connection: close\r\n\r\n" % (chunk, delay)).encode())
    data = b""
    while True:
        chunk_in = s.recv(4096)
        if not chunk_in:
            break
        data += chunk_in
    s.close()
    return data.split(b"\r\n\r\n", 1)[-1].decode("utf-8", "replace").strip()


def fetch():
    s = socket.socket()
    s.settimeout(180)
    s.connect((HOST, TLS_PORT))
    s = CTX.wrap_socket(s, server_hostname=HOST)
    s.sendall(("GET %s HTTP/1.0\r\nHost: %s:%d\r\nUser-Agent: Mozilla/5.0\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n"
               % (PATH, HOST, TLS_PORT)).encode())
    data = b""
    err = ""
    while True:
        try:
            chunk_in = s.recv(65536)
        except Exception as exc:
            err = type(exc).__name__
            break
        if not chunk_in:
            break
        data += chunk_in
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    length = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    return len(body), length, err


combos = []
for chunk in (4096, 8192, 16384):
    for delay in (0.0, 0.002, 0.01, 0.05):
        combos.append((chunk, delay))

if len(sys.argv) > 2:
    combos = [(int(sys.argv[1]), float(sys.argv[2]))]

print("%-8s %-8s %-10s %-10s %s" % ("chunk", "delay", "received",
                                    "expected", "verdict"))
for chunk, delay in combos:
    reply = tune(chunk, delay)
    received, expected, err = fetch()
    if expected is None:
        verdict = "no content-length (%s)" % err
    elif received == expected:
        verdict = "INTACT"
    else:
        verdict = "truncated by %d %s" % (expected - received, err)
    print("%-8d %-8s %-10d %-10s %s" % (chunk, delay, received, expected, verdict))
    if expected is not None and received == expected:
        print("\n=> chunk=%d delay=%s works" % (chunk, delay))
        break
