#!/usr/bin/env python3
"""Check that the deployed files contain the expected changes."""
import urllib.request

BASE = "https://raw.githubusercontent.com/scatterp2/webvm/main/"

CHECKS = [
    ("examples/python3/web_adjuster/launcher.py", [
        'params.get("bridge")',
        "proxy-inbox",
        "/proxy-inline/start/",
        "start_tls_wrapper",
        "SEND_CHUNK",
        "_watchdog",
        "build_self_contained",
        "Access-Control-Allow-Origin",
    ]),
    ("src/lib/WebVM.svelte", [
        "proxy-inbox",
        "pollBridgePopup",
        "TAILSCALE_IP",
        "bridge=1",
    ]),
    ("serviceWorker.js", [
        "PROXY_INBOX_PATH",
        "handleProxyInbox",
        "Cross-Origin-Opener-Policy",
        "httpsproxy",
    ]),
    ("examples/python3/pi_impl.py", [
        "_start_tls_wrapper",
        "print_connection_banner",
        "FORWARD_PORT_CANDIDATES",
    ]),
    ("README.md", [
        "Goal 5: Make the proxied page actually",
        "System updates made while building this",
        "Option A ",
    ]),
]

for path, patterns in CHECKS:
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": "probe", "Cache-Control": "no-cache"})
    text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    missing = [p for p in patterns if p not in text]
    status = "OK   " if not missing else "MISS "
    print(status, path, len(text), "chars",
          ("missing: " + str(missing)) if missing else "")
