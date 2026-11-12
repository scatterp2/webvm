#!/usr/bin/env python3
"""Fetch a page through the proxy and report the CSP and the injected script.

Usage: python inspect_proxy.py <host> <port> <url>
"""
import re
import socket
import sys
from urllib.parse import quote

HOST = sys.argv[1]
PORT = int(sys.argv[2])
URL = sys.argv[3] if len(sys.argv) > 3 else "https://www.kaggle.com/"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(120)
s.connect((HOST, PORT))
s.sendall(
    ("GET /proxy/{} HTTP/1.0\r\nHost: {}:{}\r\n"
     "Accept: text/html,application/xhtml+xml\r\n"
     "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36\r\n"
     "Accept-Language: en-US,en;q=0.9\r\nConnection: close\r\n\r\n").format(
        quote(URL, safe=""), HOST, PORT).encode()
)
data = b""
while True:
    try:
        chunk = s.recv(65536)
    except socket.timeout:
        break
    if not chunk:
        break
    data += chunk
s.close()

head, _, body = data.partition(b"\r\n\r\n")
headers = {}
for line in head.decode("utf-8", "replace").split("\r\n")[1:]:
    if ":" in line:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()

text = body.decode("utf-8", "replace")
print("status      :", head.split(b"\r\n")[0].decode("utf-8", "replace"))
print("bytes       :", len(body))
print("title       :", (re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I) or [None, "n/a"])[1][:80])
print()
print("CSP header  :", headers.get("content-security-policy", "(none)")[:400])
print()
csp = headers.get("content-security-policy", "")
csp_nonces = re.findall(r"'nonce-([^']+)'", csp)
html_nonces = re.findall(r'nonce="([^"]+)"', text)
print("nonce in CSP :", csp_nonces[:3])
print("nonce in HTML:", sorted(set(html_nonces))[:3])
print()
inj = re.search(r'<script nonce="([^"]*)">\(\(function\(\)\{try\{document', text)
print("injected script nonce:", inj.group(1) if inj else "(no nonce on injected script)")
print("injected script present:", "WEBVM_ADJUSTER_INJECTED" in text)
if csp_nonces and html_nonces:
    print("CSP nonce is among the HTML nonces:", csp_nonces[0] in html_nonces)
if inj and csp_nonces:
    print("injected nonce matches the CSP nonce:", inj.group(1) == csp_nonces[0])
print()
print("script tags:", len(re.findall(r"<script", text, re.I)))
print("nonced script tags:", len(re.findall(r"<script[^>]*nonce=", text, re.I)))
