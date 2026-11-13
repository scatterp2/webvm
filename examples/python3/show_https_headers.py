#!/usr/bin/env python3
"""Show the response headers of the WebVM's https proxy endpoints."""
import ssl
import sys
import urllib.request

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443

ctx = ssl.create_default_context()


def head(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
        print("== " + url)
        for k, v in resp.headers.items():
            print("   %s: %s" % (k, v[:120]))
        print("   (body %d bytes)" % len(resp.read()))
    print()


head("https://{}:{}/health".format(HOST, PORT))
head("https://{}:{}/".format(HOST, PORT))
head("https://{}:{}/proxy/{}".format(HOST, PORT, "https%3A%2F%2Fwww.kaggle.com%2F"))
