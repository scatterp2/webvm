#!/usr/bin/env python3
"""Show how the deployed pi_impl.py obtains its TLS material."""
import re
import urllib.request

req = urllib.request.Request(
    "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/pi_impl.py",
    headers={"User-Agent": "probe", "Cache-Control": "no-cache"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

for i, line in enumerate(text.splitlines(), 1):
    if re.search(r"tls|\.crt|\.key|TLS_|cert", line, re.I):
        print("%5d: %s" % (i, line[:150]))
