#!/usr/bin/env python3
"""Show the CDN redirect logic in the deployed proxy."""
import re
import urllib.request

req = urllib.request.Request(
    "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/web_adjuster/launcher.py",
    headers={"User-Agent": "probe", "Cache-Control": "no-cache"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
lines = text.splitlines()

for i, line in enumerate(lines):
    if "REDIRECT" in line or "STATIC_EXT" in line or "_should_redirect" in line:
        lo = max(0, i - 14)
        for j in range(lo, min(len(lines), i + 16)):
            mark = ">>" if j == i else "  "
            print("%s %5d: %s" % (mark, j + 1,
                                   lines[j][:140].encode("ascii", "replace").decode()))
        print("   " + "=" * 62)
