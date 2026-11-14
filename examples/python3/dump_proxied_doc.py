#!/usr/bin/env python3
"""Dump the structure of the document the proxy serves for Kaggle."""
import re
import socket
import ssl
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()


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


head, body = fetch("https://www.kaggle.com/")

m = re.search(r"<title>(.*?)</title>", body, re.S)
print("title      :", (m.group(1).strip() if m else "(none)")[:80])
print("body bytes :", len(body))
print("challenge? :", "Checking your browser" in body or "recaptcha/enterprise.js" in body
      and "window.WIZ_global_data" in body)

print("\n=== window.* assignments in the document ===")
for mm in re.finditer(r"window\.[A-Za-z_$][\w$]*\s*=", body):
    name = mm.group(0)
    snippet = body[mm.start():mm.start() + 150].replace("\n", " ")
    print("  %-34s %s" % (name, snippet[:130]))

print("\n=== inline <script> blocks (first 400 chars each) ===")
for i, mm in enumerate(re.finditer(r"<script([^>]*)>(.*?)</script>", body, re.S)):
    attrs = (mm.group(1) or "").strip()
    inner = mm.group(2).strip()
    if not inner:
        continue
    print("  [%d] attrs=%s" % (i, attrs[:90]))
    print("      %s" % inner[:400].replace("\n", " "))
    if i > 6:
        break
