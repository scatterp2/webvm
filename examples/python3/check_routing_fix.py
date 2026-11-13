#!/usr/bin/env python3
"""Check whether the routing fix reaches the served HTML."""
import re
import ssl
import sys
import urllib.request

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://www.kaggle.com/"

ctx = ssl.create_default_context()
url = "https://{}:{}/proxy/{}".format(HOST, PORT, TARGET.replace(":", "%3A").replace("/", "%2F"))
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
    text = resp.read().decode("utf-8", "replace")

print("bytes:", len(text))
print("has replaceState:", "replaceState" in text)
print("has marker:", "WEBVM_ADJUSTER_INJECTED" in text)

if "replaceState" in text:
    i = text.find("replaceState")
    print("\n--- context around the injection ---")
    print(text[max(0, i - 420):i + 160].replace("><", ">\n<"))
else:
    print("\n--- the first 700 chars of <head> ---")
    h = text.lower().find("<head")
    print(text[h:h + 700].replace("><", ">\n<"))

print("\n--- first <script> position ---")
m = re.search(r"<script", text, re.I)
print("index:", m.start() if m else None)
if m:
    print(text[m.start():m.start() + 300].replace("><", ">\n<"))
