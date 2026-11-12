# AI Agent Handoff / Operating Notes

> Keep this section current. These notes let an AI agent continue the work without asking the user to repeat repository context.

## Where things stand

| Goal | State | Evidence |
|---|---|---|
| 1 — Tailscale auto-connect | **DONE** | The node gets a 100.x address on every boot and the address is printed in the WebVM terminal. |
| 2 — Telnet access to the VM | **DONE** | `plink.exe -raw -P 2323 <ip>` gives a root shell. |
| 3 — The proxy works | **DONE, VERIFIED** | A real HTTPS page is fetched through the VM, `x-frame-options` is stripped and the proof script is injected and executes. |
| 4 — Kaggle in a proxy iframe | **DONE, VERIFIED** | `http://<ip>:28082/` shows the real Kaggle page in an iframe and the parent can read its DOM (`sameOrigin: true`). |
| 4b — that iframe **inside the GitHub Pages WebVM page** | **IN PROGRESS** | The Service Worker proxy bridge is written and deployed. The one unconfirmed point is whether a Service Worker may fetch the VM's plain-`http` proxy — the `/webvm/httpsproxy/diag` route answers that in one command (see "Getting the iframe onto the GitHub Pages WebVM page"). If it may not, the fallback is an https origin for the proxy: the WebVM already serves one on `:28443`, and enabling **Tailscale Serve** on the tailnet gives a publicly trusted one with nothing to install. |

**The single most useful next command** (after a fresh browser reset, so
the current Service Worker is loaded):

```js
fetch('/webvm/httpsproxy/diag?ip=' + '<tailscale-ip>').then(r => r.text()).then(console.log)
```

It reports exactly what the worker can fetch, which decides between the
Service Worker route and the Tailscale Serve route.

## Active Goals (in priority order)

### Goal 1: Fix Tailscale auto-connect (DONE)

**Status:** Deployed and working. Auto-connect fires 2 seconds after CheerpX init, calls `cx.networkLogin()`. Gets a 100.x.x.x Tailscale IP each boot.

**Key files:**
- `src/lib/network.js` line 4: hardcoded `DEFAULT_AUTH_KEY`
- `src/lib/WebVM.svelte` `tryAutoConnect()` (line ~393): fires 2s after cx init, calls `cx.networkLogin()`
- `src/lib/WebVM.svelte` `handleConnect()` (line ~416): early-return with authKey

### Goal 2: Lightweight telnet listener for direct AI access (DONE)

**Status:** Deployed and verified working. Telnet shell starts on boot, reachable via Tailscale.

**How it works:**
- WebVM boots → `pi.py` fetches `pi_impl.py` from main → `main()` starts the proxy (in-process threads) and the telnet shell
- `telnet_shell.py` listens on `0.0.0.0:2323` (raw TCP, no telnet negotiation)
- Each connection spawns `/bin/sh` via subprocess, bridged to the socket with threads
- Tailscale auto-connects → gets a 100.x.x.x address
- AI agent connects: `plink.exe -raw -P 2323 <tailscale-ip>`
- The current address is printed in the WebVM terminal on boot (see Goal 3,
  "Printing the correct Tailscale IP on boot")

**Verified:** TCP connection to port 2323 over Tailscale returns a shell prompt, and `root@~#` commands run inside the VM.

**Key files:**
- `examples/python3/telnet_shell.py` — the daemon (fetched from main at boot, local fallback)
- `examples/python3/pi_impl.py` `main()` — starts the daemon
- `examples/python3/pi.py` — boots the runtime; the proxy is started in-process by `pi_impl.py`

**CheerpX boot notes:**
- `pi_impl.py` is fetched from GitHub `main` at runtime, so fixes take effect on browser reset without a full deploy.
- `pi.py` is baked into the disk image — changes require a full rebuild via GitHub Actions deploy.
- Watch for `import` scoping in `main()`: a local `import time` anywhere in the function causes Python to treat `time` as local throughout, breaking earlier `time.time()` calls with `UnboundLocalError`.
- **Never exec a helper module into `globals()`.** `telnet_shell.py` has an
  `if __name__ == "__main__":` block that ends in `threading.Event().wait()`.
  Because `globals()["__name__"]` is `"__main__"`, exec'ing it there ran that
  block and blocked `main()` forever. Use a dedicated namespace with
  `__name__` set to something else.

### Goal 3: Make the proxy work  (DONE — VERIFIED)

**Status:** The old design was broken in two independent ways. Both are now
fixed in code **and verified against the live WebVM** — see "Verification
status" below for the measured evidence.

#### What was broken

1. **`http.server` / `socketserver` misbehave under CheerpX.**
   The in-process wrapper was built on `socketserver.ThreadingHTTPServer` +
   `BaseHTTPRequestHandler`. Observed in the WebVM terminal:

   ```
   [WEB_ADJUSTER] WRAPPER_HTTP "GET /health HTTP/1.0" 200 -
   OSError: [Errno 107] Transport endpoint is not connected
     launcher.py line 414 in do_GET -> line 372 in _send
     -> http/server.py end_headers -> flush_headers -> wfile.write
     -> socketserver.py write -> self._sock.sendall(b)
   ```

   `send_response()` logged `200`, then the actual write to the socket
   failed with `ENOTCONN`, so **the client never received a reply**. The
   server's `handle_error` only swallowed `ConnectionResetError`,
   `BrokenPipeError` and `ConnectionAbortedError`, so `OSError` produced a
   full traceback on every affected request.

2. **The `adjuster` pip package could never work.**
   The old design pip-installed the `adjuster` package at boot and ran it as
   a separate long-lived process on port 28081. Measured: **port 28081 was
   never open** on the running VM while 2323, 28080 and 28082 were. The
   README already noted that CheerpX starves long-lived subprocesses;
   installing a package at boot on top of that was never going to be
   reliable. `/tmp/web-adjuster.log` was also empty, i.e. the subprocess
   produced no output at all.

#### The fix

