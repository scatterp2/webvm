#!/usr/bin/env python3
"""Local test for the in-process WebVM proxy.

Starts launcher.py on high local ports, exercises the control endpoints and
proxied fetches, and checks that framing headers are stripped and the proof
script is injected.
"""
import json
import socket
import sys
import threading
import time
from urllib.parse import quote

sys.path.insert(0, "web_adjuster")

import launcher  # noqa: E402


def raw_get(port, path, host_header=None, timeout=40.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
    except OSError as exc:
        return None, None, "connect: {}".format(exc).encode()
    try:
        s.sendall(
            "GET {} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n".format(
                path, host_header or "127.0.0.1:{}".format(port)
            ).encode()
        )
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
            return None, None, b"empty"
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


def main():
    wrapper_port = 38082
    forward_port = 38081
    servers = [
        launcher.start_wrapper(wrapper_port, "wrapper"),
        launcher.start_wrapper(forward_port, "forward"),
    ]
    time.sleep(1.0)

    failures = []

    status, headers, body = raw_get(wrapper_port, "/health")
    print("[1] /health -> {} {!r}".format(status, body[:40]))
    if status is None or "200" not in status or b"ok" not in body:
        failures.append("/health did not answer 200 ok")

    status, headers, body = raw_get(wrapper_port, "/proxy-status")
    print("[2] /proxy-status -> {} {}".format(status, body[:120].decode("utf-8", "replace")))
    if status is None or b"state" not in body:
        failures.append("/proxy-status did not return JSON")

    status, headers, body = raw_get(wrapper_port, "/")
    print("[3] / -> {} bytes={} has_iframe={}".format(
        status, len(body), b"<iframe" in body))
    if status is None or b"<iframe" not in body:
        failures.append("/ did not render the iframe page")

    # Proxy a real site.
    target = "https://example.com/"
    path = "/proxy/" + quote(target, safe="")
    status, headers, body = raw_get(wrapper_port, path)
    text = body.decode("utf-8", "replace")
    print("[4] GET {} -> {}".format(path, status))
    print("    content-type   : {}".format(headers.get("content-type")))
    print("    x-frame-options: {}".format(headers.get("x-frame-options")))
    print("    body bytes     : {}".format(len(body)))
    print("    title          : {}".format(
        text.split("<title>")[1].split("</title>")[0] if "<title>" in text else "n/a"))
    print("    marker present : {}".format("WEBVM_ADJUSTER_INJECTED" in text))
    print("    attr present   : {}".format("data-webvm-adjuster-injected" in text))
    if status is None or "200" not in status:
        failures.append("proxied fetch of example.com failed")
    if "WEBVM_ADJUSTER_INJECTED" not in text:
        failures.append("injection marker missing from proxied body")
    if "x-frame-options" in headers:
        failures.append("x-frame-options was not stripped")

    # Forward proxy mode: absolute-URI request line.
    status, headers, body = raw_get(forward_port, "/")
    print("[5] forward proxy / (relative target) -> {}".format(status))

    # The browser sends "Accept-Encoding: gzip, deflate, br". The proxy must
    # not hand back a compressed body, otherwise the HTML rewriting and the
    # script injection would corrupt it.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(40)
    s.connect(("127.0.0.1", wrapper_port))
    s.sendall(
        ("GET /proxy/{} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\n"
         "Accept-Encoding: gzip, deflate, br\r\n"
         "Connection: close\r\n\r\n").format(
            quote("https://example.com/", safe=""), wrapper_port).encode()
    )
    data = b""
    while True:
        try:
            chunk = s.recv(16384)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    print("[7] gzip Accept-Encoding -> {}".format(
        head.split(b"\r\n")[0].decode("utf-8", "replace")))
    print("    content-encoding header present: {}".format(
        b"content-encoding" in head.lower()))
    print("    body is readable text            : {}".format(
        b"Example Domain" in body))
    print("    marker injected                  : {}".format(
        b"WEBVM_ADJUSTER_INJECTED" in body))
    if b"WEBVM_ADJUSTER_INJECTED" not in body:
        failures.append("gzip response was not rewritten/injected")

    # Unit check for the decompression safety net.
    import gzip
    packed = gzip.compress(b"<html><body>hello</body></html>")
    unpacked, still = launcher.decompress_body(packed, "gzip")
    print("[8] decompress_body(gzip) -> {!r} still_compressed={}".format(
        unpacked, still))
    if still or b"hello" not in unpacked:
        failures.append("decompress_body did not unpack gzip")

    # Connection reuse: the second request for the same host must reuse the
    # pooled keep-alive connection instead of doing another TLS handshake.
    print("[9] connection reuse")
    launcher._POOL.clear()
    launcher._CACHE.clear()
    first = launcher.upstream_fetch("https://example.com/", headers={})
    second = launcher.upstream_fetch("https://example.com/", headers={})
    print("    first  -> status={} bytes={}".format(first[0], len(first[2])))
    print("    second -> status={} bytes={}".format(second[0], len(second[2])))
    pooled = launcher._POOL.get(("https", "example.com", 443), [])
    print("    pooled connections for example.com: {}".format(len(pooled)))
    if not pooled:
        failures.append("no connection was pooled for reuse")

    # Cache: the same URL twice must be served from the cache.
    launcher._CACHE.clear()
    launcher.upstream_fetch("https://example.com/", headers={})
    hit = launcher._cache_get(("GET", "https://example.com/"))
    print("[10] response cache -> {}".format("HIT" if hit else "MISS"))
    if hit is None:
        failures.append("response was not cached")

    # CSP nonce: a nonce in script-src makes browsers ignore
    # 'unsafe-inline', so the injected script must carry the same nonce.
    page = ('<html><head><script nonce="abc123">var a=1;</script></head>'
            '<body><p>hi</p></body></html>')
    nonce = launcher.extract_nonce(page)
    script = launcher.build_injection_script(nonce)
    print("[11] nonce extraction -> {!r}".format(nonce))
    print("    injected script carries the nonce: {}".format(
        'nonce="abc123"' in script))
    if nonce != "abc123":
        failures.append("nonce was not extracted from the page")
    if 'nonce="abc123"' not in script:
        failures.append("injected script does not carry the page nonce")
    plain = launcher.build_injection_script(None)
    if "nonce" in plain:
        failures.append("injected script has a nonce when it should not")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(40)
    s.connect(("127.0.0.1", forward_port))
    s.sendall(
        "GET http://example.com/ HTTP/1.0\r\nHost: example.com\r\n"
        "Connection: close\r\n\r\n".encode()
    )
    data = b""
    while True:
        try:
            chunk = s.recv(16384)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head = data.split(b"\r\n\r\n", 1)
    print("[6] forward proxy absolute-URI -> {}".format(
        head[0].split(b"\r\n")[0].decode("utf-8", "replace")))
    if b"WEBVM_ADJUSTER_INJECTED" not in data:
        failures.append("forward proxy did not inject the marker")

    # Concurrency: a heavy page asks for many resources at once.  The proxy
    # must queue them instead of opening them all at the same time, or
    # CheerpX's cooperative scheduler stalls and the VM appears to hang.
    print("[11] concurrent requests")
    import threading
    results = []
    lock = threading.Lock()

    def one(index):
        try:
            status, headers, body = raw_get(
                wrapper_port,
                "/proxy/" + quote("https://example.com/?n=%d" % index, safe=""),
            )
            with lock:
                results.append((status, len(body)))
        except Exception as exc:
            with lock:
                results.append(("error: %s" % exc, 0))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(90)
    ok = sum(1 for status, _ in results
             if status and "200" in str(status))
    print("    completed {}/{} with 200".format(ok, len(results)))
    if ok != 12:
        failures.append("only {}/{} concurrent requests succeeded".format(ok, len(results)))

    for server in servers:
        server.shutdown()

    print()
    if failures:
        print("FAILURES:")
        for item in failures:
            print("  - {}".format(item))
        return 1
    print("ALL LOCAL PROXY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
