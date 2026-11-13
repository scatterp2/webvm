#!/usr/bin/env python3
"""Check the WebVM's HTTPS proxy on :28443 with full certificate validation."""
import socket
import ssl
import sys
import time
import urllib.request

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
TIMEOUT = 45


def get(url, context=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.status, resp.read()


print("=== DNS ===")
try:
    print("   resolves to", socket.gethostbyname(HOST))
except Exception as exc:
    print("   FAILED", exc)
    sys.exit(1)

print("\n=== TLS handshake with full validation ===")
ctx = ssl.create_default_context()
try:
    t0 = time.time()
    status, body = get("https://{}:{}/health".format(HOST, PORT), ctx)
    print("   OK  status={} body={!r} in {:.1f}s".format(
        status, body[:40], time.time() - t0))
except Exception as exc:
    print("   FAILED:", type(exc).__name__, exc)

print("\n=== certificate chain ===")
try:
    raw = socket.create_connection((HOST, PORT), timeout=20)
    with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
        for part in tls.get_verified_chain():
            print("   subject:", part.subject.rfc4514_string())
            print("   issuer :", part.issuer.rfc4514_string())
except Exception as exc:
    print("   (chain fetch failed:", type(exc).__name__, exc, ")")

print("\n=== the proxied page ===")
try:
    status, body = get("https://{}:{}/proxy/{}".format(
        HOST, PORT, "https%3A%2F%2Fwww.kaggle.com%2F"), ctx)
    text = body.decode("utf-8", "replace")
    print("   status:", status, "bytes:", len(body))
    print("   title :", (text.split("<title>")[1].split("</title>")[0]
                         if "<title>" in text else "n/a"))
    print("   marker:", "WEBVM_ADJUSTER_INJECTED" in text)
except Exception as exc:
    print("   FAILED:", type(exc).__name__, exc)