`examples/python3/web_adjuster/launcher.py` was rewritten as a **pure-Python
proxy that runs inside the WebVM process**:

- **Raw sockets + threads instead of `socketserver`.** This is the exact
  shape that `telnet_shell.py` already proved works inside WebVM (plain
  `socket.accept()` loop, one thread per connection). The wrapper now
  parses the request line/headers/body itself and writes the response with
  a single `sendall`, which cannot hit the `ENOTCONN` path.
- **No pip install, no helper process.** The proxy is generic and forwards
  any absolute `http`/`https` URL; nothing is installed at boot and there is
  no subprocess for CheerpX to starve.
- **Two listeners, both in-process:**
  - `:28082` wrapper — `/health`, `/proxy-status`, `/` (iframe page),
    `/proxy/<url>` (reverse proxy to an absolute URL)
  - `:28081` forward proxy — absolute-URI request lines
    (`GET http://example.com/`), falling back to `:28083` if `:28081` is
    still held by the stale disk-image launcher
- **Ports `:28080` / `:28081` are intentionally avoided** by the in-process
  proxy: the baked disk image still ships the *old* `launcher.py`, and the
  baked `pi.py` still launches it as a subprocess. Those two ports belong
  to that legacy process. `pi.py` has been updated to stop spawning it,
  but `pi.py` only changes after a full image rebuild.
- `pi_impl.py` `start_web_adjuster_sidecar()` now starts **both** threads
  and logs `WRAPPER_STARTED_IN_THREAD` / `FORWARD_PROXY_STARTED`.
- `pi_impl.py` `_run_endpoint_tests()` now fetches a **real page through
  the proxy** (`PROXY_SELF_TEST_URL = "https://example.com/"`), and prints
  `PROXY_SELF_TEST_PASS` only when the status is 200 *and* the injected
  marker is present.

#### Response rewriting (what makes the iframe work)

Applied to every proxied response:

| Header / body | Handling |
|---|---|
| `x-frame-options` | removed entirely |
| `content-security-policy` | `frame-ancestors` directive removed; `sandbox` dropped; `'unsafe-inline'` added to `script-src` so the injected script can run |
| `set-cookie` | `Domain`, `Secure`, `SameSite`, `Partitioned` removed so cookies bind to the proxy origin |
| `location` | rewritten to `/proxy/<encoded-url>` |
| HTML body | absolute `http(s)://` and protocol-relative URLs rewritten to `/proxy/<encoded-url>`; `srcset` and CSS `url(...)` handled too |
| HTML body | `INJECTION_SCRIPT` appended before `</body>` |

#### The proxy MUST keep DOM access  (important constraint)

This is the reason the proxy exists, not just a smoke test:

- The wrapper page and the iframe are served from the **same origin**
  (`http://<tailscale-ip>:28082`), so the parent page can read and write the
  iframe's DOM (`frame.contentDocument`). That is what lets an agent drive
  the proxied site.
- The injected script must actually **execute** inside the iframe, otherwise
  the proxied page is just a picture and there is nothing to drive. This is
  why CSP is relaxed (`sandbox` removed, `'unsafe-inline'` allowed) and why
  the marker element plus the `data-webvm-adjuster-injected` attribute on
  `<html>` are the proof.
- The proof check lives in the wrapper page's `checkInjection()` function
  and must report `JS/DOM INJECTION CONFIRMED`.

#### Verification status

**Goal 3 is VERIFIED.** Measured against the live WebVM on `2026-01-14`.

| Item | Status |
|---|---|
| `examples/python3/test_proxy_local.py` — full local suite on the host | **PASS** (`/health`, `/proxy-status`, iframe page, proxied `https://example.com/` returns 200 + title `Example Domain` + marker injected + no `x-frame-options`, forward-proxy absolute-URI mode) |
| Wrapper answers `/health` inside WebVM on `:28082` | **PASS** — `HTTP/1.1 200 OK` + `ok` |
| Forward proxy `:28081` inside WebVM | **PASS** — absolute-URI request `GET http://example.com/` → `HTTP/1.1 200 OK`, marker injected |
| Real site fetched through the proxy **inside WebVM** | **PASS** — `https://example.com/` → `200`, `content-type: text/html; charset=utf-8`, title `Example Domain`, 1106 bytes, `x-frame-options` absent, marker injected |
| Kaggle fetched through the proxy inside WebVM | **PASS** — `https://www.kaggle.com/` → `200`, 21206 bytes, `x-frame-options` absent, marker injected, absolute URLs rewritten to `/proxy/<encoded>` (e.g. `<base href="/proxy/https%3A%2F%2Fwww.google.com%2Frecaptcha%2F...">`). From a bare `urllib` request Kaggle answers with its **reCAPTCHA "Checking your browser"** interstitial; from the **browser** it answers with the **real Kaggle page** |
| `PROXIED status=200` lines in the WebVM terminal | **PASS** — confirms the request went through the in-process proxy |
| `x-frame-options` stripped | **PASS** — header absent from the proxied response |
| Script injection | **PASS** — `WEBVM_ADJUSTER_INJECTED` and `data-webvm-adjuster-injected` both present in the proxied body |
| Script injection on a **CSP-nonce** page | **PASS** — a nonce in `script-src` makes browsers ignore `'unsafe-inline'`, so the injected `<script>` now carries the page's own nonce. Without that, Kaggle's page loaded but the marker never appeared |
| `Errno 107` traceback | **GONE** — no `socketserver` is used any more |
| Upstream read bounded in time | **PASS** — CheerpX has no `select()`, so Python socket timeouts never fire; a watchdog thread closes the socket instead. The upstream fetch completes in ~2.4 s instead of hanging forever |
| Connection reuse | **PASS** — keep-alive connections are pooled per host (a TLS handshake per request is far too slow under CheerpX) |
| Response cache | **PASS** — 120 s cache for GET responses without `Set-Cookie` |
| Compressed bodies | **PASS** — the upstream request forces `Accept-Encoding: identity`; the browser's `gzip, deflate, br` would otherwise make the rewriting corrupt the body |
| HTTPS listener on `:28443` | **PASS** — `TLS_WRAPPER_LISTENING port=28443`, and the chain verifies against the development CA (`VERIFIED CHAIN OK -> 200 ok`) |
| Kaggle **app** loaded and usable in the iframe | **PASS** — `title: "Kaggle: The World's AI Proving Ground"` |
| JS/DOM injection confirmed inside the iframe | **PASS** — `JS/DOM INJECTION CONFIRMED: marker element present = true, documentElement attribute = true` |
| DOM access from the parent page | **PASS** — `sameOrigin: true`; the parent reads `frame.contentDocument` |
| In-VM boot self-test | **OFF by default** (`WEBVM_SELFTEST=1` enables it) — see below |

