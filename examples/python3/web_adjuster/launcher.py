#!/usr/bin/env python3
"""Start an isolated Web Adjuster proxy iframe smoke test.

Ports:
  28080: small wrapper page with an iframe
  28081: Web Adjuster, generic proxy

This is intentionally separate from the Kaggle RX/TX controller and does not
read or modify any configuration or credential files.

The wrapper binds immediately and answers /health before the adjuster exists.
The adjuster is installed and started lazily, on the first page request, so a
slow or failed package install can never stop the wrapper from listening.

The public host is taken from the request's Host header: the WebVM has no
usable OS tools (ip, ps, hostname -I, /proc/net/*) to discover it, and the
Tailscale address is not known until the node is online.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIND = "0.0.0.0"
WRAPPER_PORT = 28080
ADJUSTER_PORT = 28081
VENV = Path("/opt/web-adjuster-venv")
LOG_PATH = Path("/tmp/web-adjuster.log")

STATE_LOCK = threading.Lock()
_start_lock = threading.Lock()
_adjuster_process = None
_adjuster_host = None
_adjuster_state = "idle"
_adjuster_error = ""
_adjuster_thread = None


def log(message: str) -> None:
    print("[WEB_ADJUSTER] " + message, flush=True)


def set_state(state: str, error: str = "") -> None:
    global _adjuster_state, _adjuster_error
    with STATE_LOCK:
        _adjuster_state = state
        _adjuster_error = error


def _has_pip(python: Path) -> bool:
    try:
        return subprocess.run(
            [str(python), "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_adjuster() -> Path:
    executable = VENV / "bin" / "adjuster"
    if executable.exists():
        return executable

    found = shutil.which("adjuster")
    if found:
        log("INSTALL_STEP already_on_path path={}".format(found))
        return Path(found)

    # Debian's Python ships without ensurepip, so a plain `venv` cannot
    # bootstrap pip. Create the environment without pip; --system-site-packages
    # lets it see packages installed for the system interpreter.
    if not (VENV / "bin" / "python").exists():
        log("INSTALL_STEP create_venv")
        subprocess.run(
            [
                sys.executable, "-m", "venv",
                "--without-pip", "--system-site-packages",
                str(VENV),
            ],
            check=True,
        )

    venv_python = VENV / "bin" / "python"
    attempts = []
    if _has_pip(venv_python):
        attempts.append([
            str(venv_python), "-m", "pip", "install",
            "--disable-pip-version-check", "adjuster",
        ])
        attempts.append([
            str(venv_python), "-m", "pip", "install",
            "--disable-pip-version-check", "--break-system-packages", "adjuster",
        ])
    system_pip = shutil.which("pip3") or shutil.which("pip")
    if system_pip:
        attempts.append([
            system_pip, "install",
            "--disable-pip-version-check", "--break-system-packages", "adjuster",
        ])

    if not attempts:
        raise RuntimeError("No usable pip was found to install adjuster")

    installed = False
    for command in attempts:
        log("INSTALL_STEP try " + " ".join(command))
        try:
            result = subprocess.run(command, check=False, timeout=600)
        except subprocess.TimeoutExpired:
            log("INSTALL_STEP timeout")
            continue
        if result.returncode == 0:
            installed = True
            break
        log("INSTALL_STEP failed exit_code={}".format(result.returncode))

    if not installed:
        raise RuntimeError("pip install adjuster failed for every attempt")

    if executable.exists():
        log("INSTALL_STEP complete path={}".format(executable))
        return executable
    found = shutil.which("adjuster")
    if found:
        log("INSTALL_STEP complete path={}".format(found))
        return Path(found)
    raise RuntimeError("adjuster installed but no console script was created")


# Appended to the end of every HTML BODY the adjuster serves. It proves that
# the proxy rewrites and injects JavaScript that then executes in the browser.
# The text is deliberately free of spaces so that it survives command-line
# argument handling on every platform.
INJECTION_SCRIPT = (
    "<script>(function(){try{"
    "document.documentElement.setAttribute('data-webvm-adjuster-injected','true');"
    "(document.body||document.documentElement).appendChild("
    "Object.assign(document.createElement('div'),{"
    "id:'webvm-adjuster-injection-proof',"
    "'data-webvm-injected':'true',"
    "textContent:'WEBVM_ADJUSTER_INJECTED',"
    "style:'position:fixed;top:0;left:0;z-index:2147483647;"
    "background:#013220;color:#7dffb0;font:12px;padding:3px;'}));"
    "}catch(e){"
    "document.documentElement.setAttribute('data-webvm-adjuster-injection-error',String(e));"
    "}})();"
    "</script>"
)


def start_adjuster(host: str) -> None:
    """Install (if needed) and start the adjuster bound to the request host."""
    global _adjuster_process, _adjuster_host

    with STATE_LOCK:
        existing = _adjuster_process
        if existing is not None and existing.poll() is None:
            if _adjuster_host == host:
                return
            # The public address changed: replace the running instance.
            log("ADJUSTER_HOST_CHANGED old={} new={}".format(_adjuster_host, host))
            try:
                existing.terminate()
                existing.wait(timeout=5)
            except Exception:
                try:
                    existing.kill()
                except Exception:
                    pass
            _adjuster_process = None
        _adjuster_host = host

    set_state("installing")
    try:
        executable = ensure_adjuster()
    except Exception as exc:
        set_state("failed", "{}: {}".format(type(exc).__name__, str(exc)[:300]))
        log("ADJUSTER_INSTALL_FAILED error={}".format(_adjuster_error))
        return

    set_state("starting")
    command = [
        str(executable),
        "--address=" + BIND,
        "--port=" + str(ADJUSTER_PORT),
        "--host-suffix=" + host,
        "--default-site=www.kaggle.com",
        "--wildcard-dns=False",
        "--open-proxy",
        "--real-proxy",
        "--bodyAppend=" + INJECTION_SCRIPT,
    ]
    log("STARTING_PROXY port={} host={}".format(ADJUSTER_PORT, host))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        set_state("failed", "{}: {}".format(type(exc).__name__, str(exc)[:300]))
        log("ADJUSTER_START_FAILED error={}".format(_adjuster_error))
        return
    with STATE_LOCK:
        _adjuster_process = process

    # Wait briefly for the listening socket to appear.
    for _ in range(40):
        if process.poll() is not None:
            set_state(
                "failed",
                "adjuster exited with code {}".format(process.returncode),
            )
            log("ADJUSTER_EXITED code={}".format(process.returncode))
            return
        if proxy_reachable():
            set_state("ready")
            log("PROXY_READY url=http://{}:{}/".format(host, ADJUSTER_PORT))
            return
        time.sleep(0.5)

    set_state("starting", "adjuster did not report a listening socket in time")
    log("ADJUSTER_START_TIMEOUT")


def proxy_reachable() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", ADJUSTER_PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_adjuster_started(host: str) -> None:
    """Kick off the lazy adjuster start once per host, without blocking."""
    global _adjuster_thread
    # A separate lock serialises starts; STATE_LOCK is not held while the
    # install runs, so it cannot be used for that purpose.
    with _start_lock:
        thread = _adjuster_thread
        if thread is not None and thread.is_alive():
            return
        with STATE_LOCK:
            if _adjuster_state == "ready":
                return
        thread = threading.Thread(target=start_adjuster, args=(host,), daemon=True)
        _adjuster_thread = thread
        thread.start()


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Adjuster / Proxy iframe test</title>
<style>
body{font:16px system-ui,sans-serif;margin:0;background:#111827;color:#f9fafb}
header{padding:12px 16px;background:#1f2937}
p{margin:6px 0}
a{color:#93c5fd}
#status{font-size:13px;color:#d1d5db}
iframe{display:block;width:100%;height:calc(100vh - 140px);border:0;background:white}
</style>
</head>
<body>
<header>
<strong>Web Adjuster &rarr; Proxy iframe test</strong>
<p>Direct proxy: <a id="adjuster-link" href="#" target="_blank" rel="noopener">open Web Adjuster (port __ADJUSTER_PORT__)</a>
 | Same-origin proxy: <a id="wrapper-proxy-link" href="/proxy/" target="_blank" rel="noopener">/proxy/</a></p>
<p id="status">Waiting for the adjuster to come up...</p>
</header>
<iframe id="proxy-frame" title="Kaggle through Web Adjuster (same-origin proxy)"></iframe>
<script>
(function() {
  var status = document.getElementById('status');
  var frame = document.getElementById('proxy-frame');
  var host = window.location.hostname;
  var directUrl = 'http://' + host + ':' + __ADJUSTER_PORT__ + '/';
  var proxyUrl = '/proxy/';
  document.getElementById('adjuster-link').href = directUrl;
  document.getElementById('wrapper-proxy-link').href = proxyUrl;

  var loaded = false;
  frame.addEventListener('load', function() {
    loaded = true;
    status.textContent = 'Iframe load event fired at ' + new Date().toISOString()
      + ' - checking for injected marker...';
    window.setTimeout(checkInjection, 500);
  });
  frame.addEventListener('error', function() {
    status.textContent = 'Iframe error event fired at ' + new Date().toISOString();
  });

  function poll() {
    fetch('/proxy-status?t=' + Date.now(), {cache: 'no-store'})
      .then(function(r) { return r.json(); })
      .then(function(s) {
        if (s.state === 'ready') {
          if (!loaded) { frame.src = proxyUrl; }
          status.textContent = 'Proxy ready - loading Kaggle through same-origin /proxy/ endpoint...';
          return;
        }
        status.textContent = 'Adjuster state: ' + s.state
          + (s.error ? ' (' + s.error + ')' : '') + ' - retrying...';
        window.setTimeout(poll, 1500);
      })
      .catch(function(e) {
        status.textContent = 'Status poll failed: ' + e;
        window.setTimeout(poll, 1500);
      });
  }

  function checkInjection() {
    try {
      var doc = frame.contentDocument;
      if (!doc) {
        status.textContent += ' (contentDocument is null - cross-origin or about:blank)';
        return;
      }
      var el = doc.getElementById('webvm-adjuster-injection-proof');
      var attr = doc.documentElement.getAttribute('data-webvm-adjuster-injected');
      if (el || attr === 'true') {
        status.textContent = 'JS/DOM INJECTION CONFIRMED: marker=' + (!!el)
          + ', attr=' + attr + ', text=' + (el ? el.textContent : 'n/a');
      } else {
        status.textContent += ' (no injection marker found yet - page may still be loading)';
      }
    } catch (e) {
      status.textContent = 'Injection check error: ' + e;
    }
  }

  poll();
})();
</script>
</body>
</html>
"""


