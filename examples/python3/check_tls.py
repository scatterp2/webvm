#!/usr/bin/env python3
"""Check the WebVM's HTTPS proxy endpoint.

Usage: python check_tls.py <host> [port]
"""
import socket
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.83.142.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28443
CA = Path(__file__).resolve().parent / "web_adjuster" / "tls" / "dev-proxy-ca.crt"

url = "https://{}:{}/health".format(HOST, PORT)

print("== with the dev CA (verifies the chain) ==")
try:
    ctx = ssl.create_default_context(cafile=str(CA))
    with urllib.request.urlopen(url, timeout=25, context=ctx) as resp:
        print("  status:", resp.status, "body:", resp.read().decode().strip())
except Exception as exc:
    print("  FAILED:", type(exc).__name__, exc)

print()
print("== without verification ==")
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, timeout=25, context=ctx) as resp:
        print("  status:", resp.status, "body:", resp.read().decode().strip())
except Exception as exc:
    print("  FAILED:", type(exc).__name__, exc)

print()
print("== certificate details ==")
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((HOST, PORT), timeout=15) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
            cert = tls.getpeercert()
            print("  subject:", cert.get("subject"))
            print("  SAN    :", cert.get("subjectAltName"))
            print("  issuer :", cert.get("issuer"))
except Exception as exc:
    print("  FAILED:", type(exc).__name__, exc)