#### Additional bug found while verifying — `main()` was blocked

`pi_impl.py` exec'd `telnet_shell.py` into `globals()`. Because
`globals()["__name__"]` is `"__main__"`, that ran `telnet_shell.py`'s
`if __name__ == "__main__":` block, which ends in
`threading.Event().wait()` — an **infinite block**. `main()` never
returned, so everything after the telnet setup (the "Wrapper: ..." banner
and the whole `_run_endpoint_tests()` self-test) never ran. The symptom
was a terminal that showed the telnet lines and then nothing else.

Fixed by exec'ing `telnet_shell.py` into its own namespace with
`__name__ = "webvm_telnet_shell"`, then calling
`telnet_ns["start_telnet_shell"]()` explicitly.

#### Printing the correct Tailscale IP on boot

The Tailscale address changes on **every boot**, so it can never be
hard-coded. Two independent mechanisms now print it:

1. **Browser side** (`src/lib/network.js` + `src/lib/WebVM.svelte`) —
   the netmap already reports this node's address
   (`map.self.addresses[0]`). A new reactive store `currentIpStore` is
   exported from `network.js`, and `WebVM.svelte` writes a banner into the
   xterm terminal as soon as it is known:

   ```
   ====================================================================
   TAILSCALE_IP 100.115.174.109
   TELNET   plink.exe -raw -P 2323 100.115.174.109
   PROXY    http://100.115.174.109:28082/
   FORWARD  http://100.115.174.109:28081/
   ====================================================================
   ```

   This needs a **Pages rebuild** (`redeploy`), because `src/` is compiled
   into the site.

