#!/usr/bin/env python3
"""Download Kaggle's app.js and find the anti-forgery logic.

Read-only diagnostic: we look at how the token is obtained and sent.
"""
import os
import re
import sys
import urllib.request

URL = "https://www.kaggle.com/static/assets/app.js?v=4b5a7e1fe58cdf18"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.js")

req = urllib.request.Request(URL, headers={
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
})
try:
    data = urllib.request.urlopen(req, timeout=90).read()
except Exception as exc:
    print("download failed: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)

with open(OUT, "wb") as fh:
    fh.write(data)
text = data.decode("utf-8", "replace")
print("downloaded %d bytes -> %s" % (len(data), OUT))
print("lines: %d" % text.count("\n"))

NEEDLES = [
    "anti-forgery", "antiForgery", "anti_forgery", "antiforgery",
    "GetLoggedOutHomepageData", "HomePageService",
    "xsrf", "XSRF", "csrf", "CSRF",
    "X-XSRF-TOKEN", "x-kaggle", "Kaggle-",
]

for needle in NEEDLES:
    hits = [m.start() for m in re.finditer(re.escape(needle), text)]
    print("\n=== %s : %d hit(s)" % (needle, len(hits)))
    for h in hits[:4]:
        lo = max(0, h - 220)
        snippet = text[lo:h + 220].replace("\n", " ")
        print("  ...%s..." % snippet)
