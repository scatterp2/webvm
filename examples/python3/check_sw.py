#!/usr/bin/env python3
"""Check the deployed service worker for the expected markers."""
import urllib.request

url = "https://scatterp2.github.io/webvm/serviceWorker.js?v=5"
req = urllib.request.Request(url, headers={"User-Agent": "probe",
                                           "Cache-Control": "no-cache"})
s = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
print("chars            :", len(s))
print("has wantsCoi     :", "wantsCoi" in s)
print("deletes COOP     :", 'delete("Cross-Origin-Opener-Policy")' in s)
print("deletes COEP     :", 'delete("Cross-Origin-Embedder-Policy")' in s)
print("has bridge       :", "httpsproxy" in s)
print("has diag         :", "handleDiag" in s)