2. **VM side** (`examples/python3/pi_impl.py`) — `discover_tailscale_ip()`
   finds the address from inside the VM with three fallbacks, in order:
   - **UDP route trick** (primary): a UDP `connect()` to `100.100.100.100`
     (Tailscale's Quad100 service) sends no packets but makes the kernel
     reveal the local address it would use, which is this node's Tailscale
     IPv4. This works even though `ip`, `hostname -I` and `ps` do not.
   - `ifconfig` (the image ships `/usr/sbin/ifconfig`, but it prints
     **nothing** under CheerpX, so this fallback is effectively useless)
   - `/proc/net/fib_trie` or `/proc/net/tailscale`

   The banner is printed from a daemon thread, so it never delays the
   proxy or the telnet shell. This one only needs a **browser reset**,
   because `pi_impl.py` is fetched from `main` at runtime.

   **Verified:** after a reset the WebVM terminal showed

   ```
   ====================================================================
   TAILSCALE_IP 100.105.92.77
   TELNET   plink.exe -raw -P 2323 100.105.92.77
   PROXY    http://100.105.92.77:28082/
   FORWARD  http://100.105.92.77:28081/
   ====================================================================
   ```

   and that address was the one the host could actually reach — the
   banner matched `tailscale status`.

   **The in-VM UDP probe did NOT work** on this image
   (`TAILSCALE_IP_NOT_FOUND timeout=120s`), so the browser-side banner is
   the mechanism to rely on. The VM-side probe is kept as a fallback.

#### Note — the service worker caches the site bundle

After a Pages deploy the running tab keeps serving the **old** bundle from
the service worker. The symptom is "the deployed code contains the change
but the page does not behave differently". Clear it before testing:

```js
(async () => {
  for (const reg of await navigator.serviceWorker.getRegistrations())
    await reg.unregister();
  for (const key of await caches.keys())
    await caches.delete(key);
})();
```

then reload.


### Goal 4: Load Kaggle via proxy in iframe (browser integration)

**Status:** The proxy iframe works and keeps DOM access. Verified in the
browser against the live WebVM:

```
title      : "Kaggle: The World's AI Proving Ground"
status     : "JS/DOM INJECTION CONFIRMED: marker element present = true,
              documentElement attribute = true, marker text = WEBVM_ADJUSTER_INJECTED"
sameOrigin : true        <- the parent can read and drive the frame's DOM
markerFound: true        <- the injected script executed inside the frame
```

**How it is reached today:** open the WebVM's own proxy page,
`http://<tailscale-ip>:28082/`, which renders the target in an iframe. The
parent page and the iframe are the **same origin**, which is what keeps DOM
access working, and `http://` on an `http://` page is not mixed content.
`?target=<url>` selects a different target (default
`https://www.kaggle.com/`).

---

## Getting the iframe onto the GitHub Pages WebVM page (the open part)

The user wants the proxied page rendered **in the same page as the WebVM**,
i.e. inside `https://scatterp2.github.io/webvm/`.

### The obstacle, measured

That page is served over `https://`, and Chrome hard-blocks an `http://`
frame there:

```
Mixed Content: The page at 'https://scatterp2.github.io/webvm/' was loaded
over HTTPS, but requested an insecure frame 'http://100.x.x.x:28082/'.
This request has been blocked; the content must be served over HTTPS.
```

The request is not even sent — the network log shows **no request at all**.
So **CORS headers cannot fix this**: CORS is evaluated after a request
succeeds, mixed content is evaluated before it is made. The site's Service
Worker does not bypass it either — a page-level `fetch('http://...')` failed
with the same mixed-content error.

What CORS *does* fix is the **read**, and that is what makes the
`srcdoc` approach work: the proxy sends `Access-Control-Allow-Origin: *`,
the page reads the proxied HTML, and injects it with `srcdoc`. A `srcdoc`
frame inherits the host page's origin, so **DOM access survives the
cross-origin hop**.

### The design that is implemented: a Service Worker proxy bridge

The site already registers a Service Worker (`serviceWorker.js`) that
intercepts **every** request on its origin. That is the natural place to
put a proxy, because the worker runs in its own context and is not the
document:

| Route (all on the WebVM's own origin) | What it does |
|---|---|
| `GET /webvm/httpsproxy?base=<origin>&target=<url>` | Fetches `<origin>/proxy/<url>` through the WebVM's proxy and returns the document |
| `GET /webvm/httpsproxy/raw?base=<origin>&path=<path>` | Fetches one proxied resource (used for the document's own sub-resources) |
| `GET /webvm/httpsproxy/diag?ip=<address>` | Diagnostic: reports what the worker itself can fetch, which tells a mixed-content block apart from an unreachable host |

The document that comes back has its proxy URLs rewritten from
`http://<vm>:28082/proxy/...` to the bridge's own same-origin form, so the
sub-resources of the injected document are fetched same-origin too and
never trip mixed content.

The WebVM page's Proxy button then does:

```js
const res  = await fetch("/webvm/httpsproxy?base=" + encodeURIComponent(proxyHttpBase)
                          + "&target=" + encodeURIComponent(proxyTarget));
proxyFrameEl.srcdoc = await res.text();
```

`proxyHttpBase` is `http://<tailscale-ip>:28082`, taken from the netmap
because the address changes on every boot.

### What is verified and what is not

| Part | State |
|---|---|
| The bridge routes are served by the worker | **VERIFIED** — a request to `/webvm/httpsproxy?base=…` produced the worker's own `502 proxy bridge failed: …` response, so the worker really is handling the route |
| A worker may fetch a plain `http://` URL | **NO — mixed content applies to worker-initiated fetches too.** The diagnostic (`/webvm/httpsproxy/diag?ip=…`) settles it: `http://<vm>:28082/health` was rejected in **1 ms**, i.e. before any network activity, exactly like a document-level mixed-content block |
| A worker may fetch an `https://` URL | **YES, but only with CORS approval.** `https://example.com/` was attempted for 602 ms and then rejected, because the page is cross-origin isolated (`COEP: require-corp`) and that response carries no `Access-Control-Allow-Origin` |
| The VM's own `https://` proxy on `:28443` | rejected the same way, because `/health` had no CORS headers — **now fixed** (see below) |
| The bridge, end to end | **the fix is deployed; the re-verification is the next step** |
| The `srcdoc` injection and DOM access | **VERIFIED** in the VM's own proxy page (`sameOrigin: true`, `JS/DOM INJECTION CONFIRMED`) |
| The `srcdoc` injection and DOM access **from the GitHub Pages page** | depends on the bridge fetch succeeding |

#### The fix that follows from the diagnostic

Because the page is cross-origin isolated, the worker's fetches have to be
CORS requests against a CORS-approved server, and the responses it hands
back have to be embeddable:

- the proxy now sends `Access-Control-Allow-Origin: *` and
  `Cross-Origin-Resource-Policy: cross-origin` on **every** response, not
  only on proxied ones (previously `/health` and `/proxy-status` had
  neither, so a CORS fetch of them was rejected);
- the worker's bridge fetch uses `mode: "cors"` explicitly — the default
  in a worker can fall back to a no-cors request, which a cross-origin
  isolated context rejects for lack of a CORP header;
- the responses the worker constructs carry `CORP`/`COEP` headers.

**The `http://` leg is still blocked by mixed content**, so the bridge
cannot fetch the VM's plain-http proxy. The remaining options are:

1. **Let the browser fetch the VM's `https://` proxy** — the VM already
   serves one on `:28443` with the development certificate, and the proxy
   now sends CORS headers, so a `mode: "cors"` fetch from the worker
   should succeed **if the browser trusts that certificate**.
2. **Publish the proxy with a publicly trusted certificate** — enable
   Tailscale Serve on the tailnet (one click) and
   `tailscale serve --bg http://127.0.0.1:28443`; then the worker fetches
   `https://<node>.tailf987e.ts.net/...`, which is a normal CORS request
   to a trusted origin. **This is the clean route** — nothing has to be
   installed on the laptop and the certificate is a real one.
3. **Make the worker's fetch of the plain-http proxy possible** — not
   possible while the page is cross-origin isolated and the target is
   `http://`.

The diagnostic route is the fastest way to check any of these:

```
fetch('/webvm/httpsproxy/diag?ip=' + '<tailscale-ip>').then(r => r.text()).then(console.log)
```

It reports, for each target, whether `cors` or `no-cors` worked, and how
long it took (a rejection in ~1 ms means mixed content; a rejection after
a network round trip means CORS/COEP).

### The other route: an https origin for the proxy

If the worker cannot fetch plain http, the proxy needs an **https** origin
whose certificate the browser trusts. Two implementations exist:

| Route | Needs | Notes |
|---|---|---|
| The WebVM's own TLS listener on `:28443` | the development CA trusted by the browser | The cert and key live on the **WebVM** (fetched from `main` at boot, never from the laptop). The chain verifies: `VERIFIED CHAIN OK -> 200 ok`. `examples/python3/install_dev_ca.py` adds the CA without the UI prompt that `certutil`/`Import-Certificate` want — although on this machine even the Win32 API call is blocked by the protected-root prompt ("UI is not allowed in this operation"). |
| **Tailscale Serve** on the tailnet | Serve enabled on the tailnet (one click) and `tailscale serve --bg http://127.0.0.1:28443` | A publicly trusted certificate, so **no CA has to be trusted at all** and nothing has to be installed on the laptop. The tailnet currently reports `Serve is not enabled on your tailnet`; enabling it is a single visit to the link `tailscale serve` prints. |

`examples/python3/proxy_forward.py` publishes the proxy on
`127.0.0.1:28443` and re-resolves the live WebVM automatically, so a
`tailscale serve` target stays valid even though the WebVM's address
changes on every boot.

### Service Worker update gotcha (cost a lot of time)

The browser keeps the Service Worker script for a long time, and
**unregistering does not release control until the page reloads**. Worse,
`serviceWorker.js` only registers itself when the page is *not* already
cross-origin isolated, so on an already-isolated page the registration
silently does not happen and the stale worker keeps serving.

To pick up a new worker:

1. `navigator.serviceWorker.getRegistrations()` → `unregister()` on each
2. `caches.keys()` → `caches.delete()` on each
3. **Reload the page**
4. Check `navigator.serviceWorker.controller.scriptURL`

`src/app.html` now loads `serviceWorker.js?v=3` so a deployed change gets a
new worker identity instead of being served from the stale cache. **Bump
the `?v=` number whenever `serviceWorker.js` changes.**

If the page is already cross-origin isolated the automatic registration
does not happen at all, so after a deploy either unregister + reload, or
register the new worker explicitly from the console:

```js
const reg = await navigator.serviceWorker.register('/webvm/serviceWorker.js?v=4', {scope: '/webvm/'});
await reg.update();
```

and then reload the page so the new worker becomes the controller.

### How to test the bridge

```
1. Get the WebVM's Tailscale IP (terminal banner, or discover.py)
2. In the browser console of the WebVM page:
     fetch('/webvm/httpsproxy/diag?ip=' + '<ip>').then(r => r.text()).then(console.log)
   -> shows exactly what the worker can and cannot fetch
3. Then:
     fetch('/webvm/httpsproxy?base=' + encodeURIComponent('http://<ip>:28082')
           + '&target=' + encodeURIComponent('https://example.com/'))
       .then(r => r.text()).then(console.log)
4. Click the "Proxy" button on the WebVM page
```

**How to test the already-working path:**
1. Get the WebVM Tailscale IP from the terminal banner (or `discover.py`).
2. Open `http://<tailscale-ip>:28082/` in the browser.
3. Check the page status text for `JS/DOM INJECTION CONFIRMED`.
4. Check the WebVM terminal for `PROXIED status=200` lines.

**Challenges:**
- **Kaggle's bot check.** A bare `urllib` request through the proxy gets
  Google's "Checking your browser - reCAPTCHA" interstitial. From the
  **browser**, with the browser's own headers forwarded, the real Kaggle
  page loads — that is what the verification above shows.
- Kaggle authentication: cookies are rewritten to the proxy origin, so
  logging in through the proxied page should work, but it must be tested
  explicitly — never assumed.
- Kaggle's SPA makes many additional requests; all absolute URLs are
  rewritten to route back through the proxy, but relative and API calls
  still need checking.
- Loading a heavy SPA in the same page as the WebVM makes the page
  unresponsive (the CheerpX runtime and the SPA compete for the same
  renderer). Start with a light target.

**What NOT to do:**
- Don't try to extract the Kaggle Jupyter proxy token through RX/TX — that
  approach is blocked (see history below).
- Don't assume Kaggle authentication works through the proxy — test it
  explicitly.

## Findings and suspicions (not yet proven)

Recorded so the next agent does not re-derive them. **These are
suspicions and partial observations, not confirmed root causes.**

| # | Suspicion | Evidence | Confidence |
|---|---|---|---|
| 1 | `socketserver.BaseHTTPRequestHandler` is unsafe under CheerpX; the header write fails with `Errno 107 ENOTCONN` and the client gets nothing | Terminal traceback + client-side timeouts on `28080`; `telnet_shell.py` (raw sockets) works | **High** — fixed by replacing it with raw sockets |
| 2 | CheerpX starves long-lived subprocess servers, so a subprocess-bound HTTP server never serves | Port 28081 never opened; `/tmp/web-adjuster.log` empty; README already recorded this for the old launcher | **High** — fixed by running the proxy in-process |
| 3 | Installing the `adjuster` pip package at boot is unreliable in the Debian i386 image | No venv appeared; no log output | Medium — now moot, the dependency is gone |
| 4 | The WASM runtime crashed once with `RuntimeError: function signature mismatch` / `Uncaught TypeError: e is not a function` | Browser console | **Low / unexplained.** Did not correlate with a specific action. The VM kept serving afterwards. Worth watching for — if it recurs, capture what ran immediately before it |
| 5 | `fuser -k` against the legacy launcher may be what destabilises the old ports, and it risks the WASM runtime | `fuser` is invoked at boot by both `pi.py` and `telnet_shell.py` | Low. `pi_impl.py` no longer calls it; the baked `pi.py` still does until the next image rebuild |
| 6 | The reset generation must be **>=** the generation in the `reset` file on `main`, otherwise no reset fires | Console said `Reset signal already handled for generation 1734` while the URL carried `?reset=1715` | **High** — use the current generation from the `reset` file |
| 7 | The WebVM Tailscale IP changes on every boot, so a hard-coded IP in a test script goes stale immediately | `100.98.161.16` → gone; `100.71.2.31` → gone; `100.122.41.24` → gone; `100.80.88.9` → gone; currently `100.115.174.109` | **High** — always run `discover.py` first; the IP is now also printed on boot |
| 8 | The browser tab can fail to boot the app with `Failed to fetch dynamically imported module` (SvelteKit chunk load failure), leaving a black screen | Browser console on the first load of the session | Medium. A plain re-navigation fixed it. Related to the `cross-world service worker resource mismatch` warning |
| 9 | `ifconfig` exists at `/usr/sbin/ifconfig` but prints **nothing** under CheerpX; `/proc/net/` and `/sys/class/net/` are not populated either. The only reliable in-VM way to learn the Tailscale address is the UDP-route trick to `100.100.100.100` | `command -v ifconfig` → `/usr/sbin/ifconfig`, but `ifconfig tailscale0`, `ifconfig -a \| grep 'inet 100'`, `ls /proc/net/` and `ls /sys/class/net/` all produced no output | **High** — used as the primary method |
| 10 | `pi_impl.py` exec'ing `telnet_shell.py` into `globals()` runs its `__main__` block and blocks `main()` forever on `threading.Event().wait()` | Terminal showed the telnet lines and then nothing; no "Wrapper:" banner, no `ENDPOINT_TEST` | **High** — fixed by using a dedicated namespace |
| 11 | A reset generation **older** than the one in the `reset` file does not fire a reset, so a stale `?reset=` in the URL silently does nothing | Console: `Reset signal already handled for generation 1734` while the URL carried `?reset=1715` | **High** |
| 12 | The Pages deploy workflow only runs when the `redeploy` file changes, so `src/` changes are not published by an ordinary push | `.github/workflows/deploy.yml` `paths: - 'redeploy'` | **High** — touch `redeploy` after changing anything under `src/` |
| 13 | The **browser's own network** was unavailable for a while, which made DNS fail inside the WebVM (`gaierror: [Errno -3] Temporary failure in name resolution`) with the browser console showing failed control-plane WebSockets and failed fetches at the same time | The user confirmed the machine had no internet at that moment | **High** — it was the missing internet, not the VM. The proxy now retries the upstream fetch (3 attempts) instead of returning 502 |
| 14 | The service worker caches the SvelteKit bundle, so a freshly deployed `src/` change is **not** picked up until the service worker is unregistered and the caches are cleared | The deployed bundle contained `TAILSCALE_IP` but the running page did not; after unregistering the service worker and deleting the caches the banner appeared | **High** — always clear the service worker after a Pages deploy |
| 15 | A WebVM boot with `main()` blocked (see #10) left the proxy **idle**, which is why the first end-to-end proxy test passed. Once `main()` ran the self-test, the boot-time upstream fetch competed with the request being tested | The successful test ran on the VM whose `main()` was blocked | Medium — the self-test is now off by default |
| 16 | **CORS cannot make an `http://` frame load on an `https://` page.** Mixed content is checked before the request is made; CORS is checked after it succeeds | Chrome logged `Mixed Content: ... This request has been blocked` and **no request was captured** in the network log. The Service Worker that the site already registers does not bypass it either — the page's `fetch('http://...')` failed with the same mixed-content error | **High** — CORS is still needed for the *read*, which is what makes the `srcdoc` approach work |
| 17 | **Tailscale Serve is not enabled on this tailnet**, and the WebVM's node cannot enable it (no `tailscaled` in the guest — Tailscale runs in the browser's CheerpX runtime) | `tailscale serve --bg ...` printed `Serve is not enabled on your tailnet` and a one-click enable link; the guest has no `tailscale`/`tailscaled` binary and no node HTTPS listener on 443 | **High** — one click in the admin console enables the publicly-trusted HTTPS route |
| 18 | Adding a root certificate to the Windows trust store needs a **UI confirmation**, which a non-interactive session cannot answer | `certutil -user -addstore` hung on a dialog; `Import-Certificate` failed with `UI is not allowed in this operation`; `CertAddEncodedCertificateToStore` hung on the same protected-root prompt | **High** — `install_dev_ca.py` calls the Win32 API, but the protected-root prompt still applies; the Tailscale Serve route avoids the problem entirely |
| 19 | The browser shares this machine's network namespace (`http://127.0.0.1:<port>` opened from the browser reached a server started here) | A local HTTP server on `127.0.0.1:18099` returned `HOSTLOCAL_OK` in the browser | **High** — useful for testing, but it is a machine dependency, so it is not used in the design |

## Host-side tooling (kept in the repo)

These make it possible to work on the WebVM without pasting text into the
browser terminal:

| Script | Purpose |
|---|---|
| `examples/python3/discover.py` | Reads `tailscale status`, finds the live WebVM node and probes its ports |
| `examples/python3/vmshell.py` | Runs shell commands **inside** the WebVM over the telnet shell (port 2323); each command is delimited by a unique marker. `--file <path>` reads the commands from a file so shell quoting cannot bite |
| `examples/python3/probe.py` | Raw HTTP/TCP probes against a given host and port list |
| `examples/python3/proxy_test.py` | End-to-end proxy test: `/health`, iframe page, `/proxy-status` polling, then a real site fetched **through** the proxy, reporting headers and the injection marker |
| `examples/python3/proxy_once.py` | One proxied fetch, reporting status, headers, marker and title |
| `examples/python3/fetch_raw.py` | Dumps the raw HTTP response from a host/port/path |
| `examples/python3/check_tls.py` | Verifies the WebVM's HTTPS proxy and its certificate chain |
| `examples/python3/test_proxy_local.py` | The same proxy exercised locally on the host (no WebVM needed) — the fastest regression check |
| `examples/python3/proxy_forward.py` | Publishes the proxy on `127.0.0.1:28443`, re-resolving the live WebVM automatically |
| `examples/python3/make_proxy_certs.py` | Regenerates the development CA and the wildcard certificate |
| `examples/python3/install_dev_ca.py` | Adds the development CA to the current user's trust store |

Typical loop:

```powershell
cd examples/python3
python discover.py                 # find the live WebVM IP
python proxy_test.py <ip> 28082    # prove the proxy works end to end
python vmshell.py <ip> "cat /tmp/web-adjuster.log"   # look inside the VM
```

## Previous approaches (blocked — do not revisit)

The Kaggle API dataset/kernel RX/TX loop can transport files and command results through submitted notebook executions, but it does not expose a persistent, direct connection to Kaggle. The tokenized Jupyter proxy route was not successfully obtained through RX/TX. Do not resume that line of work without new evidence.

## Key files

| File | Purpose |
|------|---------|
| `reset` | Plain-text reset generation; changing it triggers the deployed WebVM reset poller |
| `src/app.html` | Polls `reset` and dispatches the browser reset event; loads `serviceWorker.js?v=2` (bump the `?v=` when the worker changes) |
| `src/lib/WebVM.svelte` | WebVM browser-host integration, Tailscale auto-connect, the Tailscale-IP banner, the Proxy panel |
| `src/lib/network.js` | Tailscale auth key, connection state, netmap callbacks, `currentIpStore` / `currentDnsNameStore` |
| `serviceWorker.js` | The site's Service Worker: adds the COI headers **and** hosts the `/webvm/httpsproxy` proxy bridge |
| `examples/python3/pi.py` | Boot launcher — starts proxy, loads pi_impl, runs main() |
| `examples/python3/pi_impl.py` | Runtime orchestration, endpoint self-tests, Kaggle submission |
| `examples/python3/web_adjuster/launcher.py` | The proxy itself: raw-socket HTTP server, wrapper `:28082` + forward proxy `:28081`/`:28083`, header stripping, URL rewriting, script injection |
| `examples/python3/telnet_shell.py` | Raw-TCP shell on `:2323` (Goal 2) — also the reference for the CheerpX-compatible socket pattern |
| `examples/python3/kaggle_io_loop/terminal_service.py` | Existing telnet server (routes through RX/TX — needs simplification) |
| `examples/python3/kaggle_io_loop/controller.py` | Kaggle API submission, status/output retrieval |
| `examples/python3/kaggle_io_loop/agent.py` | Notebook-side receiver/diagnostics |
| `examples/python3/test_proxy_local.py` | Local (host-side) end-to-end proxy test — no WebVM required |
| `examples/python3/discover.py`, `proxy_test.py`, `probe.py`, `vmshell.py` | Host-side tools: find the live VM IP, test the proxy, probe ports, run commands inside the VM |
| `examples/python3/proxy_forward.py` | Host-side forwarder `127.0.0.1:28443` → the live WebVM's proxy, re-resolving the WebVM automatically |
| `examples/python3/make_proxy_certs.py` | Generates the throwaway development CA and the wildcard certificate for the tailnet's MagicDNS names |
| `examples/python3/install_dev_ca.py` | Adds that CA to the current user's trust store without the UI prompt `certutil`/`Import-Certificate` require |
| `examples/python3/check_tls.py` | Verifies the WebVM's HTTPS proxy and its certificate chain |
| `examples/python3/web_adjuster/tls/` | The development CA and server certificate. **The private key is committed on purpose** so the WebVM can fetch it at boot; it only ever protects a throwaway local CA |
| `redeploy` | Append a line to trigger GitHub Pages rebuild |

## CheerpX gotchas (these cost the most time — do not rediscover them)

1. **`select()`/`poll()`/`epoll()` do not work on sockets.** Python
   implements socket timeouts with `select()`, so **a socket timeout never
   fires** — it does not raise, it wedges. A blocking read that was given a
   30 s timeout hung for the whole 60 s the browser waited, and the
   handler never logged anything. Use blocking sockets plus a watchdog
   thread that closes the socket when the deadline passes.
2. **`create_connection(..., timeout=1)` can wedge the whole VM.** The
   pending connect waits in `select()`, and because the scheduler is
   cooperative the proxy's accept loop stops running: connections are
   accepted by the kernel but no request is ever processed, so the proxy
   looks dead. Never use a socket timeout inside the WebVM.
3. **Loopback is not wired up.** Connecting to `127.0.0.1:<port>` inside
   the VM is refused even when something is listening there. The in-VM
   self-test therefore cannot use loopback.
4. **`ifconfig` exists but prints nothing**, and `/proc/net/` and
   `/sys/class/net/` are empty. The only way to learn the node's address
   from inside the VM is the UDP-route trick to `100.100.100.100`.
5. **There is no `tailscaled` in the guest.** Tailscale runs in the
   browser's CheerpX runtime, so `tailscale serve` and `tailscale cert`
   are not available inside the VM, and the node has no HTTPS listener of
   its own.
6. **Exec a helper module into its own namespace.** `globals()["__name__"]`
   is `"__main__"`, so exec'ing a module into `globals()` runs its
   `if __name__ == "__main__":` block — which for `telnet_shell.py` ends
   in `threading.Event().wait()` and blocks `main()` forever.
7. **A long-lived subprocess server gets starved**, but short-lived
   subprocesses (`/bin/sh`, `fuser`) work fine. That is why the proxy runs
   as a thread inside `pi_impl.py` rather than as a separate process.

## How the WebVM reset is triggered and verified

The reset source of truth is the plain-text `reset` file on **`main`**. Put a new generation number in that file and commit it. The deployed page in `src/app.html` fetches that raw GitHub file with cache-busting every **15 seconds**, and also checks on page startup and `pageshow`. A new generation is added to the URL as `?reset=<generation>` and dispatched as a reset event; the WebVM handler clears the disk cache and reloads.

Do not treat the “already handled” log as proof that the disk cache was reset. Verify the complete sequence in the browser console: new generation detected, reset begins, reset completes and reloads, and the URL includes the new generation after reload. If reset fails, the generation marker must be cleared so the next poll can retry.

## How to inspect runtime logs and failure evidence

The current IO-loop controller writes `/home/user/kaggle-io-loop-run/run.jsonl`. Inspect its `API_REQUEST_START`, `API_REQUEST_RESULT`, `EXECUTION_STATUS`, `OUTPUT_VALIDATION` and `LOOP_PROOF_PASS` records. The notebook writes its agent events to Kaggle execution logs and returns `tx_response.json`. Do not infer a successful loop from a successful GitHub Actions syntax test: the live Kaggle run must return a matching, checksum-valid response.

The earlier Qwen run had separate failures involving Kaggle API TLS upload errors and DNS/network access from inside Kaggle. This IO-loop proof deliberately avoids requiring notebook internet, so no DNS result is expected from the notebook. Controller-side Kaggle API calls still require working controller network access and Kaggle credentials.

**Agent workflow:** inspect the newest logs; find the earliest concrete failure; make a focused fix; commit; wait for validation; then inspect the actual Kaggle execution result. Do not ask the user to retrieve logs that repository tooling can fetch itself.
---

# Todo 
check out new code colab_worker.ipynb libpnp stuff needs merging to jupyter.ipynb the llamacpp rpc could only stay as an option in a framework and then
should not be default behavior 
# Browser Compute Project

A WebVM-based browser compute environment with WebGPU virtual compute capabilities.

## Overview

This [project](https://scatterp2.github.io/webvm/) extends [WebVM](https://webvm.io) — a serverless Linux virtual machine running in your browser via WebAssembly — with additional compute capabilities and automation tooling.

## Documentation

### Core Documentation
- **[WebVM Guide](README-webvm.md)** - Complete documentation for WebVM including setup, networking, deployment, and Claude AI integration
- **[WebGPU Compute Layer](README-addgpu.md)** - Technical specification for browser-native GPU compute using WebGPU, WebAssembly SIMD, and virtual compute interfaces

### Key Features

#### WebVM Base
- Full Linux environment running client-side in HTML5/WebAssembly
- Powered by CheerpX virtualization engine (x86-to-WebAssembly JIT)
- Tailscale networking support for private network access
- Debian distribution with native development toolchains

#### Extended Capabilities
- **Playwright Integration**: Node.js-based browser automation stack
- **Jupyter Notebooks**: Interactive Python environment
- **WebGPU Compute**: Virtual compute layer compiling workloads to WebGPU pipelines (see `README-addgpu.md`)

## Architecture

```
Browser Runtime
├── WebGPU API
├── GPU Buffers
└── Pipeline Compiler
        ↑
        │ WGSL + metadata
        │
WebVM Layer
├── /dev/cuda (virtual interface)
├── /dev/tensor (virtual interface)
├── Kernel compiler
└── Tensor graph compiler
        ↑
        │
Linux Applications (Playwright, Jupyter, etc.)
```

## Quick Start

### Use Online
Visit [https://webvm.io](https://webvm.io) to run WebVM directly in your browser.

### Deploy Your Own
See [Development & Customization](README-webvm.md#development--customization) in the WebVM guide for:
- GitHub Pages deployment
- Local serving with custom disk images
- Building custom Docker-based images

### Add GPU Compute
See [Browser Compute OS](README-addgpu.md) for implementing WebGPU-based compute pipelines.

## Related Files

- `jupyter.ipynb` - Jupyter environment configuration
- `dockerfiles/` - Docker configurations for building custom disk images
- `custom-disk-images/` - Directory for local `.ext2` disk images

## TODO Tasks

### Repository Genesis
- [ ] Merge legacy script https://github.com/scatterp2/testbrowser into `old/` directory, preserving its original purpose as a partial guide to Google's sign-up workflow

### WebVM Architecture & Account Creation
**Alex:**
- [ ] Create accounts through proxy service nested inside WebVM
- [ ] Build SaaS website to host and market services
- [ ] Review WebGPU compute layer implementation in `README-addgpu.md`
- [ ] Configure Playwright to use cookies to trigger Jupyter notebooks
- [X] Verify libpnp peer-to-peer networking in browser environment

**Arun:**
- [ ] Restrict proxy to Colombia and jurisdictions that do not require phone verification
- [ ] https://github.com/samyk/webscan
- [ ] Enlist auxiliary machines for SMS verification processing

**Scatterp:**
- [X] Test Tailscale networking integration with custom exit nodes
- [X] Complete MeshLLM integration testing with GLIBC 2.39
- [ ] Store signed-in Google cookies in secure, version-controlled vault (encrypted)
- [X] Merge jupyter and glibc2_39_meshllm_libpnp into single pipeline
- [X] Validate virtual `/dev/cuda` and `/dev/tensor` interface design
note above was validated in webvm dev channel many googlie eyes fire emojis etc they liked it! see README-addgpu.md

### Endgame Architecture
- [ ] Implement JavaScript-driven WebVM that initiates Google sign-up flow when user triggers from streaming platform
- [ ] Automate token acquisition, Colab login, and GPU T4 allocation
- [ ] Weave user GPU into MeshLLM network via libpnp (non-IPv4) or Petals protocols

### Monetization Blueprint
- [ ] Deploy single-cent ad network (X tokens per machine, billed at X cents)
- [ ] Develop video content generation module
- [ ] Build AI companion system ("AI girlfriend" module) for commercial exploitation
- [ ] Secure Amazon and Google free startup credits

### Testing & Validation
- [x] Created `test_glibc239_runner.ipynb` - fetches and runs glibc2_39 workflow from raw GitHub URL
- [ ] Implement one-click launcher that fetches raw GitHub notebook URL
- [ ] Add end-to-end validation for WebVM + WebGPU compute stack

#### Test Runner Notebook Usage

The `test_glibc239_runner.ipynb` provides:
- Configurable raw GitHub URL input for fetching the source notebook
- Automated execution of all GLIBC 2.39 build steps inside Jupyter Lab
- Environment setup, dependency installation, and mesh-llm integration
- libp2p networking component testing
- Summary report with pass/fail status for each check

**Launch in WebVM Jupyter Lab:**
1. Open `test_glibc239_runner.ipynb` in your WebVM Jupyter instance
2. Update the `NOTEBOOK_URL` variable to point to your raw GitHub notebook URL
3. Run all cells to execute the complete glibc2_39 workflow

**One-Click Launch (Future):**
The notebook can be enhanced to accept URL parameters, allowing direct launch like:
```
jupyter-lab --notebook-url="https://raw.githubusercontent.com/user/repo/main/glibc2_39_meshllm_libpnp.ipynb"
```

## Community & Support

- **WebVM Issues**: [GitHub Issues](https://github.com/leaningtech/webvm/issues)
- **Discord**: [Join the community](https://discord.gg/yTNZgySKGa)
- **CheerpX Docs**: [https://cheerpx.io/docs](https://cheerpx.io/docs)

## License

WebVM is released under the Apache License, Version 2.0. See [LICENSE.txt](LICENSE.txt) for details.

The public CheerpX deployment is free for individual technological exploration and testing. Commercial use requires a license. See [CheerpX Licensing](https://cheerpx.io/licensing) for more information.
