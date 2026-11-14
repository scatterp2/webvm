#!/usr/bin/env python3
"""What cookies does Kaggle set on the logged-out home page?

Names only, never values.
"""
import http.client
import sys

conn = http.client.HTTPSConnection("www.kaggle.com", timeout=60)
conn.request("GET", "/", headers={
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
})
resp = conn.getresponse()
body = resp.read()
print("status:", resp.status, "bytes:", len(body))
print("\n=== Set-Cookie (names only) ===")
for name, value in resp.getheaders():
    if name.lower() == "set-cookie":
        cname = value.split("=", 1)[0].strip()
        attrs = [a.strip() for a in value.split(";")[1:]]
        print("  %-28s attrs=%s" % (cname, ",".join(attrs) or "-"))

text = body.decode("utf-8", "replace")
print("\n=== env / token markers in the HTML ===")
for needle in ("XSRF_COOKIE", "COOKIE_STRING", "window.env", "__ENV__",
               "XSRF-TOKEN", "build-hash", "X-Kaggle-Build-Version"):
    print("  %-24s present: %s" % (needle, needle in text))

import re
m = re.search(r"(window\.env|__ENV__|env)\s*=\s*\{", text)
if m:
    print("\n=== env assignment context ===")
    print(text[m.start():m.start() + 900].replace("\n", " ")[:900])
