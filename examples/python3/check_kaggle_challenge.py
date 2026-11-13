#!/usr/bin/env python3
"""Fetch a few Kaggle endpoints through the proxy and look for a challenge."""
import socket
import ssl
import sys
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()

TARGETS = [
    "https://www.kaggle.com/",
    "https://www.kaggle.com/users.HomePageService/GetLoggedOutHome",
    "https://www.kaggle.com/api/i/homepage.HomeService/GetHomePage",
    "https://www.kaggle.com/api/v1/kernels/list",
]


def fetch(path):
    s = socket.socket()
    s.settimeout(60)
    s.connect((HOST, PORT))
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


for target in TARGETS:
    path = "/proxy/" + quote(target, safe="")
    head, body = fetch(path)
    status = head.split("\r\n")[0]
    low = body.lower()
    challenge = ("recaptcha" in low or "g-recaptcha" in low
                 or "checking your browser" in low)
    print("== %s" % target)
    print("   %s  %d bytes  challenge=%s" % (status, len(body), challenge))
    if challenge:
        i = low.find("recaptcha")
        print("   ...%s..." % body[max(0, i - 120):i + 160].replace("\n", " "))
    print()
