#!/usr/bin/env python3
"""Remote probe executed through the WebVM -> Kaggle RX/TX command envelope.

This intentionally avoids changing firewall rules or touching Jupyter. It reports
whether Kaggle can reach the public network and the WebVM-side proxy endpoint.
"""
import json, os, socket, shutil, subprocess, sys, time
from urllib.request import urlopen, Request

def probe_host(host, port=443):
    row = {"host": host, "port": port}
    try:
        addrs = sorted({x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        row["dns"] = True
        row["address_count"] = len(addrs)
    except Exception as exc:
        row.update(dns=False, error=type(exc).__name__ + ": " + str(exc)[:180])
        return row
    try:
        with socket.create_connection((host, port), timeout=4):
            row["tcp"] = True
    except Exception as exc:
        row.update(tcp=False, tcp_error=type(exc).__name__ + ": " + str(exc)[:180])
        return row
    row["tcp"] = True
    return row

report = {
    "probe": "webvm-rx-tx-remote-probe-v1",
    "time": time.time(),
    "hostname": socket.gethostname(),
    "uid": os.geteuid() if hasattr(os, "geteuid") else None,
    "network": [probe_host(h) for h in (
        "raw.githubusercontent.com", "api.kaggle.com",
        "kkb-production.jupyter-proxy.kaggle.net", "pste.us"
    )],
    "jupyter_8888_untouched": True,
    "firewall_changed": False,
}
try:
    req = Request("https://example.com/", headers={"User-Agent": "webvm-rx-tx-probe/1"})
    with urlopen(req, timeout=6) as r:
        report["https_example"] = {"ok": True, "status": r.status, "bytes": len(r.read(256))}
except Exception as exc:
    report["https_example"] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}
print("REMOTE_PROBE_RESULT " + json.dumps(report, sort_keys=True), flush=True)
