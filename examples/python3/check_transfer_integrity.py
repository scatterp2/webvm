#!/usr/bin/env python3
"""Is the proxy's response transfer intact?

The browser reported ERR_SSL_PROTOCOL_ERROR on a 99 KB response even
though the proxy logged SEND_OK for the whole thing.  This fetches the
same kinds of response with an ordinary client, so a short or corrupt
body points at the proxy's write path rather than the browser.
"""
import socket
import ssl
import time
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()


def fetch(path, method="GET", body=b"", extra_headers=""):
    s = socket.socket()
    s.settimeout(120)
    s.connect((HOST, PORT))
    s = CTX.wrap_socket(s, server_hostname=HOST)
    head = ("{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36\r\n"
            "Accept: */*\r\nAccept-Language: en-US,en;q=0.9\r\n"
            "{extra}Connection: close\r\n\r\n").format(
        method=method, path=path, host=HOST, port=PORT, extra=extra_headers)
    s.sendall(head.encode() + body)
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except Exception as exc:
            print("   recv error:", type(exc).__name__, exc)
            break
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def report(label, data):
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode("utf-8", "replace")
    length = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    print("%s" % label)
    print("   status        : %s" % status)
    print("   body received : %d bytes" % len(body))
    print("   content-length: %s" % length)
    if length is not None:
        if len(body) == length:
            print("   VERDICT       : intact")
        else:
            print("   VERDICT       : TRUNCATED (short by %d bytes)"
                  % (length - len(body)))
    return len(body), length


print("=== 1. the Kaggle API call that failed in the browser ===")
path = "/api/i/users.HomePageService/GetLoggedOutHomepageData"
data = fetch(path, method="POST", body=b"")
report("POST " + path, data)

print()
print("=== 2. a large file through the proxy ===")
target = ("https://raw.githubusercontent.com/scatterp2/webvm/main/"
          "examples/python3/web_adjuster/launcher.py")
data = fetch("/proxy/" + quote(target, safe=""))
report("GET the launcher", data)

print()
print("=== 3. the same file again (the proxy caches it) ===")
data = fetch("/proxy/" + quote(target, safe=""))
report("GET the launcher again", data)
