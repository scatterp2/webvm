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
WRAPPER_PORT = 28080
ADJUSTER_PORT = 28081
VENV = Path("/opt/web-adjuster-venv")
LOG_PATH = Path("/tmp/web-adjuster.log")
_adjuster_process = None


def log(message: str) -> None:
    print("[WEB_ADJUSTER] " + message, flush=True)


def ensure_adjuster() -> Path:
    executable = VENV / "bin" / "adjuster"
    if executable.exists():
        return executable

    # Use pip via system package manager instead of ensurepip (unreliable in WebVM)
    if not (VENV / "bin" / "python").exists():
        log("INSTALL_STEP create_venv")
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV), "--without-pip", "--system-site-packages"],
            check=True,
        )

    python = VENV / "bin" / "python"
    log("INSTALL_STEP pip_install_adjuster")
    # Try pip from venv first; fall back to system pip
    pip_cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "adjuster"]
    result = subprocess.run(
        pip_cmd,
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
    global _adjuster_process
    if _adjuster_process is not None and _adjuster_process.poll() is None:
        return _adjuster_process
    executable = ensure_adjuster()
    command = [
        str(executable),
        "--address=" + BIND,
        "--port=" + str(ADJUSTER_PORT),
        "--host-suffix=_",
        "--wildcard-dns=False",
        "--default-site=www.kaggle.com",
        "--viewsource=True",
        "--open-proxy",
    ]
    log("STARTING_PROXY port={} target=www.kaggle.com".format(ADJUSTER_PORT))
    _adjuster_process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    return _adjuster_process


PAGE_WITH_IFRAME = """<!doctype html>
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
<p>Proxy: <a id="adjuster-link" href="#">open Web Adjuster directly</a></p>
<p id="status">Waiting for iframe load event.</p>
</header>
<iframe id="kaggle" title="Kaggle through Web Adjuster" src=""></iframe>
<script>
(function() {
  var status = document.getElementById('status');
  var frame = document.getElementById('kaggle');
  var link = document.getElementById('adjuster-link');
  var host = window.location.host; // e.g. 100.92.188.123:28080
  var adjusterUrl = 'http://' + host.replace(/:\\d+$/, '') + ':' + {ADJUSTER_PORT} + '/';
  link.href = adjusterUrl;
  frame.src = adjusterUrl;

  frame.addEventListener('load', function() {
    status.textContent = 'Iframe load event fired. Check the frame visually; CSP/X-Frame-Options, redirects, or missing cross-domain assets may still prevent Kaggle from working.';
  });
  frame.addEventListener('error', function() {
    status.textContent = 'Iframe error event fired.';
  });
})();
</script>
</body>
</html>
"""

WRAPPER_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Adjuster / Wrapper</title>
</head>
<body>
<h1>Wrapper page - adjuster not yet installed</h1>
<p>Use /health for health check. Navigate to the iframe page to start the adjuster.</p>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if self.path in ("/", "/index.html"):
            # Derive host from Host header
            host = self.headers.get("Host", "localhost")
            page = PAGE_WITH_IFRAME.replace("{ADJUSTER_PORT}", str(ADJUSTER_PORT))
            self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not Found\n")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log("WRAPPER_HTTP " + (fmt % args))


def main() -> None:
    global _adjuster_process
    log("BOOT bind={} wrapper_port={} adjuster_port={}".format(BIND, WRAPPER_PORT, ADJUSTER_PORT))

    # Start wrapper server FIRST (before adjuster), so we can lazily start adjuster
    server = ThreadingHTTPServer((BIND, WRAPPER_PORT), Handler)
    log("WRAPPER_READY url=http://{}:{}/".format(BIND, WRAPPER_PORT))
    log("IFRAME_TEST_READY")

    def _shutdown(signum, frame):
        log("SHUTDOWN signal {}".format(signum))
        server.shutdown()
        if _adjuster_process is not None and _adjuster_process.poll() is None:
            _adjuster_process.terminate()
        server.server_close()

    import signal
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        if _adjuster_process is not None and _adjuster_process.poll() is None:
            _adjuster_process.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL {}: {}".format(type(exc).__name__, str(exc)[:500]))
        raise
