#!/usr/bin/env python3
"""Test that a connection carries more than one request.

The browser fires a page's API calls in parallel.  When the proxy closes
the connection after every response, the browser has to open a fresh one
for each -- a fresh TLS handshake each time -- and the requests it had
queued on the connections it already had were dropped when the proxy
closed them out from under it.  That is what made the page's calls fail
with ERR_EMPTY_RESPONSE.
"""
import socket
import sys
import threading
import time

sys.path.insert(0, ".")
from web_adjuster import launcher  # noqa: E402

PORT = 38099
failures = []

server = launcher.start_wrapper(PORT, "wrapper")
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(1)


def read_response(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head, rest


def send(sock, path, extra=""):
    sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s"
                  "Connection: keep-alive\r\n\r\n"
                  % (path, PORT, extra)).encode())


print("[1] two requests on one connection")
sock = socket.socket()
sock.settimeout(60)
sock.connect(("127.0.0.1", PORT))
send(sock, "/health")
head1, body1 = read_response(sock)
print("    first  ->", head1.split(b"\r\n")[0].decode(), body1[:10])
send(sock, "/proxy/https%3A%2F%2Fexample.com%2F")
head2, body2 = read_response(sock)
print("    second ->", head2.split(b"\r\n")[0].decode(), len(body2), "bytes")
sock.close()

if not head1.startswith(b"HTTP/1.1 200"):
    failures.append("first request failed")
if not head2.startswith(b"HTTP/1.1 200"):
    failures.append("second request on the same connection failed")
if b"Connection: close" in head1:
    failures.append("the response asked the client to close the connection")
if b"Example Domain" not in body2:
    failures.append("the second response was not the proxied page")
print("    ok    first response :", "yes" if head1.startswith(b"HTTP/1.1 200") else "NO")
print("    ok    second response:", "yes" if head2.startswith(b"HTTP/1.1 200") else "NO")
print("    ok    no Connection: close:", "yes" if b"Connection: close" not in head1 else "NO")

print("[2] a client that asks to close is honoured")
sock = socket.socket()
sock.settimeout(60)
sock.connect(("127.0.0.1", PORT))
sock.sendall(("GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
              "Connection: close\r\n\r\n" % PORT).encode())
head, body = read_response(sock)
sock.close()
print("    Connection: close present:", b"Connection: close" in head)
if b"Connection: close" not in head:
    failures.append("Connection: close was not honoured")

print("[3] the request body is still read whole")
sock = socket.socket()
sock.settimeout(60)
sock.connect(("127.0.0.1", PORT))
payload = b"bigcookie=" + b"x" * 5000
sock.sendall(("POST /proxy-cookie?target=https%%3A%%2F%%2Fexample.com HTTP/1.1\r\n"
              "Host: 127.0.0.1:%d\r\nContent-Length: %d\r\n"
              "Connection: close\r\n\r\n" % (PORT, len(payload))).encode() + payload)
head, body = read_response(sock)
sock.close()
jar = launcher.cookie_jar_get("https://example.com")
print("    jar:", {k: len(v) for k, v in jar.items()})
if "x" * 5000 not in jar.get("bigcookie", ""):
    failures.append("the request body was not read whole")
print("    ok    body read whole:", "yes" if "x" * 5000 in jar.get("bigcookie", "") else "NO")

server.shutdown()
print()
if failures:
    print("FAILURES:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("ALL KEEP-ALIVE TESTS PASSED")
