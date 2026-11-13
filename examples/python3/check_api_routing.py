#!/usr/bin/env python3
"""Check that the API-routing patch is injected into a proxied page."""
import socket
import sys
import threading
import time
from urllib.parse import quote

sys.path.insert(0, ".")
from web_adjuster import launcher  # noqa: E402

WRAPPER = 38082
TARGET = "https://example.com/"

server = launcher.start_wrapper(WRAPPER, "wrapper")
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(1)

path = "/proxy/" + quote(TARGET, safe="")
s = socket.socket()
s.settimeout(60)
s.connect(("127.0.0.1", WRAPPER))
s.sendall(("GET %s HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n"
            "Connection: close\r\n\r\n" % (path, WRAPPER)).encode())
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

text = data.split(b"\r\n\r\n", 1)[1].decode("utf-8", "replace")
print("bytes:", len(text))
print("has proof marker        :", "WEBVM_ADJUSTER_INJECTED" in text)
print("has XMLHttpRequest patch:", "XMLHttpRequest.prototype.open" in text)
print("has fetch patch         :", "window.fetch=function" in text)
print("has target origin       :", '"https://example.com"' in text)
i = text.find("XMLHttpRequest.prototype.open")
if i > 0:
    print("\n--- the routing patch ---")
    print(text[max(0, i - 420):i + 120].replace("><", ">\n<"))
server.shutdown()
