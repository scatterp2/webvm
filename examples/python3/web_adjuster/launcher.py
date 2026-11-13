#!/usr/bin/env python3
"""In-process HTTP proxy for WebVM / CheerpX.

Two listeners, both served by plain sockets + threads:

  :28080  wrapper page (HTML with an iframe) + control endpoints
  :28081  forward proxy (absolute-URI requests)

Why this is hand-rolled instead of using http.server / socketserver:

  * CheerpX misbehaves with socketserver's BaseHTTPRequestHandler.  The
    response header write failed with
    ``OSError: [Errno 107] Transport endpoint is not connected`` and the
    client never received a reply.  ``telnet_shell.py`` already proved that
    raw sockets plus a plain accept loop work reliably inside WebVM, so the
    proxy uses the same shape.

  * The previous design shelled out to the ``adjuster`` pip package and ran
    it as a subprocess on :28081.  CheerpX starves long-lived subprocesses,
    so that port never accepted a connection, and installing the package at
    boot was fragile.  The proxy below is pure Python and runs inside the
    WebVM process, so there is nothing to install and nothing to starve.

The proxy is generic: it forwards any absolute http/https URL.

Endpoints on the wrapper port:
    GET /health          -> "ok"
    GET /proxy-status    -> JSON state
    GET /                -> HTML page containing the proxy iframe
    GET /proxy/<url>     -> fetch <url> through the proxy
    GET /?target=<url>   -> iframe the given target (default: Kaggle)

Header handling on proxied responses:
    * ``x-frame-options`` is removed so the page can be framed
    * ``frame-ancestors`` is removed from ``content-security-policy``
    * ``Set-Cookie`` loses ``Domain``/``Secure`` so cookies stick to the
      proxy origin
    * absolute URLs in HTML bodies are rewritten to go back through the proxy
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

BIND = "0.0.0.0"
WRAPPER_PORT = 28080
ADJUSTER_PORT = 28081

# The wrapper is served on this port by pi_impl.py when it runs the server
# as an in-process thread (CheerpX cannot keep a subprocess server alive).
WRAPPER_PORT_INPROC = 28082

DEFAULT_TARGET = "https://www.kaggle.com/"

MAX_BODY = 12 * 1024 * 1024
MAX_HEADER_LINES = 200

# Appended to every HTML body the proxy serves.  It proves that the proxy
# rewrites and injects JavaScript that then executes in the browser.
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

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
))

# Headers that reveal the proxy/iframe context to upstream servers.
# Stripping these prevents bot-detection systems (like Kaggle's) from
# seeing that the request originates from an iframe inside a proxy.
PROXY_REVEAL_HEADERS = frozenset((
    "origin", "referer", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-fetch-user", "sec-ch-ua-platform",
    "sec-ch-ua-mobile", "sec-ch-ua",
))

FRAME_BLOCKED_HEADERS = frozenset(("x-frame-options",))

# File extensions that should be redirected to the CDN instead of proxied.
_STATIC_EXTS = frozenset((
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".map",
    ".webp", ".avif", ".mp4", ".webm",
))

STATE_LOCK = threading.Lock()
_state = {
    "state": "ready",
    "error": "",
    "host": "",
    "mode": "in-process",
    "requests": 0,
    "upstream_errors": 0,
}


def log(message: str) -> None:
    print("[WEB_ADJUSTER] " + message, flush=True)


def set_state(**kwargs) -> None:
    with STATE_LOCK:
        _state.update(kwargs)


def bump(key: str) -> None:
    with STATE_LOCK:
        _state[key] = int(_state.get(key, 0)) + 1


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def proxy_path(url: str, proxy_origin=None) -> str:
    """Encode an absolute URL so it can travel as a single path segment.

    ``proxy_origin`` makes the result an absolute URL.  That is required
    when the proxied HTML is injected into an iframe with ``srcdoc``: a
    relative ``/proxy/...`` URL would then resolve against the *host
    page's* origin instead of the proxy's.
    """
    path = "/proxy/" + quote(url, safe="")
    if proxy_origin:
        return proxy_origin.rstrip("/") + path
    return path


def target_from_path(path: str):
    """Decode /proxy/<encoded-url> back into an absolute URL."""
    raw = path[len("/proxy"):]
    raw = raw.lstrip("/")
    if not raw:
        return None
    return unquote(raw)


def absolutize(url: str, base: str) -> str:
    """Resolve a possibly relative URL against the page it was found in."""
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)


# --------------------------------------------------------------------------
# HTML rewriting
# --------------------------------------------------------------------------

_ABS_URL_RE = re.compile(
    r"""(?P<pre>[\s'"(=])(?P<url>(?:https?:)?//[^\s'"<>()]+)""", re.IGNORECASE
)
_CSS_URL_RE = re.compile(
    r"""url\(\s*(?P<q>['"]?)(?P<url>(?:https?:)?//[^)'"\s]+)(?P=q)\s*\)""",
    re.IGNORECASE,
)
_SRCSET_RE = re.compile(r"""(?P<pre>\bsrcset\s*=\s*)(?P<q>["'])(?P<val>[^"']*)(?P=q)""",
                         re.IGNORECASE)

# Matches src, href, action, poster, data attributes with ANY URL value
# (including relative).  Applied AFTER _ABS_URL_RE so already-rewritten
# absolute proxy URLs are left alone.
_REL_ATTR_RE = re.compile(
    r"""(?P<pre>\b(?:src|href|action|poster|data)\s*=\s*)"""
    r"""(?P<q>["'])(?P<url>[^"']*)(?P=q)""",
    re.IGNORECASE,
)
# Matches CSS url() with relative paths (not already absolute or data:).
_REL_CSS_URL_RE = re.compile(
    r"""url\(\s*(?P<q>['"]?)(?P<url>[^)'"\s]+)(?P=q)\s*\)""",
    re.IGNORECASE,
)


def _rewrite_srcset(value: str, base: str, proxy_origin=None) -> str:
    parts = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split(None, 1)
        bits[0] = _rewrite_url(bits[0], base, proxy_origin)
        parts.append(" ".join(bits))
    return ", ".join(parts)


def _rewrite_url(url: str, base: str, proxy_origin=None) -> str:
    """Point a URL (absolute or relative) back through this proxy."""
    if not url or url.startswith(("data:", "#", "javascript:", "mailto:", "blob:")):
        return url
    if "/proxy/" in url:
        return url
    absolute = absolutize(url, base)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return url
    return proxy_path(absolute, proxy_origin)


def _direct_url(url: str, base: str) -> str:
    """Resolve a relative URL to an absolute URL on the target origin.

    Static assets (JS, CSS, images, fonts) are fetched directly by the
    browser from the target CDN instead of routing through this proxy.
    CheerpX's TLS socket drops large transfers (>1 MB), so proxying
    multi-megabyte bundles like vendor.js is not reliable.
    """
    if not url or url.startswith(("data:", "#", "javascript:", "mailto:", "blob:")):
        return url
    if "/proxy/" in url:
        return url
    absolute = absolutize(url, base)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return url
    return absolute


def rewrite_html(text: str, base: str, proxy_origin=None) -> str:
    """Resolve every URL in the document to an absolute target-origin URL.

    Static sub-resources (JS, CSS, images, fonts) are loaded directly from
    the target CDN — CheerpX's TLS socket drops large transfers so routing
    multi-megabyte bundles through the proxy is unreliable.  The Referer-
    based catch-all in dispatch() handles any relative requests that slip
    through (e.g. from inside JS bundles).
    """

    def _abs(match):
        return match.group("pre") + _direct_url(
            match.group("url"), base)

    def _css(match):
        return "url({q}{u}{q})".format(
            q=match.group("q"),
            u=_direct_url(match.group("url"), base),
        )

    def _srcset(match):
        parts = []
        for chunk in match.group("val").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            bits = chunk.split(None, 1)
            bits[0] = _direct_url(bits[0], base)
            parts.append(" ".join(bits))
        return "{p}{q}{v}{q}".format(
            p=match.group("pre"),
            q=match.group("q"),
            v=", ".join(parts))

    text = _ABS_URL_RE.sub(_abs, text)
    text = _CSS_URL_RE.sub(_css, text)
    text = _SRCSET_RE.sub(_srcset, text)

    # Second pass: resolve relative URLs in src/href/action/poster/data attrs
    # to absolute target-origin URLs.
    def _rel_attr(match):
        url = match.group("url").strip()
        rewritten = _direct_url(url, base)
        if rewritten == url:
            return match.group(0)
        return "{p}{q}{u}{q}".format(
            p=match.group("pre"), q=match.group("q"), u=rewritten)

    text = _REL_ATTR_RE.sub(_rel_attr, text)

    # Third pass: resolve relative URLs inside CSS url().
    def _rel_css(match):
        url = match.group("url").strip()
        rewritten = _direct_url(url, base)
        if rewritten == url:
            return match.group(0)
        return "url({q}{u}{q})".format(q=match.group("q"), u=rewritten)

    text = _REL_CSS_URL_RE.sub(_rel_css, text)

    return text


def rewrite_css(text: str, base: str, proxy_origin=None) -> str:
    """Resolve url() references inside a CSS file to absolute target URLs."""

    def _css_url(match):
        url = match.group("url").strip()
        rewritten = _direct_url(url, base)
        if rewritten == url:
            return match.group(0)
        return "url({q}{u}{q})".format(q=match.group("q"), u=rewritten)

    text = _CSS_URL_RE.sub(_css_url, text)
    text = _REL_CSS_URL_RE.sub(_css_url, text)
    return text


# --------------------------------------------------------------------------
# Self-contained documents
#
# The WebVM page is https, and Chrome blocks an http:// subresource there as
# mixed content.  A document that is injected into an https page with
# srcdoc therefore cannot reference the proxy's http:// URLs.
#
# The fix is to make the document self-contained: every sub-resource is
# fetched **server side** (where the mixed-content rule does not apply) and
# inlined as a data: URI.  The resulting document has no external
# references left, so it can be injected into an https page as-is.
# --------------------------------------------------------------------------

_ATTR_URL_RE = re.compile(
    r"""(?P<pre>\b(?:src|href)\s*=\s*)(?P<q>["'])(?P<url>[^"']*)(?P=q)""",
    re.IGNORECASE,
)

INLINE_MAX_RESOURCES = 40
INLINE_MAX_TOTAL = 6 * 1024 * 1024


def inline_subresources(text, base_url, limit=INLINE_MAX_RESOURCES,
                        max_total=INLINE_MAX_TOTAL):
    """Replace a document's sub-resources with data: URIs.

    Returns (text, inlined_count, total_bytes).
    """
    spans = []
    seen = set()
    for match in _ATTR_URL_RE.finditer(text):
        url = match.group("url").strip()
        if not url or url.startswith(("data:", "#", "javascript:", "mailto:")):
            continue
        absolute = absolutize(url, base_url)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        spans.append((match.start("url"), match.end("url"), absolute))
        if len(spans) >= limit:
            break

    inlined = 0
    total = 0
    # Replace from the end so the earlier offsets stay valid.
    for start, end, absolute in reversed(spans):
        if total >= max_total:
            break
        try:
            status, headers, body = upstream_fetch(
                absolute, headers={"Accept": "*/*", "User-Agent": "webvm-inline/1.0"}
            )
        except Exception as exc:
            log("INLINE_FAILED url={} error={}: {}".format(
                absolute, type(exc).__name__, str(exc)[:120]))
            continue
        if status >= 400 or not body:
            continue
        content_type = ""
        for name, value in headers:
            if name.lower() == "content-type":
                content_type = value.split(";")[0].strip() or "application/octet-stream"
                break
        if not content_type:
            content_type = "application/octet-stream"
        if content_type.startswith("text/html"):
            # An HTML sub-frame cannot be inlined meaningfully.
            continue
        import base64
        payload = base64.b64encode(body).decode("ascii")
        data_uri = "data:{};base64,{}".format(content_type, payload)
        text = text[:start] + data_uri + text[end:]
        inlined += 1
        total += len(body)
    return text, inlined, total


def build_self_contained(url, host_header):
    """Fetch a page and inline its sub-resources, returning a single document."""
    status, headers, body = upstream_fetch(
        url, headers={"Accept": "text/html,application/xhtml+xml,*/*",
                       "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/124.0 Safari/537.36"}
    )
    content_type = ""
    for name, value in headers:
        if name.lower() == "content-type":
            content_type = value.lower()
    if "html" not in content_type and body[:200].lstrip()[:1] != b"<":
        return status, headers, body

    text = body.decode("utf-8", "replace")
    text, inlined, total = inline_subresources(text, url)
    # Now rewrite whatever absolute URLs are left (there should be few) and
    # inject the proof script.
    text = rewrite_html(text, url)
    script = build_injection_script(extract_nonce(text))
    lowered = text.lower()
    index = lowered.rfind("</body>")
    if index == -1:
        text = text + script
    else:
        text = text[:index] + script + text[index:]
    log("INLINE_DONE url={} resources={} bytes={}".format(url, inlined, total))
    out_headers = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in ("content-length", "content-encoding"):
            continue
        if lower in FRAME_BLOCKED_HEADERS:
            continue
        if lower == "content-security-policy":
            value = relax_csp_for_injection(strip_frame_ancestors(value))
            if not value:
                continue
        if lower == "set-cookie":
            value = sanitize_set_cookie(value)
        out_headers.append((name, value))
    out_headers.append(("Content-Type", "text/html; charset=utf-8"))
    out_headers.append(("Access-Control-Allow-Origin", "*"))
    out_headers.append(("Cross-Origin-Resource-Policy", "cross-origin"))
    return status, out_headers, text.encode("utf-8")


# --------------------------------------------------------------------------
# Inline jobs
#
# Building the self-contained document takes tens of seconds (every
# sub-resource is fetched one at a time).  A browser request that stays
# idle that long is dropped — the fetch failed after ~7 s while the proxy
# was still working.  So the build runs as a background job and the client
# polls for it; every request stays short.
# --------------------------------------------------------------------------

_INLINE_JOBS = {}
_INLINE_JOBS_LOCK = threading.Lock()
_INLINE_JOB_SEQ = [0]


def start_inline_job(url, host_header):
    """Start building a self-contained document; returns the job id."""
    with _INLINE_JOBS_LOCK:
        _INLINE_JOB_SEQ[0] += 1
        job_id = str(_INLINE_JOB_SEQ[0])
        job = {"id": job_id, "url": url, "state": "running",
               "bytes": 0, "error": "", "doc": None, "headers": []}
        _INLINE_JOBS[job_id] = job
        # Keep the table small.
        if len(_INLINE_JOBS) > 16:
            for key in list(_INLINE_JOBS)[:-16]:
                _INLINE_JOBS.pop(key, None)

    def work():
        try:
            status, headers, body = build_self_contained(url, host_header)
            job["state"] = "ready"
            job["bytes"] = len(body)
            job["doc"] = body
            job["headers"] = headers
        except Exception as exc:
            job["state"] = "failed"
            job["error"] = "{}: {}".format(type(exc).__name__, str(exc)[:200])
            log("INLINE_JOB_FAILED id={} error={}".format(job_id, job["error"]))

    threading.Thread(target=work, daemon=True,
                     name="inline-job-" + job_id).start()
    return job_id


def inline_job_status(job_id):
    with _INLINE_JOBS_LOCK:
        job = _INLINE_JOBS.get(job_id)
        if job is None:
            return {"state": "unknown"}
        return {"state": job["state"], "bytes": job["bytes"],
                "error": job["error"], "url": job["url"]}


def inline_job_document(job_id):
    with _INLINE_JOBS_LOCK:
        job = _INLINE_JOBS.get(job_id)
        if job is None or job["state"] != "ready":
            return None, [], b""
        return job["state"], job["headers"], job["doc"]


def strip_frame_ancestors(csp: str) -> str:
    """Remove only the frame-ancestors directive from a CSP header."""
    kept = []
    for directive in csp.split(";"):
        if directive.strip().lower().startswith("frame-ancestors"):
            continue
        if directive.strip():
            kept.append(directive.strip())
    return "; ".join(kept)


def relax_csp_for_injection(csp: str) -> str:
    """Make the CSP permissive enough for the injected proof script to run.

    * ``sandbox`` would block scripting entirely, so it is dropped.
    * ``script-src`` gains ``'unsafe-inline'`` so the injected inline
      ``<script>`` executes and can touch the DOM.
    * every other directive is left alone.
    """
    directives = []
    for directive in csp.split(";"):
        directive = directive.strip()
        if not directive:
            continue
        name = directive.split(None, 1)[0].lower()
        if name == "sandbox":
            continue
        if name == "script-src" and "'unsafe-inline'" not in directive.lower():
            directive = directive + " 'unsafe-inline'"
        directives.append(directive)
    return "; ".join(directives)


def sanitize_set_cookie(value: str) -> str:
    """Make a cookie belong to the proxy origin instead of the upstream host."""
    pieces = []
    for part in value.split(";"):
        name = part.split("=", 1)[0].strip().lower()
        if name in ("domain", "secure", "samesite", "partitioned"):
            continue
        pieces.append(part.strip())
    return "; ".join(pieces)


# --------------------------------------------------------------------------
# Upstream fetching
# --------------------------------------------------------------------------

# CheerpX's cooperative scheduler means two TLS handshakes at the same
# time starve each other and can wedge the whole VM.  Serialising them
# avoids that; the watchdog bounds a handshake whose client disappears.
_TLS_HANDSHAKE_LOCK = threading.Lock()
TLS_HANDSHAKE_TIMEOUT = 25


def _ssl_context():
    ctx = ssl.create_default_context()
    # The proxy terminates the client's TLS leg upstream, so the upstream
    # certificate is not the client's business.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# --------------------------------------------------------------------------
# Upstream connection pool and response cache
#
# A TLS handshake inside CheerpX is expensive (crypto is slow in WASM), and
# a page like Kaggle pulls dozens of sub-resources.  Opening a fresh
# connection per request means a handshake per resource, which makes the
# page take minutes.  Reusing keep-alive connections turns that into one
# handshake per host.
# --------------------------------------------------------------------------

_POOL = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX_PER_HOST = 4

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 120.0
_CACHE_MAX = 64

# How many upstream fetches may be in flight at the same time.
#
# A heavy page (Kaggle's SPA) asks for dozens of resources at once.  Every
# one of them would otherwise open a connection and start a TLS handshake
# inside CheerpX, whose scheduler is cooperative: the VM's accept loop
# stops running, the whole machine appears to hang, and the ports stop
# answering.
#
# One at a time is deliberate.  Two concurrent TLS handshakes were enough
# to wedge the VM completely -- the handshake blocks the cooperative
# scheduler, so interleaved handshakes starve each other.  Serialising
# them keeps the VM responsive; the response cache absorbs the repeats.
_UPSTREAM_CONCURRENCY = 1
_UPSTREAM_SEMAPHORE = threading.Semaphore(_UPSTREAM_CONCURRENCY)


def _pool_get(key):
    with _POOL_LOCK:
        conns = _POOL.get(key)
        if not conns:
            return None
        while conns:
            conn = conns.pop()
            if getattr(conn, "sock", None) is not None:
                return conn
        return None


def _pool_put(key, conn):
    with _POOL_LOCK:
        conns = _POOL.setdefault(key, [])
        if len(conns) >= _POOL_MAX_PER_HOST:
            try:
                conn.close()
            except OSError:
                pass
        else:
            conns.append(conn)


def _pool_drop(key, conn):
    with _POOL_LOCK:
        conns = _POOL.get(key)
        if conns and conn in conns:
            conns.remove(conn)


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.time():
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key, value):
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (time.time() + _CACHE_TTL, value)


