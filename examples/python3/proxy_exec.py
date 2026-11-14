#!/usr/bin/env python3
"""Drive the proxied page from the host.

The page runs in a cross-origin iframe, so the page that embeds it
cannot script it.  The proxy's injected script can: it polls the proxy
for an action and runs it.  This is the client for that channel.

    python proxy_exec.py --target https://www.kaggle.com \
        --action '{"type":"click","text":"Sign in with Google"}'

    python proxy_exec.py --target https://www.kaggle.com --snapshot
    python proxy_exec.py --target https://www.kaggle.com --text

The action vocabulary is fixed and is implemented inside the page, so no
caller-supplied code is ever evaluated:

    {"type": "click", "selector": "..."}   click the first match
    {"type": "click", "text": "..."}       click the first element whose
                                          text contains the string
    {"type": "type", "selector": "...", "text": "..."}
    {"type": "snapshot"}                   list the clickable elements
    {"type": "text"}                       the page's text
    {"type": "url"}                        the page's URL
    {"type": "scroll", "y": 123}
"""
import argparse
import json
import socket
import ssl
import time
from urllib.parse import quote

HOST = "webvm-proxy.tailf987e.ts.net"
PORT = 28443
CTX = ssl.create_default_context()


def request(path, method="GET", body=b""):
    s = socket.socket()
    s.settimeout(60)
    s.connect((HOST, PORT))
    s = CTX.wrap_socket(s, server_hostname=HOST)
    head = ("{method} {path} HTTP/1.0\r\nHost: {host}:{port}\r\n"
            "User-Agent: proxy-exec/1.0\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {length}\r\n"
            "Connection: close\r\n\r\n").format(
        method=method, path=path, host=HOST, port=PORT, length=len(body))
    s.sendall(head.encode() + body)
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except Exception:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head_bytes, _, payload = data.partition(b"\r\n\r\n")
    status = head_bytes.split(b"\r\n")[0].decode("utf-8", "replace")
    return status, payload.decode("utf-8", "replace")


def queue(target, action):
    status, payload = request(
        "/proxy-exec?target=" + quote(target, safe=""), "POST",
        json.dumps(action).encode("utf-8"))
    print("queued  : %s %s" % (status, payload.strip()))
    return status


def wait_for_result(target, timeout=25):
    deadline = time.time() + timeout
    seen = ""
    while time.time() < deadline:
        status, payload = request(
            "/proxy-exec/result?target=" + quote(target, safe=""))
        if payload.strip() and payload != seen:
            seen = payload
            print("result  : %s" % payload)
            return payload
        time.sleep(1)
    print("result  : (nothing within %ss)" % timeout)
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="https://www.kaggle.com",
                        help="the origin whose page should act")
    parser.add_argument("--action", help="the action, as JSON")
    parser.add_argument("--click", help="click the first element with this text")
    parser.add_argument("--selector", help="CSS selector to click")
    parser.add_argument("--type-into", help="CSS selector to type into")
    parser.add_argument("--text", action="store_true", help="read the page text")
    parser.add_argument("--snapshot", action="store_true",
                        help="list the page's clickable elements")
    parser.add_argument("--url", action="store_true", help="read the page URL")
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()

    action = None
    if args.action:
        action = json.loads(args.action)
    elif args.click:
        action = {"type": "click", "text": args.click}
    elif args.selector:
        action = {"type": "click", "selector": args.selector}
    elif args.type_into:
        action = {"type": "type", "selector": args.type_into, "text": ""}
    elif args.text:
        action = {"type": "text"}
    elif args.snapshot:
        action = {"type": "snapshot"}
    elif args.url:
        action = {"type": "url"}

    if action is None:
        parser.error("nothing to do: pass --action, --click, --selector, "
                     "--type-into, --text, --snapshot or --url")

    queue(args.target, action)
    wait_for_result(args.target, args.timeout)


if __name__ == "__main__":
    main()
