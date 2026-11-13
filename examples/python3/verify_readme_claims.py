#!/usr/bin/env python3
"""Check the online README's claims against the deployed code.

Usage: python verify_readme_claims.py
"""
import urllib.request

BASE = "https://raw.githubusercontent.com/scatterp2/webvm/main/"


def fetch(path):
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": "probe", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def listdir(path):
    req = urllib.request.Request(
        "https://api.github.com/repos/scatterp2/webvm/contents/" + path,
        headers={"User-Agent": "probe"})
    import json
    return [e["name"] for e in json.loads(
        urllib.request.urlopen(req, timeout=30).read().decode())]


CLAIMS = [
    ("src/app.html", [
        ("loads the worker with a ?v= query", "serviceWorker.js?v="),
        ("which ?v= value?", None),
    ]),
    ("src/lib/network.js", [
        ("sets the node hostname to webvm-proxy", 'hostname'),
    ]),
    ("src/lib/WebVM.svelte", [
        ("uses a credentialless iframe", "credentialless"),
        ("points the iframe at the TLS proxy", "28443"),
        ("still polls the bridge inbox", "proxy-inbox"),
        ("still opens the bridge popup", "bridge=1"),
    ]),
    ("examples/python3/web_adjuster/launcher.py", [
        ("rewrites assets to direct CDN URLs", "_direct_url"),
        ("injects a history.replaceState fix", "replaceState"),
        ("redirects static assets", "302"),
        ("strips sec-fetch headers", "sec-fetch"),
    ]),
    ("examples/python3/pi_impl.py", [
        ("fetches the LE certificate at boot", "webvm-proxy.crt"),
        ("fetches the LE key at boot", "webvm-proxy.key"),
    ]),
    ("serviceWorker.js", [
        ("still has the bridge inbox", "PROXY_INBOX_PATH"),
        ("still has the bridge routes", "httpsproxy"),
    ]),
]

for path, claims in CLAIMS:
    try:
        text = fetch(path)
    except Exception as exc:
        print("FAIL", path, exc)
        continue
    print("\n== " + path + " (" + str(len(text)) + " chars)")
    for label, needle in claims:
        if needle is None:
            continue
        print("   " + ("YES " if needle in text else "no  ") + label)

print("\n== examples/python3/web_adjuster/tls/ ==")
try:
    for name in sorted(listdir("examples/python3/web_adjuster/tls")):
        print("   " + name)
except Exception as exc:
    print("   FAIL", exc)

print("\n== app.html worker version ==")
text = fetch("src/app.html")
for line in text.splitlines():
    if "serviceWorker.js" in line:
        print("   " + line.strip())
