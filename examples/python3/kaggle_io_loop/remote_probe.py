#!/usr/bin/env python3
"""Discover the live Kaggle Jupyter proxy route from the full process list.

Equivalent to inspecting `ps auxww`: scan every readable process command line,
not only explicit --ServerApp.base_url / --NotebookApp.base_url arguments.
Only the tokenized route is written to /kaggle/working/webvm_proxy_route.json
for return through the existing RX/TX channel. Never print the token.
"""
import glob
import json
import re
from pathlib import Path

HOST = "kkb-production.jupyter-proxy.kaggle.net"
RESULT = Path("/kaggle/working/webvm_proxy_route.json")
ROUTE_RE = re.compile(r"(/k/([0-9]+)/([A-Za-z0-9._~-]+)/proxy/?)(?=$|[\s'\\";,])")

def process_cmdlines():
    """Yield (pid, argv) for every readable process, like ps auxww."""
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(cmdline_path.split("/")[2])
            raw = Path(cmdline_path).read_bytes()
        except (OSError, ValueError):
            continue
        args = [
            value.decode("utf-8", "replace")
            for value in raw.split(b"\0") if value
        ]
        if args:
            yield pid, args

process_count = 0
jupyter_count = 0
found = None
for pid, args in process_cmdlines():
    process_count += 1
    joined = " ".join(args)
    is_jupyter = "jupyter" in joined.lower() or "notebook" in joined.lower()
    if is_jupyter:
        jupyter_count += 1
    # Search the entire argv text, not just the value of a base_url option.
    # Prefer a match on a Jupyter/Notebook process, but retain any match as
    # fallback because wrappers can carry the route in their own arguments.
    matches = list(ROUTE_RE.finditer(joined))
    if matches and (is_jupyter or found is None):
        match = matches[0]
        found = {
            "host": HOST,
            "path": match.group(1).rstrip("/"),
            "kernel_id": match.group(2),
            "token": match.group(3),
            "source_pid": pid,
            "source_is_jupyter": is_jupyter,
        }
        if is_jupyter:
            break

if not found:
    try:
        RESULT.unlink()
    except FileNotFoundError:
        pass
    print(
        "WEBVM_PROXY_ROUTE_DISCOVERY found=false process_count={} jupyter_process_count={} "
        "searched=full_cmdline".format(process_count, jupyter_count),
        flush=True,
    )
else:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(found, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "WEBVM_PROXY_ROUTE_DISCOVERY found=true source_pid={} source_is_jupyter={} "
        "kernel_id={} token_present=true searched=full_cmdline".format(
            found["source_pid"], str(found["source_is_jupyter"]).lower(),
            found["kernel_id"]),
        flush=True,
    )
