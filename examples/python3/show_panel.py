#!/usr/bin/env python3
"""Show every fetch the deployed WebVM.svelte makes, and the panel's loader."""
import re
import urllib.request

req = urllib.request.Request(
    "https://raw.githubusercontent.com/scatterp2/webvm/main/src/lib/WebVM.svelte",
    headers={"User-Agent": "probe", "Cache-Control": "no-cache"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
lines = text.splitlines()

print("=== every fetch( in the file ===")
for i, line in enumerate(lines):
    if "fetch(" in line:
        print("%5d: %s" % (i + 1, line.strip()[:150].encode("ascii", "replace").decode()))

print("\n=== loadProxyFrame ===")
for i, line in enumerate(lines):
    if "function loadProxyFrame" in line:
        for j in range(i, min(len(lines), i + 45)):
            print("%5d: %s" % (j + 1, lines[j][:150].encode("ascii", "replace").decode()))
        break

print("\n=== 'Navigated to HTTPS proxy' context ===")
for i, line in enumerate(lines):
    if "Navigated to HTTPS" in line:
        for j in range(max(0, i - 12), min(len(lines), i + 8)):
            print("%5d: %s" % (j + 1, lines[j][:150].encode("ascii", "replace").decode()))
        break
