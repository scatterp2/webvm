#!/usr/bin/env python3
"""Inspect what the proxy returns for the Kaggle document.

Header NAMES only for anything sensitive (Set-Cookie names, no values).
"""
import socket
import ssl
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()

target = "https://www.kaggle.com/"
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
head = head.decode("utf-8", "replace")
body = body.decode("utf-8", "replace")

print("=== response head ===")
for line in head.split("\r\n"):
    name = line.split(":", 1)[0].strip()
    if name.lower() == "set-cookie":
        # names only, never values
        cookie_name = line.split(":", 1)[1].strip().split("=", 1)[0]
        print("Set-Cookie: %s=<redacted>" % cookie_name)
    else:
        print(line[:150])

print("\n=== body facts ===")
print("bytes: %d" % len(body))
for needle in ("anti_forgery", "antiForgery", "csrf", "CSRF", "xsrf", "XSRF",
               "X-XSRF", "xsrf-token", "_token"):
    print("  %-14s in body: %s" % (needle, needle in body))

print("\n=== proof / routing scripts present? ===")
print("  proof marker   :", "WEBVM_ADJUSTER_INJECTED" in body)
print("  fetch patch    :", "window.fetch=function" in body)
print("  xhr patch      :", "XMLHttpRequest.prototype.open" in body)
print("  replaceState   :", "replaceState" in body)

# any token-looking assignments in the html
import re
for m in re.finditer(r"[A-Za-z_$][\w$]{0,40}\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", body):
    print("  token-ish: %s" % m.group(0)[:110])
