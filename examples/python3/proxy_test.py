#!/usr/bin/env python3
"""Exercise the WebVM proxy end-to-end: trigger adjuster start, poll status,
then fetch a real site THROUGH the proxy and report headers + injection marker.

Usage: python proxy_test.py <host> [wrapper_port]
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.69.170.98"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28082
TARGET = "https://example.com/"


def raw_get(port, path, host_header=None, timeout=20.0, extra_headers=""):
    """Return (status_line, headers_dict, body_bytes) from a raw HTTP/1.0 GET."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, port))
    except OSError as exc:
        return None, None, "connect: {}".format(exc).encode()
    try:
        req = "GET {} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n{}".format(
            path, host_header or "{}:{}".format(HOST, port), extra_headers
        )
        s.sendall(req.encode())
        data = b""
        while True:
            try:
                chunk = s.recv(16384)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if len(data) > 5 * 1024 * 1024:
                break
        if not data:
            return None, None, b"empty response"
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        status = lines[0] if lines else ""
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        return status, headers, body
    finally:
        s.close()


def main():
    print("=== target: {}:{} ===".format(HOST, PORT))

    print("\n[1] GET /health")
    status, headers, body = raw_get(PORT, "/health")
    print("    status:", status, "| body:", repr(body[:80]))

    print("\n[2] GET / (triggers lazy adjuster start)")
    status, headers, body = raw_get(PORT, "/")
    print("    status:", status, "| bytes:", len(body))
    has_iframe = b"<iframe" in body
    print("    contains <iframe>:", has_iframe)

    print("\n[3] polling /proxy-status for adjuster readiness")
    ready = False
    for i in range(40):
        status, headers, body = raw_get(PORT, "/proxy-status", timeout=10)
        text = body.decode("utf-8", "replace").strip()
        print("    [{:>2}] {} -> {}".format(i, status, text[:200]))
        if '"state": "ready"' in text or '"state":"ready"' in text:
            ready = True
            break
        time.sleep(3)

    if not ready:
        print("\n    adjuster did NOT reach ready state")

    print("\n[4] GET /proxy/{}  (real site THROUGH the proxy)".format(TARGET))
    from urllib.parse import quote
    status, headers, body = raw_get(PORT, "/proxy/" + quote(TARGET, safe=""), timeout=40)
    print("    status:", status)
    if headers:
        for k in ("server", "x-frame-options", "content-security-policy",
                  "content-type", "location", "content-length"):
            if k in headers:
                print("    {}: {}".format(k, headers[k][:120]))
    print("    body bytes:", len(body))
    text = body.decode("utf-8", "replace")
    print("    has WEBVM_ADJUSTER_INJECTED marker:",
          "WEBVM_ADJUSTER_INJECTED" in text)
    print("    has data-webvm-adjuster-injected attr:",
          "data-webvm-adjuster-injected" in text)
    print("    title:", (text.split("<title>")[1].split("</title>")[0]
                         if "<title>" in text else "n/a"))
    print("\n    --- body preview (first 600 chars) ---")
    print(text[:600])


if __name__ == "__main__":
    main()
