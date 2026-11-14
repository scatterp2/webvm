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

import collections
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

# Gives the page a read/write view of the cookie jar the proxy holds.
#
# In a credentialless iframe `document.cookie` is always empty, so a page
# that reads a cookie to build a request header (Kaggle reads XSRF-TOKEN
# that way) cannot find it and gives up.  This bridges the jar into the
# page: the getter returns the cookies the proxy holds for the target,
# and the setter records what the page writes and hands it to the proxy
# so it goes upstream with the next request.
#
# Like the other injected scripts, the text avoids spaces so it survives
# being passed through command-line argument handling.
_COOKIE_BRIDGE_SCRIPT = (
    "<script>(function(){try{"
    "var C=__COOKIE_STRING__;"
    "var S={};"
    "function parse(s){s.split(';').forEach(function(p){"
    "var i=p.indexOf('=');"
    "if(i>0)S[p.slice(0,i).trim()]=p.slice(i+1).trim();"
    "});}"
    "parse(C);"
    "function str(){var a=[];for(var k in S){a.push(k+'='+S[k]);}return a.join('; ');}"
    "var proto=window.Document&&Document.prototype;"
    "var desc=proto&&Object.getOwnPropertyDescriptor(proto,'cookie');"
    "if(desc&&desc.configurable){"
    "Object.defineProperty(document,'cookie',{"
    "configurable:true,enumerable:true,"
    "get:function(){return str();},"
    "set:function(v){"
    "if(typeof v!=='string')return;"
    "var i=v.indexOf('=');if(i<0)return;"
    "var k=v.slice(0,i).trim(),val=v.slice(i+1).trim();"
    "if(val===''){delete S[k];}else{S[k]=val;}"
    "try{"
    "var b=new Blob([k+'='+val],{type:'text/plain'});"
    "navigator.sendBeacon('__COOKIE_ENDPOINT__',b);"
    "}catch(e){}"
    "}});"
    "}"
    "}catch(e){}})();"
    "</script>"
)

# Routes the page's own API calls back through the proxy.
#
# The document is served from the proxy's origin, but the page's JavaScript
# calls the original site absolutely ("https://www.kaggle.com/api/...").
# Those calls would leave the proxy, hit the internet directly and be
# blocked by CORS, which is what made Kaggle's SPA render its crash page.
# Patching fetch and XMLHttpRequest sends them to the proxy instead, where
# the catch-all resolves them against the original target.
#
# Like the proof script, the text avoids spaces so it survives being passed
# through command-line argument handling.
_API_ROUTING_SCRIPT = (
    "<script>(function(){try{"
    "var O=__TARGET_ORIGIN__;"
    "function P(u){try{"
    "if(typeof u!=='string')return u;"
    "if(u.indexOf(O)!==0)return u;"
    "return '/proxy/'+encodeURIComponent(u);"
    "}catch(e){return u;}}"
    "var f=window.fetch;"
    # The native fetch has to be called with the window as its receiver.
    # Calling it with an undefined "this" (which is what a module gets)
    # makes it throw "TypeError: Failed to fetch".
    "window.fetch=function(i,o){try{"
    "if(typeof i==='string')i=P(i);"
    "else if(i&&i.url)i=new Request(P(i.url),i);"
    "}catch(e){}"
    "return f.call(window,i,o);};"
    "var x=XMLHttpRequest.prototype.open;"
    "XMLHttpRequest.prototype.open=function(m,u){try{u=P(u);}catch(e){}"
    "return x.call(this||window,m,u);};"
    "}catch(e){}})();"
    "</script>"
)

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
))

