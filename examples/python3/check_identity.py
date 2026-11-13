#!/usr/bin/env python3
"""Check that the node identity still matches the LE certificate.

The LE certificate is issued for CN=webvm-proxy.tailf987e.ts.net.  The
WebVM node must register under that name or the certificate will not
validate, and the proxy panel's https origin will not resolve.
"""
import json
import re
import subprocess
import urllib.request

BASE = "https://raw.githubusercontent.com/scatterp2/webvm/main/"


def fetch(path):
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": "probe", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


print("=== src/lib/network.js ===")
net = fetch("src/lib/network.js")
for line in net.splitlines():
    if re.search(r"authKey|hostname|DEFAULT_AUTH|tskey", line):
        shown = line.strip()
        # Never print a full secret: mask everything after the prefix.
        shown = re.sub(r"(tskey-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+", r"\1…", shown)
        print("   " + shown[:140])

print("\n=== the live tailnet nodes ===")
out = subprocess.run(
    [r"C:\Program Files\Tailscale\Tailscale.exe", "status", "--json"],
    capture_output=True, text=True, timeout=25).stdout
data = json.loads(out)
for peer in (data.get("Peer") or {}).values():
    if not peer.get("Online"):
        continue
    dns = (peer.get("DNSName") or "").rstrip(".")
    ips = peer.get("TailscaleIPs") or []
    tags = peer.get("Tags") or []
    print("   %-34s %-16s tags=%s" % (dns, ips[0] if ips else "", tags))

print("\n=== what the panel points at ===")
sv = fetch("src/lib/WebVM.svelte")
for line in sv.splitlines():
    if "28443" in line or "webvm-proxy" in line or "credentialless" in line:
        print("   " + line.strip()[:140])
