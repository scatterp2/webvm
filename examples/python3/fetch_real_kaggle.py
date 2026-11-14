#!/usr/bin/env python3
"""Retry the proxied Kaggle document until we get the real page, then dump it.

The proxy's egress can be served Google's reCAPTCHA challenge page instead
of the app.  The challenge is transient, so retry until the real page shows
up, then report how the page delivers the XSRF token.
"""
import re
import socket
import ssl
import sys
import time
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()
ATTEMPTS = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def fetch(target):
    path = "/proxy/" + quote(target, safe="")
    s = socket.socket()
    s.settimeout(90)
    s.connect((HOST, PORT))
    s = CTX.wrap_socket(s, server_hostname=HOST)
    s.sendall(("GET %s HTTP/1.0\r\nHost: %s:%d\r\n"
                "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36\r\n"
                "Accept: text/html,application/xhtml+xml,*/*\r\n"
                "Accept-Language: en-US,en;q=0.9\r\n"
                "Connection: close\r\n\r\n" % (path, HOST, PORT)).encode())
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode("utf-8", "replace"), body.decode("utf-8", "replace")


real = None
for i in range(1, ATTEMPTS + 1):
    try:
        head, body = fetch("https://www.kaggle.com/")
    except Exception as exc:
        print("attempt %d: error %s" % (i, exc))
        time.sleep(8)
        continue
    m = re.search(r"<title>(.*?)</title>", body, re.S)
    title = (m.group(1).strip() if m else "(no title)")[:60]
    print("attempt %d: %6d bytes  title=%r" % (i, len(body), title))
    if "reCAPTCHA" not in title and "Checking your browser" not in body:
        real = (head, body)
        break
    time.sleep(8)

if not real:
    print("\nnever got the real page -- still challenged")
    sys.exit(2)

head, body = real
print("\n=== the REAL page (%d bytes) ===" % len(body))

print("\n--- Set-Cookie (names only) ---")
names = []
for line in head.split("\r\n"):
    if line.lower().startswith("set-cookie:"):
        names.append(line.split(":", 1)[1].strip().split("=", 1)[0])
print("  ", names or "(none)")

print("\n--- token / cookie markers in the body ---")
for needle in ("XSRF-TOKEN", "XSRF_COOKIE", "COOKIE_STRING", "document.cookie",
               "csrfmiddlewaretoken", "window.env", "KAGGLE_ENV", "__ENV"):
    print("   %-20s %s" % (needle, needle in body))

print("\n--- window.* assignments ---")
for mm in re.finditer(r"window\.[A-Za-z_$][\w$]*\s*=", body):
    print("   %s" % body[mm.start():mm.start() + 220].replace("\n", " ")[:220])

print("\n--- inline scripts ---")
for i, mm in enumerate(re.finditer(r"<script([^>]*)>(.*?)</script>", body, re.S)):
    inner = mm.group(2).strip()
    if not inner:
        continue
    print("   [%d] %s" % (i, inner[:600].replace("\n", " ")))
    if i > 8:
        break

print("\n--- any cookie assignment in the body ---")
for mm in re.finditer(r"document\.cookie[^;]{0,200}", body):
    print("   %s" % mm.group(0).replace("\n", " ")[:200])