def _close_quietly(sock):
    try:
        sock.close()
    except OSError:
        pass


def _watchdog(sock, timeout):
    """Close a socket after `timeout` seconds.

    CheerpX does not implement select()/poll() on sockets, and Python
    implements socket timeouts with select().  A timeout therefore never
    fires here and the read blocks forever.  A watchdog thread that closes
    the socket is the only reliable way to bound a network operation.
    """
    def _kill():
        log("WATCHDOG_FIRED timeout={}s".format(timeout))
        _close_quietly(sock)

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    return timer


def _open_upstream(scheme, host, port, timeout):
    """Open a socket to the upstream and do the TLS handshake if needed."""
    import socket as _socket

    raw = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    raw.settimeout(None)          # blocking: select() is unavailable
    timer = _watchdog(raw, timeout)
    try:
        raw.connect((host, port))
        log("UPSTREAM_PHASE url_connect host={} port={} ok".format(host, port))
        if scheme == "https":
            raw = _ssl_context().wrap_socket(raw, server_hostname=host)
            log("UPSTREAM_PHASE tls_done host={} port={}".format(host, port))
    except Exception:
        timer.cancel()
        _close_quietly(raw)
        raise
    return raw, timer


def upstream_fetch(url, method="GET", headers=None, body=None, timeout=30,
                   attempts=3):
    """Fetch an absolute http/https URL.  Returns (status, headers, body).

    The WebVM resolves DNS through the browser, and the browser's network
    is occasionally unavailable ("Temporary failure in name resolution").
    Retrying turns those transient failures into a successful fetch
    instead of a 502.
    """
    import http.client

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("unsupported scheme: {}".format(parts.scheme))
    host = parts.hostname
    if not host:
        raise ValueError("no host in {}".format(url))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    target_origin = "{}://{}".format(parts.scheme, parts.hostname)
    if parts.port and parts.port not in (80, 443):
        target_origin += ":{}".format(parts.port)

    outgoing = {}
    for name, value in (headers or {}).items():
        lower = name.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower in ("host", "content-length", "connection"):
            continue
        if lower in PROXY_REVEAL_HEADERS:
            continue
        outgoing[name] = value
    # Always ask for an uncompressed body.  The proxy has to rewrite the HTML
    # and inject a script, which is impossible on a gzip/brotli stream — and
    # the browser sends "Accept-Encoding: gzip, deflate, br" by default.
    outgoing["Accept-Encoding"] = "identity"
    outgoing.setdefault("User-Agent",
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    outgoing.setdefault("Accept-Language", "en-US,en;q=0.9")
    outgoing.setdefault("Accept",
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8")
    outgoing["Referer"] = target_origin + "/"
    outgoing["sec-fetch-dest"] = "document"
    outgoing["sec-fetch-mode"] = "navigate"
    outgoing["sec-fetch-site"] = "none"
    outgoing["sec-fetch-user"] = "?1"

    cache_key = (method, url)
    if method == "GET":
        cached = _cache_get(cache_key)
        if cached is not None:
            log("UPSTREAM_TIMING url={} cache=hit total_ms=0".format(url))
            return cached

    key = (parts.scheme, host, port)
    last_error = None
    for attempt in range(1, attempts + 1):
        started = time.time()
        conn = None
        timer = None
        reused = False
        acquired = False
        try:
            _UPSTREAM_SEMAPHORE.acquire()
            acquired = True
            conn = _pool_get(key)
            if conn is not None:
                reused = True
                if getattr(conn, "sock", None) is None:
                    conn = None
                    reused = False
                else:
                    timer = _watchdog(conn.sock, timeout)
            if conn is None:
                raw, timer = _open_upstream(parts.scheme, host, port, timeout)
                conn = http.client.HTTPConnection(host, port, timeout=None)
                conn.sock = raw
            conn.request(method, path, body=body, headers=outgoing)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY)
            result = (resp.status, resp.getheaders(), data)
            if timer is not None:
                timer.cancel()
            if reused or not resp.will_close:
                _pool_put(key, conn)
                conn = None
            log("UPSTREAM_TIMING url={} reused={} status={} bytes={} total_ms={}".format(
                url, reused, result[0], len(data), int((time.time() - started) * 1000)))
            if method == "GET" and result[0] < 400:
                has_cookie = any(name.lower() == "set-cookie"
                                  for name, _ in result[1])
                if not has_cookie:
                    _cache_put(cache_key, result)
            return result
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            if timer is not None:
                timer.cancel()
            if conn is not None:
                _close_quietly(getattr(conn, "sock", None))
                try:
                    conn.close()
                except OSError:
                    pass
                _pool_drop(key, conn)
            log("UPSTREAM_RETRY attempt={}/{} url={} error={}: {}".format(
                attempt, attempts, url, type(exc).__name__, str(exc)[:120]))
            if attempt < attempts:
                time.sleep(min(2 * attempt, 6))
        finally:
            if acquired:
                _UPSTREAM_SEMAPHORE.release()
    raise last_error


# A nonce in a CSP script-src makes browsers IGNORE 'unsafe-inline', so an
# injected inline script has to carry the same nonce as the page's own
# scripts.  Kaggle (like most SPAs) uses one.
_NONCE_RE = re.compile(r"""nonce\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def extract_nonce(text):
    """Find the nonce used by the page's own scripts, if any."""
    match = _NONCE_RE.search(text)
    return match.group(1) if match else None


def build_injection_script(nonce=None):
    """The proof script, carrying the page's nonce when there is one."""
    if not nonce:
        return INJECTION_SCRIPT
    return INJECTION_SCRIPT.replace(
        "<script>", '<script nonce="{}">'.format(nonce), 1
    )


def decompress_body(body, encoding):
    """Best-effort decompression.  Returns (body, still_compressed)."""
    if not body or not encoding:
        return body, False
    enc = encoding.strip().lower()
    try:
        if enc == "gzip" or enc == "x-gzip":
            import gzip
            return gzip.decompress(body), False
        if enc == "deflate":
            import zlib
            try:
                return zlib.decompress(body), False
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS), False
    except Exception:
        # Leave the body alone; the caller keeps the Content-Encoding header
        # so the browser can still decode it.
        return body, True
    # brotli / unknown: keep it compressed and pass the header through.
    return body, enc not in ("identity", "")


def transform_response(status, headers, body, request_url, proxy_origin=None):
    """Strip framing blockers, rewrite URLs and inject the proof script."""
    content_type = ""
    content_encoding = ""
    for name, value in headers:
        if name.lower() == "content-type":
            content_type = value.lower()
        elif name.lower() == "content-encoding":
            content_encoding = value
    if content_encoding:
        body, still_compressed = decompress_body(body, content_encoding)
        if still_compressed:
            # Cannot rewrite a compressed body; hand it through untouched.
            return status, list(headers), body

    is_html = "text/html" in content_type or (
        not content_type and body[:200].lstrip()[:1] == b"<")

    out_headers = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower in FRAME_BLOCKED_HEADERS:
            continue
        if lower == "content-length":
            continue
        if lower == "content-encoding":
            # The body was decompressed above, so the header must go too.
            continue
        if lower == "set-cookie":
            value = sanitize_set_cookie(value)
        elif lower == "content-security-policy":
            value = relax_csp_for_injection(strip_frame_ancestors(value))
            if not value:
                continue
        elif lower == "location":
            try:
                value = proxy_path(absolutize(value, request_url), proxy_origin)
            except Exception:
                pass
        out_headers.append((name, value))

    # CORS: the WebVM page (a different origin) fetches the proxied HTML
    # and renders it into an iframe with srcdoc, which keeps the frame in
    # the page's own origin and therefore keeps DOM access.  CORS headers
    # are what make that fetch readable.
    out_headers.append(("Access-Control-Allow-Origin", "*"))
    out_headers.append(("Access-Control-Allow-Credentials", "false"))
    out_headers.append(("Access-Control-Allow-Headers", "*"))
    out_headers.append(("Access-Control-Expose-Headers", "*"))
    # The WebVM page is cross-origin isolated, so responses have to be
    # embeddable by a COEP: require-corp document.
    out_headers.append(("Cross-Origin-Resource-Policy", "cross-origin"))

    is_css = "text/css" in content_type

    if is_html and body:
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = None
        if text is not None:
            text = rewrite_html(text, request_url, proxy_origin)
            nonce = extract_nonce(text)
            # SPAs use window.location.pathname for client-side routing.
            # When the page is served at /proxy/<encoded-url>, the router
            # sees that path and renders a 404.  Fix by replacing the URL
            # with the target's own path before any scripts run.
            target_parts = urlsplit(request_url)
            target_path = target_parts.path or "/"
            if target_parts.query:
                target_path += "?" + target_parts.query
            nonce_attr = ' nonce="{}"'.format(nonce) if nonce else ""
            path_fix = (
                '<script{nonce}>'
                'try{{history.replaceState(null,"",{path})}}catch(e){{}}'
                '</script>'
            ).format(nonce=nonce_attr, path=json.dumps(target_path))
            # Inject the path fix into <head> BEFORE any other scripts
            # so the SPA router sees the correct pathname.
            lowered = text.lower()
            head_end = lowered.find("</head>")
            first_script = lowered.find("<script")
            if first_script != -1 and (head_end == -1 or first_script < head_end):
                text = text[:first_script] + path_fix + text[first_script:]
            elif head_end != -1:
                text = text[:head_end] + path_fix + text[head_end:]
            else:
                text = path_fix + text
            # Inject the proof marker just before </body>.
            script = build_injection_script(nonce)
            lowered = text.lower()
            index = lowered.rfind("</body>")
            if index == -1:
                text = text + script
            else:
                text = text[:index] + script + text[index:]
            body = text.encode("utf-8")

    elif is_css and body:
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = None
        if text is not None:
            text = rewrite_css(text, request_url, proxy_origin)
            body = text.encode("utf-8")

    return status, out_headers, body


# --------------------------------------------------------------------------
# Minimal HTTP parsing / writing over raw sockets
# --------------------------------------------------------------------------

class HttpError(Exception):
    pass


def recv_until(sock, terminator, limit=65536):
    data = b""
    while terminator not in data:
        if len(data) > limit:
            raise HttpError("header section too large")
        try:
            chunk = sock.recv(4096)
        except OSError as exc:
            raise HttpError("recv failed: {}".format(exc))
        if not chunk:
            break
        data += chunk
    return data


def recv_exactly(sock, count):
    data = b""
    while len(data) < count:
        try:
            chunk = sock.recv(min(65536, count - len(data)))
        except OSError as exc:
            raise HttpError("recv failed: {}".format(exc))
        if not chunk:
            break
        data += chunk
    return data


class Request(object):
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body

    def header(self, name, default=None):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return default


def parse_request(sock):
    raw = recv_until(sock, b"\r\n\r\n")
    if not raw:
        raise HttpError("empty request")
    head, _, rest = raw.partition(b"\r\n\r\n")
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
    except Exception as exc:
        raise HttpError("undecodable request: {}".format(exc))
    if not lines or not lines[0].strip():
        raise HttpError("no request line")
    bits = lines[0].split()
    if len(bits) < 2:
        raise HttpError("bad request line: {}".format(lines[0]))
    method = bits[0].upper()
    target = bits[1]
    version = bits[2] if len(bits) > 2 else "HTTP/1.0"

    headers = []
    for line in lines[1:]:
        if not line.strip():
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
        if len(headers) > MAX_HEADER_LINES:
            raise HttpError("too many headers")

    body = b""
    length_header = None
    for name, value in headers:
        if name.lower() == "content-length":
            length_header = value
    if length_header:
        try:
            length = int(length_header)
        except ValueError:
            length = 0
        if length > 0:
            body = recv_exactly(sock, min(length, MAX_BODY))
    return Request(method, target, version, headers, body)


# Responses are written in chunks of this size.  A single large write to a
# CheerpX socket can be truncated: the server logs SEND_OK but the browser
# sees a broken body and reports "Failed to fetch".
SEND_CHUNK = 16384

STATUS_TEXT = {    200: "OK", 204: "No Content", 206: "Partial Content",
    301: "Moved Permanently", 302: "Found", 303: "See Other",
    304: "Not Modified", 307: "Temporary Redirect", 308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 407: "Proxy Authentication Required",
    408: "Request Timeout", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
    504: "Gateway Timeout",
}


def send_response(sock, status, headers, body, close=True, client=None):
    """Write a complete HTTP response.  Never raises on a dead client."""
    reason = STATUS_TEXT.get(status, "OK")
    head = ["HTTP/1.1 {} {}".format(status, reason)]
    has_length = False
    for name, value in headers:
        if name.lower() == "content-length":
            has_length = True
        head.append("{}: {}".format(name, value))
    if not has_length:
        head.append("Content-Length: {}".format(len(body)))
    head.append("Cache-Control: no-store")
    if close:
        head.append("Connection: close")
    try:
        raw_head = ("\r\n".join(head) + "\r\n\r\n").encode("iso-8859-1")
    except UnicodeEncodeError as exc:
        log("SEND_ENCODING_FAILED status={} error={}".format(status, exc))
        raw_head = ("HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
                    "Content-Length: 40\r\nConnection: close\r\n\r\n"
                    "header encoding failed\r\n").encode("ascii")
        body = b""
    payload = raw_head + (body or b"")
    log("SEND_START status={} bytes={} client={}".format(
        status, len(payload), client))
    try:
        # Send in chunks.  A single large write to a CheerpX socket can be
        # truncated, and the browser then reports "Failed to fetch" even
        # though the server logged SEND_OK.  The self-contained document is
        # a few hundred kilobytes, so this matters.
        sent = 0
        while sent < len(payload):
            chunk = payload[sent:sent + SEND_CHUNK]
            sock.sendall(chunk)
            sent += len(chunk)
        log("SEND_OK status={} bytes={}".format(status, len(payload)))
        # Let the client read the response before the socket goes away.
        # Closing straight after a large write truncates it: the browser
        # then reports "Failed to fetch" even though the server logged
        # SEND_OK.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        time.sleep(0.4)
        return True
    except OSError as exc:
        log("SEND_FAILED status={} bytes={} client={} error={}: {}".format(
            status, len(payload), client, type(exc).__name__, exc))
        return False


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

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
 iframe{display:block;width:100%;height:calc(100vh - 150px);border:0;background:white}
</style>
</head>
<body>
<header>
<strong>Web Adjuster &rarr; Proxy iframe test</strong>
<p>Target: <span id="target-label"></span></p>
<p>Proxy: <a id="proxy-link" href="#" target="_blank" rel="noopener">open forward proxy (port __ADJUSTER_PORT__)</a></p>
<p id="status">Loading the proxied page...</p>
</header>
<iframe id="proxy-frame" title="Proxy through Web Adjuster" src="__IFRAME_SRC__"></iframe>
<script>
(function() {
  var status = document.getElementById('status');
  var frame = document.getElementById('proxy-frame');
  var link = document.getElementById('proxy-link');
  document.getElementById('target-label').textContent = __TARGET_JSON__;
  link.href = 'http://' + window.location.hostname + ':' + __ADJUSTER_PORT__ + '/';

  var loaded = false;
  var confirmed = false;
  frame.addEventListener('load', function() {
    loaded = true;
    status.textContent = 'Iframe load event fired at ' + new Date().toISOString()
      + ' - this alone does not prove the page rendered correctly.';
    checkInjection();
  });
  frame.addEventListener('error', function() {
    status.textContent = 'Iframe error event fired at ' + new Date().toISOString();
  });

  function checkInjection() {
    try {
      var doc = frame.contentDocument;
      if (!doc) {
        status.textContent = 'Iframe document is not reachable yet - retrying...';
        return false;
      }
      if (!doc.documentElement) {
        status.textContent = 'Iframe is still parsing (readyState='
          + doc.readyState + ') - retrying...';
        return false;
      }
      var el = doc.getElementById('webvm-adjuster-injection-proof');
      var attr = doc.documentElement.getAttribute('data-webvm-adjuster-injected');
      if (el || attr === 'true') {
        confirmed = true;
        status.textContent = 'JS/DOM INJECTION CONFIRMED: marker element present = '
          + (!!el) + ', documentElement attribute = ' + attr
          + ', marker text = ' + (el ? el.textContent : 'n/a');
        return true;
      }
      status.textContent = 'Iframe loaded (title="' + (doc.title || '?')
        + '") but the injection marker is not there yet - retrying...';
      return false;
    } catch (e) {
      status.textContent = 'Injection check failed: ' + e;
      return false;
    }
  }

  // The proxied page can take a long time to parse under CheerpX, so keep
  // polling until the marker shows up.  In bridge mode the popup does not
  // display the page at all -- it only fetches the document to hand it
  // over, and a second load would compete with that fetch for the
  // proxy's serialised upstream queue.
  if (!__BRIDGE_FLAG__) {
    (function poll() {
      if (!confirmed) { checkInjection(); }
      window.setTimeout(poll, 2000);
    })();
  }

  // Bridge mode: this page was opened as a popup by the WebVM page.
  //
  // The WebVM page is https and cannot fetch the proxy's http:// address
  // (mixed content), and a popup cannot postMessage back to its opener
  // because the page sets Cross-Origin-Opener-Policy: same-origin, which
  // nulls window.opener.  But the opener CAN read popup.location.href
  // cross-origin, so the document is handed over in the popup's fragment.
  //
  // The document is self-contained (every sub-resource is inlined as a
  // data: URI by the proxy), so the opener can inject it with srcdoc and
  // the frame inherits the opener's origin — which keeps DOM access.
  if (__BRIDGE_FLAG__) {
    document.getElementById('status').textContent =
      'Bridge mode: fetching the self-contained document...';
    console.log('[bridge] fetching the document');
    // A single proxied fetch.  Building a fully inlined document means one
    // upstream fetch per sub-resource, and the WebVM can only do one TLS
    // handshake at a time -- that saturates the VM.  The sub-resource URLs
    // in the document are absolute and point back at the proxy, so they
    // load as soon as the proxy has a trusted https origin.
    fetch('/proxy/' + encodeURIComponent(__TARGET_JSON__), {cache: 'no-store'})
      .then(function (r) {
        console.log('[bridge] proxy status', r.status);
        return r.text();
      })
      .then(function (html) {
        console.log('[bridge] document bytes', html.length);
        document.getElementById('status').textContent =
          'Bridge mode: handing the document over (' + html.length + ' bytes)...';
        // Hand the document over by navigating to the opener's own origin
        // with the document in the query string.  A navigation to the
        // page's origin is always intercepted by its service worker, which
        // is the only channel left: COOP: same-origin nulls
        // window.opener and also blocks reading popup.location.
        var referrer = document.referrer || '';
        var origin = referrer ? referrer.split('/')[0] : '';
        // The opener passes its origin explicitly; the referrer is not
        // always available (a referrer policy can withhold it).
        var params = new URLSearchParams(window.location.search);
        if (params.get('origin')) {
          origin = params.get('origin');
        }
        if (!origin) {
          document.getElementById('status').textContent =
            'Bridge mode: cannot work out the opener origin.';
          return;
        }
        location.href = origin + '/webvm/proxy-inbox?doc='
          + encodeURIComponent(html);
      })
      .catch(function (e) {
        console.log('[bridge] failed', e);
        document.getElementById('status').textContent = 'Bridge failed: ' + e;
      });
  }
})();
</script>
</body>
</html>
"""


def render_page(target, proxy_origin=None, bridge=False):
    return (PAGE_TEMPLATE
            .replace("__ADJUSTER_PORT__", str(ADJUSTER_PORT))
            .replace("__IFRAME_SRC__", "about:blank" if bridge
                     else proxy_path(target, proxy_origin))
            .replace("__TARGET_JSON__", json.dumps(target))
            .replace("__BRIDGE_FLAG__", "true" if bridge else "false"))


class Handler(object):
    """Serves one request.  Kept compatible with the old socketserver API
    so that pi_impl.py can keep using ``launcher_ns["Handler"]``."""

    def __init__(self, request, client_address, server):
        # ``request`` is the client socket (socketserver-compatible naming).
        self.request = request
        self.client_address = client_address
        self.server = server
        self.mode = getattr(server, "mode", "wrapper")
        self.parsed = None

    # -- helpers ---------------------------------------------------------
    def client_label(self):
        try:
            return "{}:{}".format(*self.client_address)
        except Exception:
            return "?"

    def proxy_origin(self):
        """The public origin of this proxy, taken from the Host header.

        Rewritten URLs have to be absolute when the proxied HTML is put
        into a frame with srcdoc, otherwise they would resolve against the
        host page's origin instead of the proxy's.
        """
        host = ""
        if self.parsed is not None:
            host = self.parsed.header("Host") or ""
        if not host:
            return None
        scheme = "https" if getattr(self.server, "is_tls", False) else "http"
        return "{}://{}".format(scheme, host)

    def _send(self, status, content_type, body, extra=None):
        headers = [
            ("Content-Type", content_type),
            # The WebVM page is cross-origin isolated (COEP: require-corp),
            # so every response the service worker fetches has to be
            # CORS-approved and carry a permissive CORP header.
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Headers", "*"),
            ("Access-Control-Expose-Headers", "*"),
            ("Cross-Origin-Resource-Policy", "cross-origin"),
        ]
        if extra:
            headers.extend(extra)
        send_response(self.request, status, headers, body,
                      client=self.client_label())

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _proxy_url(self, url, method="GET", body=None, extra_headers=None):
        """Fetch a URL through the proxy and return the transformed response."""
        bump("requests")
        headers = {}
        if self.parsed is not None:
            for name, value in self.parsed.headers:
                headers[name] = value
        if extra_headers:
            headers.update(extra_headers)
        try:
            status, upstream_headers, upstream_body = upstream_fetch(
                url, method=method, headers=headers, body=body
            )
        except Exception as exc:
            bump("upstream_errors")
            log("UPSTREAM_FAILED url={} error={}: {}".format(
                url, type(exc).__name__, str(exc)[:200]))
            self._send(502, "text/plain; charset=utf-8",
                       ("Proxy fetch failed: {}\n".format(exc)).encode("utf-8"))
            return

        status, out_headers, out_body = transform_response(
            status, upstream_headers, upstream_body, url, self.proxy_origin()
        )
        log("PROXIED status={} url={} bytes={}".format(status, url, len(out_body)))
        send_response(self.request, status, out_headers, out_body,
                      client=self.client_label())

    # -- dispatch --------------------------------------------------------
    def handle(self):
        try:
            request = parse_request(self.request)
        except HttpError as exc:
            log("BAD_REQUEST error={}".format(exc))
            return
        except OSError as exc:
            log("READ_FAILED error={}: {}".format(type(exc).__name__, exc))
            return
        self.parsed = request

        try:
            self.dispatch(request)
        except Exception as exc:
            log("HANDLER_ERROR error={}: {}".format(type(exc).__name__, str(exc)[:300]))

    def dispatch(self, request):
        target = request.target
        mode = self.mode
        log("REQUEST method={} target={} client={}".format(
            request.method, target[:140], self.client_label()))

        # Forward-proxy mode: the request line carries an absolute URI.
        if mode == "forward" and target.startswith("http://") \
                or mode == "forward" and target.startswith("https://"):
            self._proxy_url(target, method=request.method, body=request.body or None)
            return

        path = target.split("?", 1)[0]
        query = target.split("?", 1)[1] if "?" in target else ""
        params = {}
        for chunk in query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            params[unquote(key)] = unquote(value)

        if path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/proxy-status":
            with STATE_LOCK:
                payload = dict(_state)
            payload["wrapper_port"] = WRAPPER_PORT_INPROC
            payload["forward_port"] = ADJUSTER_PORT
            self._send_json(200, payload)
            return
        if request.method == "OPTIONS":
            # CORS preflight for the WebVM page's cross-origin fetch.
            self._send(204, "text/plain; charset=utf-8", b"", extra=[
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS"),
                ("Access-Control-Allow-Headers", "*"),
                ("Access-Control-Max-Age", "600"),
            ])
            return
        if path.startswith("/proxy/"):
            url = target_from_path(path)
            if not url:
                self._send(400, "text/plain; charset=utf-8",
                           b"Missing target URL\n")
                return
            self._proxy_url(url, method=request.method, body=request.body or None)
            return
        if path.startswith("/proxy-inline/start/"):
            url = target_from_path(path.replace("/proxy-inline/start", "/proxy", 1))
            if not url:
                self._send(400, "text/plain; charset=utf-8",
                           b"Missing target URL\n")
                return
            job_id = start_inline_job(url, self.proxy_origin())
            self._send_json(200, {"id": job_id, "state": "running", "url": url})
            return
        if path.startswith("/proxy-inline/status/"):
            job_id = path.rsplit("/", 1)[-1]
            self._send_json(200, inline_job_status(job_id))
            return
        if path.startswith("/proxy-inline/doc/"):
            job_id = path.rsplit("/", 1)[-1]
            state, headers, body = inline_job_document(job_id)
            if state != "ready":
                self._send(202, "application/json; charset=utf-8",
                           b'{"state":"not ready"}')
                return
            # The document can be a few hundred kilobytes and a large
            # transfer over the CheerpX socket never completes, so it is
            # fetched in parts.
            part = 0
            total = 1
            if "?" in path:
                params = {}
                for chunk in path.split("?", 1)[1].split("&"):
                    if "=" in chunk:
                        key, _, value = chunk.partition("=")
                        params[key] = unquote(value)
                try:
                    part = int(params.get("part", "0"))
                    total = max(1, int(params.get("total", "1")))
                except ValueError:
                    part, total = 0, 1
            piece_size = max(1, (len(body) + total - 1) // total)
            piece = body[part * piece_size:(part + 1) * piece_size]
            out_headers = [(name, value) for name, value in headers
                           if name.lower() != "content-length"]
            out_headers.append(("Content-Length", str(len(piece))))
            send_response(self.request, 200, out_headers, piece,
                          client=self.client_label())
            return
        if path in ("/", "/index.html"):
            target_url = params.get("target") or DEFAULT_TARGET
            bridge = params.get("bridge") in ("1", "true", "yes")
            body = render_page(target_url, self.proxy_origin(), bridge).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        # Catch-all: resolve bare paths (e.g. /static/assets/app.css) that
        # sub-resources request when the iframe navigates directly to a
        # /proxy/ URL.  The Referer carries the proxy URL of the page that
        # triggered the load, so we can recover the target origin.
        # After history.replaceState the Referer won't contain /proxy/,
        # so fall back to DEFAULT_TARGET for any unmatched path.
        referer = request.header("referer", "")
        ref_path = urlsplit(referer).path if referer else ""
        ref_target = None
        if ref_path.startswith("/proxy/"):
            ref_target = target_from_path(ref_path)
        if not ref_target:
            ref_target = DEFAULT_TARGET
        resolved = absolutize(target, ref_target)
        if resolved and urlsplit(resolved).scheme in ("http", "https"):
            # Static assets (JS, CSS, images, fonts) → 302 to CDN.
            # CheerpX TLS drops large transfers so proxying bundles
            # is unreliable; the browser fetches them directly.
            ext = resolved.rsplit("?", 1)[0].rsplit(".", 1)
            ext = ("." + ext[-1].lower()) if len(ext) > 1 else ""
            if ext in _STATIC_EXTS:
                log("REDIRECT resolved={} from referer={}".format(
                    resolved[:140], ref_target[:80]))
                self._send(302, "text/plain; charset=utf-8",
                           b"Redirecting\n",
                           extra=[("Location", resolved)])
                return
            log("CATCHALL resolved={} from referer={}".format(
                resolved[:140], ref_target[:80]))
            self._proxy_url(resolved, method=request.method,
                            body=request.body or None)
            return
        self._send(404, "text/plain; charset=utf-8", b"Not Found\n")


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class WrapperServer(object):
    """Raw-socket HTTP server.

    ``http.server``/``socketserver`` are deliberately avoided: under CheerpX
    the header write failed with ``OSError: [Errno 107] Transport endpoint
    is not connected`` and the client never got a reply.

    When ``ssl_context`` is given the accepted connections are wrapped in
    TLS, so the proxy can be reached over https:// (needed because Chrome
    blocks an http:// iframe on an https:// page as mixed content).
    """

    def __init__(self, address, handler_class, mode="wrapper", ssl_context=None):
        self.address = address
        self.handler_class = handler_class
        self.mode = mode
        self.ssl_context = ssl_context
        self.is_tls = ssl_context is not None
        self._running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(address)
        self.socket.listen(16)

    def serve_forever(self, poll_interval=0.5):
        while self._running:
            try:
                client, addr = self.socket.accept()
            except OSError:
                if not self._running:
                    break
                continue
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            threading.Thread(
                target=self._handle_client,
                args=(client, addr),
                daemon=True,
                name="proxy-client-{}".format(addr[0]),
            ).start()

    def _handle_client(self, client, addr):
        try:
            if self.ssl_context is not None:
                # Two TLS handshakes at the same time starve each other in
                # CheerpX and wedge the whole VM (see finding 20), so the
                # handshake is serialised.  The watchdog bounds it as well:
                # a client that goes away mid-handshake (the browser
                # aborts its health check) must not hold the lock forever
                # and block every later connection.
                timer = _watchdog(client, TLS_HANDSHAKE_TIMEOUT)
                try:
                    with _TLS_HANDSHAKE_LOCK:
                        client = self.ssl_context.wrap_socket(
                            client, server_side=True)
                finally:
                    timer.cancel()
            handler = self.handler_class(client, addr, self)
            handler.handle()
        except Exception as exc:
            log("CONNECTION_ERROR error={}: {}".format(
                type(exc).__name__, str(exc)[:200]))
        finally:
            try:
                client.close()
            except OSError:
                pass
    def shutdown(self):
        self._running = False
        try:
            self.socket.close()
        except OSError:
            pass

    def server_close(self):
        self.shutdown()


def start_wrapper(port=WRAPPER_PORT_INPROC, mode="wrapper"):
    server = WrapperServer((BIND, port), Handler, mode=mode)
    thread = threading.Thread(target=server.serve_forever,
                              name="proxy-wrapper", daemon=True)
    thread.start()
    log("WRAPPER_LISTENING bind={} port={} mode={}".format(BIND, port, mode))
    return server


def start_tls_wrapper(port, cert_path, key_path, mode="wrapper"):
    """Start the wrapper with TLS, so it can be reached over https://."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server = WrapperServer((BIND, port), Handler, mode=mode,
                           ssl_context=context)
    thread = threading.Thread(target=server.serve_forever,
                              name="proxy-wrapper-tls", daemon=True)
    thread.start()
    log("TLS_WRAPPER_LISTENING bind={} port={} cert={}".format(
        BIND, port, cert_path))
    return server


def main() -> None:
    log("BOOT bind={} wrapper_port={} forward_port={}".format(
        BIND, WRAPPER_PORT, ADJUSTER_PORT))
    servers = []
    try:
        servers.append(start_wrapper(WRAPPER_PORT, "wrapper"))
        servers.append(start_wrapper(ADJUSTER_PORT, "forward"))
    except OSError as exc:
        log("BIND_FAILED error={}: {}".format(type(exc).__name__, exc))
        raise
    log("PROXY_READY wrapper=http://{}:{}/ forward=http://{}:{}/".format(
        BIND, WRAPPER_PORT, BIND, ADJUSTER_PORT))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("FATAL {}: {}".format(type(exc).__name__, str(exc)[:500]))
        raise
