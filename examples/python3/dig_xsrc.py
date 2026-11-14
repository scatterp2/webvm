#!/usr/bin/env python3
"""Dig into how Kaggle delivers the XSRF token.

1. All response headers from a browser-like request (names only for cookies).
2. The page title, to see whether we got the real page or a challenge.
3. Where app.js defines env.XSRF_COOKIE / COOKIE_STRING.
"""
import http.client
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

conn = http.client.HTTPSConnection("www.kaggle.com", timeout=60)
conn.request("GET", "/", headers=HEADERS)
resp = conn.getresponse()
raw = resp.read()
print("status:", resp.status, "bytes:", len(raw))
print("\n=== ALL response headers ===")
for name, value in resp.getheaders():
    if name.lower() == "set-cookie":
        print("  Set-Cookie: %s=<redacted>" % value.split("=", 1)[0].strip())
    else:
        print("  %s: %s" % (name, value[:120]))

import gzip
try:
    if resp.getheader("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
except Exception:
    pass
text = raw.decode("utf-8", "replace")

m = re.search(r"<title>(.*?)</title>", text, re.S)
print("\ntitle:", m.group(1).strip() if m else "(none)")

print("\n=== token-ish strings in the HTML ===")
for needle in ("XSRF", "xsrf", "COOKIE_STRING", "XSRF_COOKIE",
               "document.cookie", "recaptcha", "recaptchaEnterprise"):
    hits = [mm.start() for mm in re.finditer(re.escape(needle), text)]
    print("  %-18s %d hit(s)" % (needle, len(hits)))
    for h in hits[:3]:
        print("      ...%s..." % text[max(0, h - 150):h + 150].replace("\n", " "))

# --- app.js: where does env come from? ---
appjs = open(os.path.join(HERE, "app.js"), encoding="utf-8", errors="replace").read()
print("\n=== app.js: env definitions ===")
for needle in ("XSRF_COOKIE:", "COOKIE_STRING:", "XSRF_COOKIE=", "env=",
               "window.__ENV", "__ENV__", "getEnv"):
    hits = [mm.start() for mm in re.finditer(re.escape(needle), appjs)]
    print("  %-18s %d hit(s)" % (needle, len(hits)))
    for h in hits[:2]:
        print("      ...%s..." % appjs[max(0, h - 260):h + 260].replace("\n", " "))
