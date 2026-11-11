#!/usr/bin/env python3
"""Discover the live Kaggle Jupyter proxy route for the WebVM tokenized marker send.

The token is written only to /kaggle/working/webvm_proxy_route.json, which is
returned as part of tx_response.json to the WebVM controller. It is never
printed into notebook output or diagnostic logs.
"""
import glob
import json
import re
from pathlib import Path

HOST = "kkb-production.jupyter-proxy.kaggle.net"
RESULT = Path("/kaggle/working/webvm_proxy_route.json")

def find_base_url():
    pids = [12] + [
        int(item.split("/")[2]) for item in glob.glob("/proc/[0-9]*/cmdline")
        if item.split("/")[2].isdigit() and int(item.split("/")[2]) != 12
    ]
    for pid in pids:
        try:
            args = [
                value.decode("utf-8", "replace")
                for value in Path("/proc/{}/cmdline".format(pid)).read_bytes().split(b"\0")
                if value
            ]
        except OSError:
            continue
        if not any("jupyter" in value.lower() or "notebook" in value.lower() for value in args):
            continue
        for index, value in enumerate(args):
            if value.startswith(("--NotebookApp.base_url=", "--ServerApp.base_url=")):
                return value.split("=", 1)[1]
            if value in ("--NotebookApp.base_url", "--ServerApp.base_url") and index + 1 < len(args):
                return args[index + 1]
    return None

base_url = find_base_url()
match = re.fullmatch(r"/k/([0-9]+)/([A-Za-z0-9._~-]+)/proxy/?", str(base_url or ""))
if not match:
    try:
        RESULT.unlink()
    except FileNotFoundError:
        pass
    print("WEBVM_PROXY_ROUTE_DISCOVERY found=false base_url_present={}".format(bool(base_url)), flush=True)
else:
    route = {
        "host": HOST,
        "path": base_url,
        "kernel_id": match.group(1),
        "token": match.group(2),
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(route, sort_keys=True) + "\n", encoding="utf-8")
    print("WEBVM_PROXY_ROUTE_DISCOVERY found=true kernel_id={} token_present=true".format(
        match.group(1)), flush=True)