def render_page() -> str:
    return PAGE_TEMPLATE.replace("__ADJUSTER_PORT__", str(ADJUSTER_PORT))


FRAME_BLOCKED_HEADERS = frozenset(("x-frame-options",))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reverse_proxy(self, downstream_path: str) -> None:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError

        target = "http://127.0.0.1:{}/{}".format(
            ADJUSTER_PORT, downstream_path.lstrip("/"))
        try:
            req = Request(target)
            for name in ("Accept", "Accept-Language", "Accept-Encoding",
                         "Cookie", "Referer", "User-Agent"):
                val = self.headers.get(name)
                if val:
                    req.add_header(name, val)
            with urlopen(req, timeout=30) as resp:
                body = resp.read(4 * 1024 * 1024)
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    lower = key.lower()
                    if lower in FRAME_BLOCKED_HEADERS:
                        continue
                    if lower == "content-security-policy" and "frame-ancestors" in val.lower():
                        continue
                    if lower in ("content-length", "transfer-encoding",
                                 "connection", "keep-alive"):
                        continue
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except HTTPError as exc:
            body = "Proxy upstream returned {}\n".format(exc.code).encode()
            self._send(exc.code, "text/plain; charset=utf-8", body)
        except (URLError, OSError) as exc:
            msg = "Proxy fetch failed: {}\n".format(exc).encode()
            self._send(502, "text/plain; charset=utf-8", msg)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/proxy-status":
            with STATE_LOCK:
                payload = {
                    "state": _adjuster_state,
                    "error": _adjuster_error,
                    "host": _adjuster_host,
                    "proxy_port": ADJUSTER_PORT,
                    "reachable": proxy_reachable(),
                }
            body = json.dumps(payload).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path.startswith("/proxy/") or path == "/proxy":
            downstream = path[6:] or "/"
            host = self.headers.get("Host", "")
            host = host.split(":")[0] or "localhost"
            ensure_adjuster_started(host)
            self._reverse_proxy(downstream)
            return
        if path in ("/", "/index.html"):
            host = self.headers.get("Host", "")
            host = host.split(":")[0] or "localhost"
            ensure_adjuster_started(host)
            body = render_page().encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        self._send(404, "text/plain; charset=utf-8", b"Not Found\n")

    def log_message(self, fmt, *args):
        log("WRAPPER_HTTP " + (fmt % args))


class WrapperServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers routinely close keep-alive connections early; that is not
        # an error worth printing into the launcher log.
        import sys as _sys

        exc_type = _sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def main() -> None:
    log("BOOT bind={} wrapper_port={} adjuster_port={}".format(BIND, WRAPPER_PORT, ADJUSTER_PORT))

    server = WrapperServer((BIND, WRAPPER_PORT), Handler)
    server.daemon_threads = True
    log("WRAPPER_READY url=http://{}:{}/".format(BIND, WRAPPER_PORT))
    log("IFRAME_TEST_READY")

    def _shutdown(signum, frame):
        log("SHUTDOWN signal {}".format(signum))
        threading.Thread(target=server.shutdown, daemon=True).start()

    import signal

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        with STATE_LOCK:
            process = _adjuster_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL {}: {}".format(type(exc).__name__, str(exc)[:500]))
        raise
