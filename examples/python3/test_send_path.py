#!/usr/bin/env python3
"""Tests for the response write path.

Three properties matter for the WebVM, and each of them used to be broken:

  1. A client that stops reading must not park the writer.  Under CheerpX a
     blocking send never returns, and enough parked threads stop the VM
     from scheduling anything -- the wedge.  The write loop is
     non-blocking and has an absolute deadline, so it always returns.

  2. A large body must arrive byte-for-byte.  /proxy-sendtest exists to
     measure that end to end; this checks the response against the
     X-Body-Sha1 header.

  3. A compressed body that is not markup is forwarded as-is, so the bytes
     on the wire are the bytes the upstream sent (99 KB of JSON is ~13 KB
     gzipped, and the transfer path is the bottleneck).
"""
import socket
import sys
import threading
import time

sys.path.insert(0, ".")
from web_adjuster import launcher  # noqa: E402

PORT = 38101
failures = []

server = launcher.start_wrapper(PORT, "wrapper")
time.sleep(0.5)


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


def header(head, name):
    for line in head.split(b"\r\n"):
        if line.lower().startswith(name.lower().encode() + b":"):
            return line.split(b":", 1)[1].strip().decode()
    return None


# ---------------------------------------------------------------- [1]
print("[1] a client that stops reading does not park the writer")
import hashlib  # noqa: E402

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
peer_addr = listener.getsockname()

writer_side = socket.socket()
writer_side.connect(peer_addr)
reader_side, _ = listener.accept()
# Never read from reader_side, and keep the receive window tiny.
reader_side.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)

old_timeout = launcher.CLIENT_WRITE_TIMEOUT
old_chunk = launcher.SEND_CHUNK
old_delay = launcher.SEND_DELAY
launcher.CLIENT_WRITE_TIMEOUT = 2
launcher.SEND_CHUNK = 8192
launcher.SEND_DELAY = 0.01
body = b"A" * (8 * 1024 * 1024)
started = time.time()
ok = launcher.send_response(writer_side, 200, [("Content-Type",
                                               "application/octet-stream")],
                            body, close=False, client="test")
elapsed = time.time() - started
launcher.CLIENT_WRITE_TIMEOUT = old_timeout
launcher.SEND_CHUNK = old_chunk
launcher.SEND_DELAY = old_delay
print("    returned {} after {:.1f}s (limit was 2s)".format(ok, elapsed))
if ok:
    failures.append("a write into a full socket reported success")
if elapsed > 8:
    failures.append("the write did not honour its deadline")
writer_side.close()
reader_side.close()
listener.close()

# ---------------------------------------------------------------- [2]
print("[2] /proxy-sendtest delivers a 99 KB body intact")
sock = socket.socket()
sock.settimeout(90)
sock.connect(("127.0.0.1", PORT))
sock.sendall(("GET /proxy-sendtest?bytes=101248 HTTP/1.1\r\n"
              "Host: 127.0.0.1:%d\r\nConnection: close\r\n\r\n"
              % PORT).encode())
head, body = read_response(sock)
sock.close()
expected = header(head, "X-Body-Sha1")
actual = hashlib.sha1(body).hexdigest()[:16]
print("    status {} bytes {} sha {} (expected {})".format(
    head.split(b"\r\n")[0].decode(), len(body), actual, expected))
if len(body) != 101248:
    failures.append("sendtest body was truncated: {} bytes".format(len(body)))
if expected != actual:
    failures.append("sendtest body checksum mismatch")

# ---------------------------------------------------------------- [3]
print("[3] a gzipped body that is not markup is passed through")
import gzip  # noqa: E402

payload = b'{"items":[' + b'{"id":1,"name":"x"},' * 2000 + b'{"id":0}]}'
packed = gzip.compress(payload, 6)
status, headers, body = launcher.transform_response(
    200,
    [("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
    packed,
    "https://www.kaggle.com/api/i/users.HomePageService/GetLoggedOutHomepageData",
    None,
)
print("    upstream {} -> forwarded {} (headers {})".format(
    len(packed), len(body), [n for n, _ in headers]))
if body != packed:
    failures.append("compressed JSON was re-encoded on the way through")
if not any(n.lower() == "content-encoding" for n, _ in headers):
    failures.append("content-encoding was dropped from a compressed body")

# ---------------------------------------------------------------- [4]
print("[4] a gzipped HTML document is rewritten, then re-compressed")
html = b"<html><head></head><body><p>hello</p></body></html>"
status, headers, body = launcher.transform_response(
    200,
    [("Content-Type", "text/html"), ("Content-Encoding", "gzip")],
    gzip.compress(html * 40),
    "https://example.com/",
    None,
)
plain = gzip.decompress(body) if any(
    n.lower() == "content-encoding" for n, _ in headers) else body
print("    rewritten markers: injected={} path_fix={}".format(
    b"WEBVM_ADJUSTER_INJECTED" in plain,
    b"history.replaceState" in plain))
if b"WEBVM_ADJUSTER_INJECTED" not in plain:
    failures.append("gzipped HTML was not injected")

print()
if failures:
    print("FAILURES:")
    for failure in failures:
        print("  -", failure)
    sys.exit(1)
print("ALL SEND PATH TESTS PASSED")
