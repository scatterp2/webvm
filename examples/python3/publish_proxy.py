#!/usr/bin/env python3
"""Publish the WebVM's proxy over https with Tailscale Serve.

Tailscale Serve terminates https on the tailnet with a publicly trusted
certificate and forwards to a plain-http target **server side** — which is
the normal "https front, http upstream" reverse-proxy arrangement and is
not affected by the browser's mixed-content rules.

The target is the WebVM's proxy.  Because the WebVM's Tailscale address
changes on every boot, the target is normally the WebVM's **node name**,
which is stable when the node is registered with a tagged auth key
(`tag:webvm` -> `tag-webvm.tailf987e.ts.net`).  Without a tag the script
falls back to the live address and has to be re-run after each boot.

Usage:
    python publish_proxy.py                 # prefer a tagged node
    python publish_proxy.py --tag tag:webvm
    python publish_proxy.py --ip 100.x.x.x
    python publish_proxy.py --status        # show the current serve config
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

TAILSCALE = r"C:\Program Files\Tailscale\Tailscale.exe"
PROXY_PORT = 28082


def run(args):
    return subprocess.run([TAILSCALE] + args, capture_output=True, text=True,
                          timeout=60)


def status_json():
    result = run(["status", "--json"])
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def pick_node(tag=None, ip=None):
    """Return (dns_name, ip) of the WebVM node to publish."""
    data = status_json()
    suffix = data.get("MagicDNSSuffix", "")
    peers = (data.get("Peer") or {}).values()

    if ip:
        for peer in peers:
            if ip in (peer.get("TailscaleIPs") or []):
                return (peer.get("DNSName", "").rstrip("."), ip)
        return (None, ip)

    candidates = []
    for peer in peers:
        if not peer.get("Online"):
            continue
        tags = peer.get("Tags") or []
        host = peer.get("HostName") or ""
        dns = (peer.get("DNSName") or "").rstrip(".")
        ips = peer.get("TailscaleIPs") or []
        if tag:
            want = tag.split(":")[-1]
            if any(t.split(":")[-1] == want for t in tags):
                candidates.append((0, dns, ips[0] if ips else None))
        elif host.startswith("js"):
            candidates.append((1, dns, ips[0] if ips else None))
    if not candidates:
        return (None, None)
    candidates.sort()
    _, dns, address = candidates[0]
    return (dns, address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=None,
                        help="prefer the node carrying this tag")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--port", type=int, default=PROXY_PORT)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        result = run(["serve", "status"])
        print(result.stdout or result.stderr)
        return 0

    dns, address = pick_node(args.tag, args.ip)
    if not dns and not address:
        print("No live WebVM node found.", file=sys.stderr)
        return 1

    target = dns or address
    print("Publishing the WebVM proxy at {}:{} (node {})".format(
        target, args.port, dns or address))
    result = run(["serve", "--bg", "http://{}:{}".format(target, args.port)])
    output = (result.stdout or "") + (result.stderr or "")
    print(output.strip())
    if result.returncode != 0:
        if "Serve is not enabled" in output:
            print("\nServe has to be enabled on the tailnet first.  The link "
                  "above enables it for this node.", file=sys.stderr)
        return result.returncode

    data = status_json()
    suffix = data.get("MagicDNSSuffix", "")
    self_dns = (data.get("Self", {}).get("DNSName") or "").rstrip(".")
    print("\nThe proxy is now reachable at:")
    print("  https://{}/proxy/<url>".format(self_dns))
    print("and the WebVM page can use it as its proxy origin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
