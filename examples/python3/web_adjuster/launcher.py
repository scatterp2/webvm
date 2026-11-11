#!/usr/bin/env python3
"""Start an isolated Web Adjuster Kaggle iframe smoke test.

Ports:
  28080: small wrapper page with an iframe
  28081: Web Adjuster, configured for Kaggle

This is intentionally separate from the Kaggle RX/TX controller and does not
read or modify any configuration or credential files.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIND = "0.0.0.0"
PUBLIC_IP = "100.92.188.123"
WRAPPER_PORT = 28080
ADJUSTER_PORT = 28081
VENV = Path("/opt/web-adjuster-venv")
LOG_PATH = Path("/tmp/web-adjuster.log")


def log(message: str) -> None:
    print("[WEB_ADJUSTER] " + message, flush=True)


def ensure_adjuster() -> Path:
    executable = VENV / "bin" / "adjuster"
    if executable.exists():
        return executable

    if not (VENV / "bin" / "python").exists():
        log("INSTALL_STEP create_venv")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    python = VENV / "bin" / "python"
    log("INSTALL_STEP pip_install_adjuster")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "adjuster"],
        check=True,
        timeout=300,
    )
    if not executable.exists():
        found = shutil.which("adjuster")
        if found:
            return Path(found)
        raise RuntimeError("pip install completed but no adjuster console script was created")
    log("INSTALL_STEP complete")
    return executable


def start_adjuster() -> subprocess.Popen:
    executable = ensure_adjuster()
    command = [
        str(executable),
        "--address=" + BIND,
        "--port=" + str(ADJUSTER_PORT),
        "--host-suffix=" + PUBLIC_IP,
        "--wildcard-dns=False",
        "--default-site=www.kaggle.com",
        "--viewsource=True",
    ]
    log("STARTING_PROXY port={} target=www.kaggle.com".format(ADJUSTER_PORT))
    return subprocess.Popen(command, stdin=subprocess.DEVNULL)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Adjuster / Kaggle iframe smoke test</title>
<style>
body{font:16px system-ui,sans-serif;margin:0;background:#111827;color:#f9fafb}
header{padding:12px 16px;background:#1f2937}
p{margin:6px 0}
a{color:#93c5fd}
#status{font-size:13px;color:#d1d5db}
iframe{display:block;width:100%;height:calc(100vh - 112px);border:0;background:white}
</style>
</head>
<body>
<header>
<strong>Web Adjuster → Kaggle iframe test</strong>
<p>Proxy: <a href="http://100.92.188.123:28081/" target="_blank" rel="noopener">open Web Adjuster directly</a></p>
<p id="status">Waiting for iframe load event. A load event does not prove the page's scripts or authentication worked.</p>
</header>
<iframe id="kaggle" title="Kaggle through Web Adjuster" src="http://100.92.188.123:28081/"></iframe>
<script>
const frame = document.getElementById('kaggle');
frame.addEventListener('load', () => {
  document.getElementById('status').textContent =
    'Iframe load event fired. Check the frame visually; CSP/X-Frame-Options, redirects, or missing cross-domain assets may still prevent Kaggle from working.';
});
frame.addEventListener('error', () => {
  document.getElementById('status').textContent = 'Iframe error event fired.';
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html", "/health"):
            self.send_error(404)
            return
        body = b"ok\n" if self.path == "/health" else PAGE.encode("utf-8")
        content_type = "text/plain; charset=utf-8" if self.path == "/health" else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log("WRAPPER_HTTP " + (fmt % args))


def main() -> None:
    log("BOOT bind={} wrapper_port={} adjuster_port={}".format(BIND, WRAPPER_PORT, ADJUSTER_PORT))
    adjuster = start_adjuster()
    time.sleep(2)
    if adjuster.poll() is not None:
        raise RuntimeError("Web Adjuster exited during startup with code {}".format(adjuster.returncode))
    server = ThreadingHTTPServer((BIND, WRAPPER_PORT), Handler)
    log("WRAPPER_READY url=http://{}:{}/".format(PUBLIC_IP, WRAPPER_PORT))
    log("PROXY_EXPECTED url=http://{}:{}/".format(PUBLIC_IP, ADJUSTER_PORT))
    log("IFRAME_TEST_READY")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if adjuster.poll() is None:
            adjuster.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL {}: {}".format(type(exc).__name__, str(exc)[:500]))
        raise
