#!/usr/bin/env python3
"""Check whether the deployed Pages bundle contains the Tailscale-IP banner code."""
import re
import urllib.request

BASE = "https://scatterp2.github.io/webvm"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")


html = fetch(BASE + "/")
chunks = sorted(set(re.findall(r'/_app/immutable/[^"\'\s]+\.js', html)))
print("chunks referenced:", len(chunks))

found = []
for chunk in chunks:
    try:
        body = fetch(BASE + chunk)
    except Exception as exc:
        print("  fetch failed", chunk, exc)
        continue
    if "TAILSCALE_IP" in body:
        found.append(chunk + "  <- TAILSCALE_IP")
    if "currentIpStore" in body:
        found.append(chunk + "  <- currentIpStore")

print()
if found:
    for item in found:
        print(" ", item)
    print("\n=> deployed bundle CONTAINS the new code")
else:
    print("=> deployed bundle is STALE (no TAILSCALE_IP / currentIpStore)")
