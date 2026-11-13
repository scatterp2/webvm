#!/usr/bin/env python3
"""Check the two open Goal 5 items in the deployed proxy code."""
import re
import urllib.request

req = urllib.request.Request(
    "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/web_adjuster/launcher.py",
    headers={"User-Agent": "probe", "Cache-Control": "no-cache"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
lines = text.splitlines()

print("=== 302 redirect logic ===")
for i, line in enumerate(lines):
    if re.search(r"30[12]|redirect", line, re.I):
        print("%5d: %s" % (i + 1, line[:150].encode("ascii", "replace").decode()))

print("\n=== replaceState injection ===")
for i, line in enumerate(lines):
    if "replaceState" in line:
        lo = max(0, i - 8)
        for j in range(lo, min(len(lines), i + 6)):
            print("%5d: %s" % (j + 1, lines[j][:150].encode("ascii", "replace").decode()))
        print("   ---")