# Headers that reveal the proxy/iframe context to upstream servers.
# Stripping these prevents bot-detection systems (like Kaggle's) from
# seeing that the request originates from an iframe inside a proxy.
#
# Only "origin" and "referer" qualify: they carry the proxy's own URL.
# The Sec-Fetch-* and Sec-CH-UA-* headers describe the *browser*, and a
# real browser always sends them.  Dropping them -- or claiming
# "sec-fetch-mode: navigate" on an API call, which no browser ever does --
# is a much stronger bot signal than the headers themselves, and it is
# what earned us a reCAPTCHA challenge page.  They are forwarded
# verbatim instead, with Sec-Fetch-* rewritten to describe a direct visit
# to the target origin.
PROXY_REVEAL_HEADERS = frozenset((
    "origin", "referer",
))

FRAME_BLOCKED_HEADERS = frozenset(("x-frame-options",))

# File extensions that should be redirected to the CDN instead of proxied.
#
# CheerpX's TLS drops large transfers, so proxying multi-megabyte bundles is
# unreliable; the browser fetches them straight from the CDN instead.
#
# Fonts are deliberately NOT in this list.  A stylesheet, a script and an
# image are fetched in no-cors mode, so a redirect to the CDN works for
# them.  A font is always fetched in CORS mode, and Kaggle's CDN sends no
# Access-Control-Allow-Origin for fonts, so a redirected font fails with
# ERR_FAILED.  Fonts are proxied instead, and the proxy adds
# Access-Control-Allow-Origin to every response.
_STATIC_EXTS = frozenset((
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".map", ".webp", ".avif", ".mp4", ".webm",
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

# A ring buffer of recent log lines, for /proxy-log.
_LOG_BUFFER = collections.deque(maxlen=1000)

# --------------------------------------------------------------------------
# Cookie jar
# --------------------------------------------------------------------------
#
# The proxied page is served from the proxy's own origin, and the panel
# renders it in a `credentialless` iframe.  A credentialless iframe has an
# ephemeral storage context: it neither sends cookies nor accepts them, and
# `document.cookie` is always empty inside it.
#
# That breaks any site whose front end reads a cookie to build a request
# header.  Kaggle does exactly that:
#
#     var e = cookie.get("XSRF-TOKEN") || env.XSRF_COOKIE;
#     if (!e) throw Error("An anti-forgery token could not be found.");
#
# so the client threw before it ever sent a request, and the page showed
# its own crash screen.
#
# The proxy therefore keeps the upstream session itself: it records the
# Set-Cookie headers it receives, sends them back upstream, and hands the
# page a read/write view of the same jar through an injected cookie
# bridge.  The page then behaves exactly as it would in a normal browser.

_COOKIE_JAR = {}
_COOKIE_JAR_LOCK = threading.Lock()


def cookie_jar_get(origin):
    """The cookies the proxy holds for ``origin`` (name -> value)."""
    with _COOKIE_JAR_LOCK:
        return dict(_COOKIE_JAR.get(origin) or {})


def cookie_jar_store(origin, raw_values):
    """Record Set-Cookie header values.  Returns the updated jar.

    Only the cookie's name and value are kept; the attributes (Domain,
    Path, Expires, ...) are the upstream site's business and are dropped
    because the cookie is replayed against that same origin anyway.
    """
    with _COOKIE_JAR_LOCK:
        jar = _COOKIE_JAR.setdefault(origin, {})
        for raw in raw_values or ():
            head = (raw or "").split(";", 1)[0].strip()
            if "=" not in head:
                continue
            name, value = head.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            if cookie_is_expired(raw):
                jar.pop(name, None)
            else:
                jar[name] = value
        return dict(jar)


def cookie_is_expired(raw):
    """True when a Set-Cookie value deletes the cookie."""
    low = (raw or "").lower()
    for part in low.split(";")[1:]:
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return int(part.split("=", 1)[1].strip()) <= 0
            except ValueError:
                return False
        if part.startswith("expires="):
            if "1970" in part or "Thu, 01 Jan 1970" in part:
                return True
    return False


def cookie_jar_header(origin):
    """The ``Cookie`` request header for ``origin``, or None."""
    jar = cookie_jar_get(origin)
    if not jar:
        return None
    return "; ".join("{}={}".format(k, v) for k, v in jar.items())


def cookie_jar_string(origin):
    """The jar as a ``name=value; name=value`` string."""
    return cookie_jar_header(origin) or ""


def target_origin_of(url):
    """The ``scheme://host[:port]`` of an absolute URL.

    The cookie jar is keyed by this, so the same function has to be used
    everywhere the origin is needed -- a mismatch would silently split
    one site's session into two jars.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return ""
    origin = "{}://{}".format(parts.scheme, parts.hostname)
    port = parts.port
    if port and port not in (80, 443):
        origin += ":{}".format(port)
    return origin


def log(message: str) -> None:
    print("[WEB_ADJUSTER] " + message, flush=True)
    _LOG_BUFFER.append(message)


def log_buffer_text(limit=400):
    """The last few log lines, for diagnostics.

    The WebVM's terminal is a small scrolling window, so the interesting
    lines are often gone by the time something goes wrong.  This keeps a
    ring buffer so /proxy-log can hand them over.
    """
    lines = list(_LOG_BUFFER)[-limit:]
    return "\n".join(lines) + "\n"


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


def build_upstream_headers(url, method, headers, target_origin=None):
    """The headers to send upstream for ``url``.

    The request has to look like a request the browser would make
    directly to the target, because that is what the target's bot
    detection is looking for.
    """
    if not target_origin:
        target_origin = target_origin_of(url)
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
    # CSRF/anti-forgery middleware commonly checks Origin on state-changing
    # requests.  The browser's Origin is the proxy origin, so do not forward
    # it verbatim; send the upstream target origin instead.  Keep Referer on
    # the upstream origin as well (never leak the proxy URL upstream).
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        outgoing["Origin"] = target_origin
    # The page runs in a credentialless iframe, so the browser cannot hold
    # the upstream's cookies.  Replay the jar the proxy holds for this
    # origin instead; a cookie the browser did send wins, because it is
    # the same cookie arriving by a different route.
    jar_cookie = cookie_jar_header(target_origin)
    if jar_cookie:
        merged = dict(cookie_jar_get(target_origin))
        for name, value in (headers or {}).items():
            if name.lower() != "cookie":
                continue
            for part in value.split(";"):
                if "=" in part:
                    key, val = part.split("=", 1)
                    merged[key.strip()] = val.strip()
        outgoing["Cookie"] = "; ".join(
            "{}={}".format(k, v) for k, v in merged.items())
    # Sec-Fetch-*: forward the browser's real values.  A document request
    # keeps the navigation shape, because that is what a direct visit to
    # the target looks like.  Everything else (the site's own API calls)
    # keeps the shape the browser gave it -- claiming "navigate" for an
    # API call is the sort of thing bot detection looks for.
    #
    # Exactly one set goes out: the copy above keeps the browser's
    # capitalisation, so emitting our own lowercase set as well would
    # send the header twice and the first one would win upstream.
    sec_fetch = {}
    for name, value in (headers or {}).items():
        if name.lower().startswith("sec-fetch-"):
            sec_fetch[name.lower()] = value
    for name in list(outgoing):
        if name.lower().startswith("sec-fetch-"):
            del outgoing[name]
    if not sec_fetch or sec_fetch.get(
            "sec-fetch-dest", "").lower() == "document":
        outgoing["sec-fetch-dest"] = "document"
        outgoing["sec-fetch-mode"] = "navigate"
        outgoing["sec-fetch-site"] = "none"
        outgoing["sec-fetch-user"] = "?1"
    else:
        outgoing.update(sec_fetch)
        # "cross-site" would reveal that the page lives in a frame on
        # another site; the request is presented as a same-site one.
        if sec_fetch.get("sec-fetch-site", "").lower() == "cross-site":
            outgoing["sec-fetch-site"] = "same-origin"
    return outgoing


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

    target_origin = target_origin_of(url)
    if not target_origin:
        raise ValueError("no host in {}".format(url))

    outgoing = build_upstream_headers(url, method, headers, target_origin)

    # HTML can contain a session-bound anti-forgery token without a
    # Set-Cookie response header. A URL-only cache key could replay that
    # token across sessions. Cookie-bearing requests must bypass the cache.
    # The cookie that matters is the one the request actually went out
    # with, which includes the ones the proxy replayed from its own jar.
    has_request_cookie = any(name.lower() == "cookie" for name in outgoing)
    cache_key = (method, url)
    if method == "GET" and not has_request_cookie:
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
            response_type = ""
            for name, value in result[1]:
                if name.lower() == "content-type":
                    response_type = value.lower()
                    break
            # Only cache what is safe to replay: no session cookie went
            # out with the request, the response is not HTML (it can carry
            # a session-bound anti-forgery token), and it is not JSON (the
            # site's own API answers, which are bound to the session that
            # asked for them).
            if (method == "GET" and not has_request_cookie
                    and result[0] < 400
                    and "text/html" not in response_type
                    and "json" not in response_type):
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
    """The proof marker, carrying the page's nonce when there is one."""
    if not nonce:
        return INJECTION_SCRIPT
    return INJECTION_SCRIPT.replace(
        "<script>", '<script nonce="{}">'.format(nonce), 1
    )


def build_runtime_scripts(nonce=None, target_origin=None, proxy_origin=None,
                          cookies=None):
    """The scripts that have to run *before* the page's own JavaScript.

    Two of them:

    * the API routing patch, which sends the page's own absolute API
      calls back through the proxy instead of straight out to the
      internet, where they would be blocked by CORS;
    * the cookie bridge, which gives the page the cookies the proxy
      holds for the target, because a credentialless iframe cannot hold
      cookies itself.

    Both have to be installed before the page's first script runs: the
    page reads its cookie while it initialises, and it may capture
    ``fetch`` at that moment too.

    Every script carries the page's own CSP nonce.  A page's CSP often
    lists a nonce *and* 'unsafe-inline'; the 'unsafe-inline' is ignored
    once a nonce is present, so an injected script without the nonce is
    silently blocked and never runs.
    """
    scripts = []
    if target_origin:
        scripts.append(_API_ROUTING_SCRIPT.replace(
            "__TARGET_ORIGIN__", json.dumps(target_origin)))
    if proxy_origin:
        endpoint = "{}/proxy-cookie?target={}".format(
            proxy_origin.rstrip("/"),
            quote(target_origin or "", safe=""))
        scripts.append(_COOKIE_BRIDGE_SCRIPT.replace(
            "__COOKIE_STRING__", json.dumps(cookies or "")
        ).replace("__COOKIE_ENDPOINT__", endpoint))
    if not scripts:
        return ""
    attr = ' nonce="{}"'.format(nonce) if nonce else ""
    return "".join(script.replace("<script>", "<script{}>".format(attr))
                   for script in scripts)


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
            # The API routing patch and the cookie bridge have to be in
            # place before the page's first script runs: the page reads
            # its cookie while it initialises, and it may capture
            # ``fetch`` at that moment too.
            runtime = build_runtime_scripts(
                nonce, target_origin_of(request_url), proxy_origin,
                cookie_jar_string(target_origin_of(request_url)))
            head_scripts = path_fix + runtime
            # Inject the head scripts BEFORE any other scripts
            # so the SPA router sees the correct pathname and the
            # runtime patches are in place before the page's own code.
            lowered = text.lower()
            head_end = lowered.find("</head>")
            first_script = lowered.find("<script")
            if first_script != -1 and (head_end == -1 or first_script < head_end):
                text = text[:first_script] + head_scripts + text[first_script:]
            elif head_end != -1:
                text = text[:head_end] + head_scripts + text[head_end:]
            else:
                text = head_scripts + text
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


# How long a client connection may sit idle before the proxy gives up on it.
#
# The browser opens connections it may never use -- Chrome speculatively
# connects to an origin it is about to use, and abandons the connection
# when a navigation is superseded or the frame is replaced.  A client
# socket with no timeout leaves the handler thread blocked in recv()
# forever.
#
# That is fatal here rather than merely untidy: CheerpX schedules the
# VM's threads cooperatively, so one thread parked in a socket call can
# stall the whole VM.  The symptom was that the proxy stopped answering
# *everything* -- even a plain /health request from another client --
# as soon as the browser loaded a page through it.  Bounding the wait
# turns that from a permanent hang into a failed connection.
CLIENT_READ_TIMEOUT = 15
CLIENT_WRITE_TIMEOUT = 30
# How long a keep-alive connection waits for the next request.  It has to
# outlast the browser's own queueing: the page fires its API calls in
# parallel, and the ones that wait for a free connection are the ones
# that must not be dropped.
KEEPALIVE_TIMEOUT = 45


def recv_until(sock, terminator, limit=65536, initial=b""):
    """Read until ``terminator`` appears.  Returns (data, leftover).

    The leftover matters on a keep-alive connection: one read can pick up
    the end of the headers, the body and the start of the next request,
    and those bytes must not be thrown away.
    """
    data = initial
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
    if terminator in data:
        head, _, rest = data.partition(terminator)
        return head + terminator, rest
    return data, b""


def recv_exactly(sock, count, initial=b""):
    """Read exactly ``count`` bytes.  Returns (data, leftover)."""
    data = initial[:count]
    rest = initial[count:]
    while len(data) < count:
        try:
            chunk = sock.recv(min(65536, count - len(data)))
        except OSError as exc:
            raise HttpError("recv failed: {}".format(exc))
        if not chunk:
            break
        data += chunk
    return data, rest


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


def parse_request(sock, initial=b""):
    """Parse one request.  Returns (request, leftover_bytes)."""
    raw, leftover = recv_until(sock, b"\r\n\r\n", initial=initial)
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
            body, leftover = recv_exactly(sock, min(length, MAX_BODY),
                                         initial=leftover + rest)
        else:
            leftover += rest
    else:
        leftover += rest
    return Request(method, target, version, headers, body), leftover


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
        #
        # The write must not have a socket timeout on it.  Python
        # implements a socket timeout with select(), CheerpX does not
        # implement select() properly, and the write comes out truncated:
        # the browser receives a broken TLS stream and reports
        # ERR_SSL_PROTOCOL_ERROR on a response the proxy logged as sent
        # in full.  A watchdog bounds the write instead -- the same
        # mechanism the upstream fetch uses.
        try:
            sock.settimeout(None)
        except OSError:
            pass
        timer = _watchdog(sock, CLIENT_WRITE_TIMEOUT)
        try:
            sent = 0
            while sent < len(payload):
                chunk = payload[sent:sent + SEND_CHUNK]
                written = sock.send(chunk)
                if not written:
                    break
                sent += written
        finally:
            timer.cancel()
        log("SEND_OK status={} bytes={}".format(status, len(payload)))
        # Let the client read the response before the socket goes away.
        # Closing straight after a large write truncates it: the browser
        # then reports "Failed to fetch" even though the server logged
        # SEND_OK.  A keep-alive connection stays open instead.
        if close:
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

    def keep_alive(self):
        """Whether this connection may carry another request.

        HTTP/1.1 keeps the connection open unless the client says
        otherwise.  Honouring that matters here: the browser fires a
        page's API calls in parallel, and if every response closes the
        connection the browser has to open a fresh one for each -- which
        means a fresh TLS handshake each time (expensive in the VM) and a
        queue of requests waiting on connections the server then closes
        out from under them.  The page's calls failed with
        ERR_EMPTY_RESPONSE for exactly that reason.
        """
        request = self.parsed
        if request is None:
            return False
        token = (request.header("connection") or "").lower()
        if "close" in token:
            return False
        if (request.version or "HTTP/1.1").upper() == "HTTP/1.0" \
                and "keep-alive" not in token:
            return False
        return True

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
                      close=not self.keep_alive(),
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

        # Record the upstream's cookies before the page is built, so the
        # cookie bridge hands the page the session it has just been given.
        set_cookies = [value for name, value in upstream_headers
                       if name.lower() == "set-cookie"]
        if set_cookies:
            origin = target_origin_of(url)
            jar = cookie_jar_store(origin, set_cookies)
            # Names only -- a cookie value is a credential.
            log("COOKIE_STORED origin={} names={}".format(
                origin, ",".join(sorted(jar))))

        status, out_headers, out_body = transform_response(
            status, upstream_headers, upstream_body, url, self.proxy_origin()
        )
        log("PROXIED status={} url={} bytes={}".format(status, url, len(out_body)))
        send_response(self.request, status, out_headers, out_body,
                      close=not self.keep_alive(),
                      client=self.client_label())

    # -- dispatch --------------------------------------------------------
    def handle(self):
        # A keep-alive connection carries one request after another.
        leftover = b""
        first = True
        while True:
            timeout = CLIENT_READ_TIMEOUT if first else KEEPALIVE_TIMEOUT
            first = False
            try:
                if timeout != CLIENT_READ_TIMEOUT:
                    try:
                        self.request.settimeout(timeout)
                    except OSError:
                        pass
                # The socket timeout is not reliable here (Python implements
                # it with select(), which CheerpX does not implement), so
                # the watchdog is what actually bounds the wait.
                timer = _watchdog(self.request, timeout)
                try:
                    request, leftover = parse_request(self.request, leftover)
                finally:
                    timer.cancel()
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
                log("HANDLER_ERROR error={}: {}".format(
                    type(exc).__name__, str(exc)[:300]))
                return

            if not self.keep_alive():
                return
            # The next request has to arrive before the socket times out.
            # The timeout is what stops an idle connection from holding a
            # thread (and, under CheerpX, the whole VM) indefinitely.
            log("KEEP_ALIVE waiting for the next request")

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
        if path == "/proxy-cookie":
            # The cookie bridge in the page calls this when the page writes
            # a cookie, so the proxy can hold it and send it upstream.
            origin = params.get("target") or ""
            raw = (request.body or b"").decode("utf-8", "replace").strip()
            if not origin or "=" not in raw:
                self._send(400, "text/plain; charset=utf-8", b"Bad request\n")
                return
            jar = cookie_jar_store(origin, [raw])
            # Names only -- a cookie value is a credential.
            log("COOKIE_WRITTEN origin={} names={}".format(
                origin, ",".join(sorted(jar))))
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/proxy-status":
            with STATE_LOCK:
                payload = dict(_state)
            payload["wrapper_port"] = WRAPPER_PORT_INPROC
            payload["forward_port"] = ADJUSTER_PORT
            self._send_json(200, payload)
            return
        if path == "/proxy-log":
            self._send(200, "text/plain; charset=utf-8",
                       log_buffer_text().encode("utf-8"))
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
            # Never wait on a client forever.  See CLIENT_READ_TIMEOUT.
            try:
                client.settimeout(CLIENT_READ_TIMEOUT)
            except OSError:
                pass
            threading.Thread(
                target=self._handle_client,
                args=(client, addr),
                daemon=True,
                name="proxy-client-{}".format(addr[0]),
            ).start()

    def _handle_client(self, client, addr):
        started = time.time()
        try:
            if self.ssl_context is not None:
                # The handshake happens in this thread so that one slow
                # client cannot block the accept loop.
                client = self.ssl_context.wrap_socket(client, server_side=True)
                log("TLS_HANDSHAKE_OK peer={} ms={}".format(
                    addr[0], int((time.time() - started) * 1000)))
            handler = self.handler_class(client, addr, self)
            handler.handle()
        except Exception as exc:
            log("CONNECTION_ERROR peer={} error={}: {}".format(
                addr[0], type(exc).__name__, str(exc)[:200]))
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
