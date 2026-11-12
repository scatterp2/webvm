#!/usr/bin/env python3
"""One-shot check: fetch a real page through the WebVM proxy and report.

Usage: python proxy_once.py <host> [port] [url]
"""
import socket
import sys
from urllib.parse import quote

HOST = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28082
URL = sys.argv[3] if len(sys.argv) > 3 else "https://example.com/"


def raw_get(path, timeout=45.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, PORT))
    except OSError as exc:
        return None, {}, "connect: {}".format(exc).encode()
    try:
        s.sendall(
            "GET {} HTTP/1.0\r\nHost: {}:{}\r\nConnection: close\r\n\r\n".format(
                path, HOST, PORT).encode())
        data = b""
        while True:
            try:
                chunk = s.recv(16384)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        if not data:
            return None, {}, b"empty response"
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        return lines[0], headers, body
    finally:
        s.close()


status, headers, body = raw_get("/proxy/" + quote(URL, safe=""))
text = body.decode("utf-8", "replace")
print("host={}:{}  url={}".format(HOST, PORT, URL))
print("status          :", status)
print("content-type    :", headers.get("content-type"))
print("x-frame-options :", headers.get("x-frame-options"))
print("csp             :", headers.get("content-security-policy"))
print("bytes           :", len(body))
print("marker injected :", "WEBVM_ADJUSTER_INJECTED" in text)
print("html attr       :", "data-webvm-adjuster-injected" in text)
if "<title>" in text:
    print("title           :", text.split("<title>")[1].split("</title>")[0])
print("--- preview ---")
print(text[:400])
