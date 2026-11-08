#!/usr/bin/env python3
"""Bounded network probe executed through the WebVM -> Kaggle RX/TX envelope.

Every DNS/connection check runs in a child process with a hard wall-clock limit,
so a resolver stall cannot block the RX/TX acknowledgement. This does not change
firewall rules or touch Jupyter.
"""
import json, os, platform, shutil, subprocess, sys, time

PROBE_CODE = r"""
import json, socket, sys
host, port = sys.argv[1], int(sys.argv[2])
addresses = sorted({item[4][0] for item in socket.getaddrinfo(
    host, port, type=socket.SOCK_STREAM
)})
with socket.create_connection((host, port), timeout=2):
    pass
print(json.dumps({"dns": True, "address_count": len(addresses), "tcp": True}))
"""

HTTPS_CODE = r"""
import json
from urllib.request import urlopen, Request
req = Request("https://example.com/", headers={"User-Agent": "webvm-rx-tx-probe/1"})
with urlopen(req, timeout=3) as response:
    print(json.dumps({"ok": True, "status": response.status,
                      "bytes": len(response.read(256))}))
"""

def run_bounded(code, args, timeout):
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, *args],
            capture_output=True, text=True, timeout=timeout, check=False
        )
        if result.returncode != 0:
            return {"ok": False, "error": "ProbeFailed",
                    "detail": (result.stderr or "")[-180:]}
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": "InvalidProbeOutput"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ProbeTimeout"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:160]}

def probe_host(host, port=443):
    result = run_bounded(PROBE_CODE, [host, str(port)], 5)
    row = {"host": host, "port": port}
    if result.get("ok") is False:
        row.update(dns_or_tcp=False, error=result.get("error", "ProbeFailed"))
        if result.get("detail"):
            row["detail"] = result["detail"]
    else:
        row.update(result)
    return row

def bounded_command(argv, timeout=3):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-1200:],
            "stderr": (p.stderr or "")[-600:],
        }
    except Exception as exc:
        return {"error": type(exc).__name__}

def read_small(path, limit=3000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception as exc:
        return {"error": type(exc).__name__}

def netfilter_diagnostics():
    commands = {}
    for name in ("iptables", "iptables-nft", "iptables-legacy", "nft", "apt-get", "dpkg", "busybox"):
        commands[name] = shutil.which(name)
    for path in ("/usr/sbin/iptables", "/sbin/iptables", "/usr/bin/iptables",
                 "/usr/sbin/iptables-nft", "/usr/sbin/iptables-legacy",
                 "/usr/sbin/nft", "/sbin/nft"):
        if os.path.exists(path):
            commands["exists:" + path] = True
    proc_names = read_small("/proc/net/ip_tables_names", 1000)
    module_names = read_small("/proc/modules", 8000)
    module_matches = []
    if isinstance(module_names, str):
        module_matches = [line.split()[0] for line in module_names.splitlines()
                          if any(k in line.lower() for k in
                                 ("ip_tables", "iptable_nat", "nf_nat", "nf_tables", "nft_chain"))]
    apt_cache = bounded_command(["apt-cache", "policy", "iptables"], 3) if commands.get("apt-get") else {"note": "apt-get not found"}
    dpkg_arch = bounded_command(["dpkg", "--print-architecture"], 3) if commands.get("dpkg") else {"note": "dpkg not found"}
    uname = bounded_command(["uname", "-a"], 3)
    return {
        "architecture": platform.machine(),
        "kernel": uname,
        "commands": commands,
        "dpkg_architecture": dpkg_arch,
        "apt_policy_iptables": apt_cache,
        "proc_ip_tables_names": proc_names,
        "netfilter_modules_loaded": module_matches,
        "config_netfilter": {
            "proc_config_gz": "present" if os.path.exists("/proc/config.gz") else "absent",
            "boot_config": "present" if os.path.exists("/boot/config-" + os.uname().release) else "absent",
        },
    }

report = {
    "probe": "webvm-rx-tx-remote-probe-v3",
    "time": time.time(),
    "hostname": os.uname().nodename if hasattr(os, "uname") else None,
    "uid": os.geteuid() if hasattr(os, "geteuid") else None,
    "network": [probe_host(host) for host in (
        "raw.githubusercontent.com", "api.kaggle.com",
        "kkb-production.jupyter-proxy.kaggle.net", "pste.us"
    )],
    "jupyter_8888_untouched": True,
    "firewall_changed": False,
}
report["https_example"] = run_bounded(HTTPS_CODE, [], 7)
report["netfilter_diagnostics"] = netfilter_diagnostics()
print("REMOTE_PROBE_RESULT " + json.dumps(report, sort_keys=True), flush=True)
