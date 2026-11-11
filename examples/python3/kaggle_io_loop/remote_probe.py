#!/usr/bin/env python3
"""Find the Kaggle proxy token using the notebook's full process listing.

The live process table is searched with `ps auxww`, as requested. The
matching token is written only to /kaggle/working/webvm_proxy_route.json
for return through the existing RX/TX channel. It is never printed.
"""
import json
import re
import subprocess
from pathlib import Path

HOST = "kkb-production.jupyter-proxy.kaggle.net"
RESULT = Path("/kaggle/working/webvm_proxy_route.json")
ROUTE_RE = re.compile(r"/k/([0-9]+)/([^/\s'\"?]+)/proxy(?:/|$)")

try:
    ps = subprocess.run(
        ["ps", "auxww"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    process_lines = ps.stdout.splitlines()
except (OSError, subprocess.SubprocessError) as exc:
    try:
        RESULT.unlink()
    except FileNotFoundError:
        pass
    print(
        "WEBVM_PROXY_ROUTE_DISCOVERY found=false method=ps_auxww "
        "error_type={}".format(type(exc).__name__),
        flush=True,
    )
    raise SystemExit(0)

jupyter_lines = [
    line for line in process_lines
    if "jupyter" in line.lower() or "notebook" in line.lower()
]
search_groups = (jupyter_lines, process_lines)
found = None
source_group = None

# Search the entire ps command line, prioritising Jupyter/Notebook processes.
for group_name, lines in (("jupyter_process", jupyter_lines), ("all_processes", process_lines)):
    for line in lines:
        match = ROUTE_RE.search(line)
        if match:
            found = {
                "host": HOST,
                "path": "/k/{}/{}/proxy".format(match.group(1), match.group(2)),
                "kernel_id": match.group(1),
                "token": match.group(2),
            }
            source_group = group_name
            break
    if found:
        break

if not found:
    try:
        RESULT.unlink()
    except FileNotFoundError:
        pass
    print(
        "WEBVM_PROXY_ROUTE_DISCOVERY found=false method=ps_auxww "
        "process_count={} jupyter_process_count={}".format(
            max(0, len(process_lines) - 1), len(jupyter_lines)),
        flush=True,
    )
else:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(found, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "WEBVM_PROXY_ROUTE_DISCOVERY found=true method=ps_auxww "
        "source={} kernel_id={} token_present=true".format(
            source_group, found["kernel_id"]),
        flush=True,
    )
