#!/usr/bin/env python3
"""Show the _direct_url rewriting in the deployed proxy."""
import re
import urllib.request

req = urllib.request.Request(
    "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/web_adjuster/launcher.py",
    headers={"User-Agent": "probe", "Cache-Control": "no-cache"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
lines = text.splitlines()

for i, line in enumerate(lines):
    if "def _direct_url" in line:
        for j in range(i, min(len(lines), i + 55)):
            mark = ">>" if j == i else "  "
            print("%s %5d: %s" % (mark, j + 1,
                                   lines[j][:140].encode("ascii", "replace").decode()))
        break
