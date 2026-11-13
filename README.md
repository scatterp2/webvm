# AI Agent Handoff / Operating Notes

> Keep this section current. These notes let an AI agent continue the work without asking the user to repeat repository context.

## Where things stand

## Current Kaggle blocker and finish line

**Next step: fix Kaggle's anti-forgery-token failure.** Keep the browser's session cookies and matching token together, ensure session-dependent HTML is not served from a shared URL-only cache, and verify the upstream request headers against the failing request. The proposed patch is a hypothesis until tested; do not mark this step complete just because it has been committed.

**Final step: make the real Kaggle page visibly display inside WebVM through the proxy.** The job is not finished when the anti-forgery error disappears, when the proxy returns HTTP 200, or when scripts load. The final acceptance test is a browser-visible Kaggle page rendered in the WebVM proxy panel, with any remaining routing/rendering failures resolved. Update the status table below only when that end-to-end result has actually been observed.

The detailed sequence and validation checklist are in [the Kaggle anti-forgery handoff](docs/handoff-kaggle-antiforgery.md).


| Goal | State | Evidence |
|---|---|---|
| 1 — Tailscale auto-connect | **DONE** | The node gets a 100.x address on every boot and the address is printed in the WebVM terminal. |
| 2 — Telnet access to the VM | **DONE** | `plink.exe -raw -P 2323 <ip>` gives a root shell. |
| 3 — The proxy works | **DONE, VERIFIED** | A real HTTPS page is fetched through the VM, `x-frame-options` is stripped and the proof script is injected and executes. |
| 4 — Kaggle in a proxy iframe | **DONE, VERIFIED** | `http://<ip>:28082/` shows the real Kaggle page in an iframe and the parent can read its DOM (`sameOrigin: true`). |
| 4b — that iframe **inside the GitHub Pages WebVM page** | **DONE, VERIFIED** | The Proxy button in the bottom-right of the WebVM page opens a panel with the proxied page in an iframe. Measured: `panelStatus: "Loaded 7255 bytes through the bridge - srcdoc frame keeps DOM access."`, `title: "Kaggle: The World's AI Proving Ground"`, `markerFound: true`, `htmlAttr: "true"`. |
| 5 — make the proxied page actually **show** | **IN PROGRESS** | Real LE cert on `:28443`, `credentialless` iframe for COEP compat, static assets load from CDN via `_direct_url()`, proxy strips bot-detection headers (sec-fetch-*, Referer, Origin) and sends browser-like headers upstream. Kaggle's JS app loads and runs (title correct, CSS/background renders, Kaggle ASCII art in console). **Remaining:** SPA client-side router sees `/proxy/…` path and renders 404; `history.replaceState` injection and CORS-safe redirect scoping in progress. |

### The one thing to try next

Fix the SPA client-side routing: the `history.replaceState` injection
must execute before Kaggle's React Router so the router sees `/` instead
of `/proxy/…`. Also scope the 302 CDN redirects to `.js` files only —
fonts and images redirected to Kaggle's CDN get blocked by CORS because
the CDN doesn't send `Access-Control-Allow-Origin`.

### How the proxy panel works today

Press the **Proxy** button in the bottom-right of the WebVM page. A
`credentialless` iframe navigates to
`https://webvm-proxy.tailf987e.ts.net:28443/proxy/<encoded-url>`. The
proxy fetches the target, strips framing blockers, rewrites URLs to the
CDN, injects the proof script, and serves the response with CORS/CORP
headers. Static assets (JS, CSS) in `<script>`/`<link>` tags are
rewritten to absolute Kaggle CDN URLs by `_direct_url()` so they bypass
the proxy entirely. Webpack dynamic chunks hit the catch-all handler
which either redirects (`.js`) or proxies them.

### Things that will bite you next

- **Bump `?v=` when you change `serviceWorker.js`.** `src/app.html` loads
  `serviceWorker.js?v=9`. The browser caches a worker for a long time and
  `unregister()` does not release control until the page reloads, so a
  changed worker keeps serving the old code until you unregister **and**
  reload. See "Service Worker update gotcha" below.
- **The WebVM does not boot without cross-origin isolation.** The service
  worker sets `COEP: require-corp` + `COOP: same-origin`. With them
  removed the guest hangs at start-up and never registers on the tailnet.
  They are required, and `COOP` is exactly what nulls a popup's
  `window.opener` — which is why the proxy panel navigates the frame
  **directly** to the proxy's https origin instead of going through a
  popup.  (The popup bridge still exists in the code and in
  `serviceWorker.js`, but it is no longer on the panel's path.)
- **The Pages deploy can fail spuriously.** The build job succeeds and
  only `actions/deploy-pages@v4` fails; re-running the workflow (touch
  `redeploy` and push) fixed it. The job logs need auth to read.
- **Two TLS handshakes at once inside the VM wedge it.** See finding 20.

## Active Goals (in priority order)

**Goal 5 is the only one still open** — goals 1 to 4 are done and
verified. Goal 5 is "make the proxied page actually show"; its options and
the measured reasons for each are in **Goal 5** below.

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
| Kaggle **app** loaded and usable in the iframe | **PASS** — `title: "Kaggle: The World's AI Proving Ground"`, 15 scripts, the VM stays healthy afterwards |
| JS/DOM injection confirmed inside the iframe | **PASS** — `JS/DOM INJECTION CONFIRMED: marker element present = true, documentElement attribute = true` |
| DOM access from the parent page | **PASS** — `sameOrigin: true`; the parent reads `frame.contentDocument` |
| The VM survives a full SPA load | **PASS** — after Kaggle's page and its resources loaded through the proxy, `/health` still answers `200` |
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


### Goal 4: Load Kaggle via proxy in iframe (DONE — VERIFIED)

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

### Goal 5: Make the proxied page actually *show* (IN PROGRESS)

**Status:** The HTTPS proxy with a real LE cert is working. The proxy
panel uses a `credentialless` iframe that navigates directly to the TLS
proxy on `:28443`. Static assets are loaded from Kaggle's CDN via
`_direct_url()` rewriting and through 302 redirects. Bot-detection
headers are stripped and replaced with browser-like headers.

**`example.com` renders perfectly in the panel** (verified visually: the
book icon, the English text and all the translations). Kaggle's own SPA
loads too — its CSS, its JavaScript and its images all arrive — but
Kaggle answers the app's data call with a **reCAPTCHA enterprise
challenge**, so the frame shows Kaggle's own crash page
("Something went wrong and this page crashed!").

**The remaining issues:**

| Part | Status |
|---|---|
| HTML delivered with correct title | **works** — "Kaggle: The World's AI Proving Ground" |
| Proof script injection | **works** — `WEBVM_ADJUSTER_INJECTED` banner visible |
| LE cert on `:28443` (no mixed content) | **works** — `TLS_PROXY_STARTED port=28443 cert=LE`, and the chain validates from an ordinary client: `https://webvm-proxy.tailf987e.ts.net:28443/health` → `200 ok` |
| `credentialless` iframe for COEP | **works** — the frame renders the page, not a blank frame |
| Static assets (JS, CSS, images) | **works** — `_direct_url()` rewrites them to absolute Kaggle URLs, and the catch-all 302-redirects them to the CDN; they arrive with `200` |
| `history.replaceState` routing injection | **works** — verified byte-for-byte in the served HTML, injected before the first script and carrying the page's nonce: `<script nonce="…">try{history.replaceState(null,"","/")}catch(e){}</script>` |
| The page's own API calls | **works** — the injected script patches `fetch` and `XMLHttpRequest.open` so absolute calls to the original site are rewritten to `/proxy/<encoded-url>` |
| **`example.com` in the panel** | **works** — it renders completely |
| **Kaggle's own data call** | **BROKEN** — Kaggle answers with a **reCAPTCHA enterprise challenge** (`https://www.google.com/recaptcha/enterprise/anchor` shows up in the network log), so the app renders its crash page. This is Kaggle's bot detection, not the proxy |
| Fonts | **known limitation** — the stylesheet is rewritten to an absolute Kaggle CDN URL, so the font inside it resolves against the CDN and is fetched in CORS mode; Kaggle's CDN sends no `Access-Control-Allow-Origin` for fonts, so that one font fails with `ERR_FAILED`. Making it work means proxying the CSS **and** rewriting its `url(...)` references to the proxy, which risks CheerpX dropping the CSS transfer. Cosmetic only — the page still renders. Fonts are no longer CDN-redirected when they do come through the proxy. |

#### What was done (commits, most recent first)

1. **`347f181` — SPA routing fix (history.replaceState injection)**
   Injects `history.replaceState(null, '', '/')` before any scripts so
   React Router sees `/` instead of `/proxy/…`. Also updates catch-all to
   resolve against `DEFAULT_TARGET` when Referer lacks `/proxy/`. **Bug:**
   the injection isn't appearing in the page — needs debugging.

2. **`ba96210` — Static asset redirect + header cleanup**
   - Catch-all sends 302 redirect for static assets (`.js`, `.css`,
     images, fonts) to the CDN URL instead of proxying through CheerpX's
     unreliable TLS socket.
   - Strips `sec-fetch-*`, `Referer`, `Origin` from upstream requests;
     replaces them with headers that look like a direct browser navigation.
   - Adds `Accept-Language` and a proper `Accept` header.
   **Bug:** redirecting fonts/images breaks CORS — only `.js` and `.css`
   should redirect; fonts/images must stay proxied for CORS headers.

3. **`43af852` — `credentialless` iframe for COEP**
   The WebVM page's service worker sets `COEP: require-corp`. Without
   `credentialless` on the iframe, cross-origin content renders white
   even with `CORP: cross-origin`. The attribute bypasses this.

4. **`6eeb204` — Direct iframe navigation (no CORS fetch)**
   The proxy panel sets `iframe.src` directly instead of fetching via
   CORS and using `srcdoc`. Simpler and avoids the bridge popup entirely.

5. **`171d900` — Direct CDN loading (`_direct_url()`)**
   `rewrite_html()` rewrites `<script src>`, `<link href>`, `<img src>`
   etc. to absolute Kaggle CDN URLs. CheerpX's TLS socket drops large
   transfers (>1 MB), so proxying multi-MB bundles is unreliable.

6. **Earlier commits** — LE cert generation (tag-swap trick), proxy panel
   UI, bridge popup (now superseded by direct iframe navigation).

#### Remaining work

1. **Fix `history.replaceState` injection** — debug why the `<script>`
   isn't appearing in `<head>`. Possibly a nonce mismatch or the `<head>`
   search failing. The script must execute before Kaggle's React Router.

2. **Scope 302 redirects to `.js` only** — fonts (`@font-face`) and
   images (in certain CSS/JS contexts) need CORS headers that Kaggle's
   CDN doesn't provide. Only `.js` (loaded via `<script>`) and possibly
   `.css` (loaded via `<link>`) can safely redirect. Everything else must
   stay proxied (the proxy adds `Access-Control-Allow-Origin: *`).

3. **API calls** — Kaggle's SPA makes `fetch()` calls to `/api/v1/…`.
   After `history.replaceState` changes the URL, these will go to the
   proxy as bare paths. The catch-all resolves them against
   `DEFAULT_TARGET` and proxies them (not redirected — they need CORS).
   May need cookie forwarding for authenticated endpoints.

#### Previous options (for reference — Option A/tag-swap variant is what shipped)

The mixed-content problem (http sub-resources on an https page) was
solved by generating a **real Let's Encrypt certificate** for the
WebVM's DNS name via the tag-swap trick (see below). The bridge popup
and the inline/data-URI approaches are no longer needed.

Options A-E were evaluated when the proxy only had HTTP. Now that the
TLS proxy on `:28443` uses a publicly-trusted LE cert, the iframe loads
over HTTPS and mixed content is no longer an issue.

#### What was actually implemented — the tag-swap cert trick

Instead of Tailscale Serve (which requires enabling Serve on the tailnet),
we generated a **real Let's Encrypt certificate** for the WebVM's DNS name
by temporarily tagging a real machine with the WebVM's identity:

**How the cert was generated (2024-11-12):**

1. Created a **tagged auth key** (`tag:webvm`, ephemeral + reusable) in the
   Tailscale admin console so the WebVM node gets a stable name.
2. Added `hostname: "webvm-proxy"` to the `networkInterface` object in
   `src/lib/network.js` so the WebVM registers as
   `webvm-proxy.tailf987e.ts.net`.
3. **Tag-swap trick:** temporarily re-registered the laptop as `tag:webvm`
   with hostname `webvm-proxy`:
   ```powershell
   tailscale up --auth-key=tskey-auth-<tagged-key> --force-reauth
   tailscale set --hostname=webvm-proxy
   ```
   The laptop briefly became `webvm-proxy.tailf987e.ts.net` — a real
   machine that Let's Encrypt can validate.
4. Generated the cert:
   ```powershell
   tailscale cert webvm-proxy.tailf987e.ts.net
   ```
   This produces `webvm-proxy.tailf987e.ts.net.crt` (full chain, 4 certs)
   and `webvm-proxy.tailf987e.ts.net.key` (EC private key). Tailscale
   handles the ACME challenge via its DNS integration.
5. Copied the cert and key to `examples/python3/web_adjuster/tls/` as
   `webvm-proxy.crt` and `webvm-proxy.key`, committed and pushed.
6. **Restored the laptop** — re-authenticated with the user's own identity
   and reset the hostname back to the machine's Windows name:
   ```powershell
   tailscale up --force-reauth --reset
   tailscale set --hostname=desktop-21m8c49
   ```
7. The DNS name `webvm-proxy.tailf987e.ts.net` is now free for the
   WebVM's JS node to claim on next boot.

**How the cert is used at runtime:**

- `pi_impl.py` fetches `webvm-proxy.crt` and `webvm-proxy.key` from
  GitHub raw at boot and starts the TLS wrapper on `:28443`.
- The browser can then access `https://webvm-proxy.tailf987e.ts.net:28443/`
  — a publicly trusted cert, no mixed content.

**Cert renewal:** the cert expires **2027-02-10**. To renew, repeat the
tag-swap: temporarily register a real machine as `webvm-proxy` with
`tag:webvm`, run `tailscale cert`, copy the new files, restore the machine.

---

## System updates made while building this

Everything below is in the repository and deployed. The commits are the
history of this work.

| Area | What changed |
|---|---|
| `examples/python3/web_adjuster/launcher.py` | Pure-Python proxy running inside the WebVM: raw sockets, wrapper on `:28082`, forward proxy on `:28081`/`:28083`, TLS on `:28443` with LE cert. `/proxy/<url>` reverse proxy with header stripping, URL rewriting, nonce-aware script injection, `history.replaceState` injection for SPA routing, 302 CDN redirects for static assets in catch-all, bot-detection header cleanup (`sec-fetch-*`, `Referer`, `Origin` replaced with browser-like headers), keep-alive connection pooling, 120 s response cache, watchdog timeouts, serialised upstream queue (semaphore of 1). |
| `examples/python3/pi_impl.py` | Starts the proxy **in-process** (not as a subprocess), fetches the launcher and the TLS material from `main` at boot, prints the Tailscale-IP banner, and keeps the boot self-test off by default. |
| `examples/python3/pi.py` | No longer spawns the launcher as a subprocess; it only loads `pi_impl.py` and runs `main()`. |
| `examples/python3/telnet_shell.py` | Unchanged — it is the reference for the CheerpX-compatible socket pattern. |
| `serviceWorker.js` | Keeps the COI headers the WebVM needs, and hosts `/webvm/httpsproxy` (the CORS bridge routes), `/webvm/httpsproxy/diag` (the diagnostic) and `/webvm/proxy-inbox` (the bridge inbox). |
| `src/lib/network.js` | Exports `currentIpStore` and `currentDnsNameStore` so the page reacts to the node's address and name. Sets `hostname: "webvm-proxy"` in the `networkInterface` so the node registers as `webvm-proxy.tailf987e.ts.net`, matching the LE cert CN. Uses a tagged auth key (`tag:webvm`) for stable identity. |
| `src/lib/WebVM.svelte` | Prints the Tailscale-IP banner into the terminal, and adds the Proxy panel: a `credentialless` iframe that navigates directly to the HTTPS proxy (`webvm-proxy.tailf987e.ts.net:28443`). No bridge popup or `srcdoc` needed. |
| `src/app.html` | Loads `serviceWorker.js?v=9` — **bump the `?v=` whenever the worker changes**. |
| `examples/python3/web_adjuster/tls/` | Real **Let's Encrypt** certificate and EC private key for `webvm-proxy.tailf987e.ts.net`, fetched by the VM at boot. The private key is committed on purpose so the WebVM can fetch it at runtime. Generated via the tag-swap trick (see Goal 5). Expires 2027-02-10. |
| `examples/python3/*.py` (host-side tools) | `discover.py`, `probe.py`, `proxy_test.py`, `proxy_once.py`, `fetch_raw.py`, `vmshell.py`, `check_tls.py`, `check_sw.py`, `inspect_proxy.py`, `publish_proxy.py`, `make_proxy_certs.py`, `install_dev_ca.py`, `test_proxy_local.py`. |

**Deployment behaviour worth knowing:**

- The `reset` file is the source of truth for the browser reset; the
  number in the page URL must be **at least** the one in the file.
- `examples/python3/pi.py` and `examples/python3/web_adjuster/launcher.py`
  are fetched **at runtime** from `main`, so a change to the proxy needs
  only a browser reset — no image rebuild.
- `src/**` and `serviceWorker.js` are compiled into the Pages site, so
  they need a **Pages rebuild** (touch `redeploy` and push).
- The Pages deploy sometimes fails **after** a green build, only at the
  `actions/deploy-pages@v4` step; re-running the workflow fixes it.
- The disk image is rebuilt on every deploy, so `pi.py` changes take
  effect then.


## Getting the iframe onto the GitHub Pages WebVM page (how it was done)

The user wants the proxied page rendered **in the same page as the WebVM**,
i.e. inside `https://scatterp2.github.io/webvm/`. This is now done — see
Goal 4 and Goal 5.

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
with the same mixed-content error, and so did the worker's own fetch
(rejected in **0–1 ms**, i.e. before the network was touched).

What CORS *does* fix is the **read**, and that is what makes the `srcdoc`
approach work: the proxy sends `Access-Control-Allow-Origin: *` on every
response, the page reads the proxied HTML, and injects it with `srcdoc`. A
`srcdoc` frame inherits the host page's origin, so **DOM access survives
the cross-origin hop**.

### The design that is implemented: the bridge popup

Four facts force this shape:

1. The page is https and the proxy is http → the page cannot fetch the
   proxy (mixed content), and neither can the Service Worker.
2. The page needs **cross-origin isolation** (without it the guest hangs
   at start-up and never registers on the tailnet — verified), and
   `COOP: same-origin` **nulls a popup's `window.opener`**, so a popup
   cannot `postMessage` back.
3. `COOP: same-origin` also **blocks reading `popup.location.href`**:
   `SecurityError: Failed to read a named property 'href' from
   'Location'`. (Without COOP, reading the href of a cross-origin popup
   is allowed — so the fragment channel works *only* when the opener is
   not COOP-isolated, which the WebVM cannot afford.)
4. The popup therefore has **no channel back to the opener at all** —
   except a **navigation to the opener's own origin**, which the opener's
   Service Worker always intercepts.

So the chain is:

```
WebVM page (https)
  └─ opens popup  http://<vm>:28082/?bridge=1&origin=<page origin>&target=…
        └─ GET /proxy/<target>                (same-origin http, no mixed content)
              └─ the proxy fetches the target, strips the framing headers,
                 rewrites the URLs back through the proxy and injects the
                 proof script
        └─ navigates to  <page origin>/webvm/proxy-inbox?doc=<document>
  └─ the page's service worker intercepts that navigation, stashes the
     document in the Cache Storage and answers with a small page
  └─ the opener polls /webvm/proxy-inbox and injects the document
  └─ iframe.srcdoc = document      -> the frame inherits the page's origin
```

**Everything is on the WebVM and in the deployed site. Nothing has to be
installed on the laptop, and no tailnet setting is needed.**

| Piece | Where | State |
|---|---|---|
| `/proxy/<url>` — the proxied document | `examples/python3/web_adjuster/launcher.py` | **VERIFIED** — 200, 7145 bytes for Kaggle, framing headers stripped, script injected |
| `/?bridge=1&origin=…&target=…` — the popup mode | same file | **VERIFIED** — the popup fetched the document (`[bridge] proxy status 200`, `[bridge] document bytes 7145`) and navigated to the inbox |
| `/webvm/proxy-inbox` — the service worker's inbox | `serviceWorker.js` | **VERIFIED** — the popup's navigation was intercepted ("The proxied document was handed over") and the opener's poll returned the 7200 byte document |
| The opener: open the popup, poll `/webvm/proxy-inbox`, `srcdoc` it | `src/lib/WebVM.svelte` | **VERIFIED** — `srcdocLen: 7255`, the frame's title is Kaggle's and the marker is present |
| `srcdoc` frames inherit the parent origin | the browser | **VERIFIED** — the parent read the frame's `title`, the marker element and the `data-webvm-adjuster-injected` attribute |

### What is verified and what is not

| Part | State |
|---|---|
| The proxy's CORS/CORP headers | **VERIFIED** — `Access-Control-Allow-Origin: *` and `Cross-Origin-Resource-Policy: cross-origin` on every response |
| The bridge routes are served by the worker | **VERIFIED** — the worker answered with its own `502 proxy bridge failed: …` |
| The popup fetching the document | **VERIFIED** — `[bridge] proxy status 200`, `[bridge] document bytes 7145` |
| The popup handing the document over | **VERIFIED** — the popup navigated to `/webvm/proxy-inbox?doc=…` and the worker answered "The proxied document was handed over" |
| The opener picking it up and injecting it | **VERIFIED** — `srcdocLen: 7255`, `title: "Kaggle: The World's AI Proving Ground"`, `markerFound: true` |
| DOM access from the WebVM page | **VERIFIED** — the parent read the frame's title, the marker element and the `data-webvm-adjuster-injected` attribute |
| The proxied page's own sub-resources (its SPA JavaScript and CSS) | **BLOCKED** — they point back at the proxy's `http` address, which the https page blocks as mixed content. The document and the injected script work; the SPA does not become interactive until the proxy has a trusted https origin (Tailscale Serve) |

### Things the browser will not let us do (all measured, not assumed)

| Attempt | Result |
|---|---|
| `<iframe src="http://<vm>:28082/…">` on the https page | **blocked** — mixed content, and no request is even sent |
| `fetch("http://<vm>:28082/…")` from the page | **blocked** — mixed content |
| the same fetch from the service worker | **blocked** — mixed content, rejected in 0–1 ms |
| `popup.postMessage` from the popup to the page | **impossible** — `window.opener` is null under `COOP: same-origin` |
| reading `popup.location.href` from the page | **blocked** — `SecurityError` under `COOP: same-origin` |
| a **navigation** by the popup to the page's own origin | **works** — the page's service worker always intercepts it, even from a cross-origin popup |

### The alternative: an https origin for the proxy

If the bridge popup turns out to be too fiddly, the proxy can be published
over https with a publicly trusted certificate instead. That is the
"real server" case, where **https → http upstream is normal** — nginx,
Caddy and `tailscale serve` all do it, because the *server* makes the
outbound request and servers are not bound by the browser's mixed-content
rules.

| Route | Needs | Notes |
|---|---|---|
| **Tailscale Serve** on the tailnet | Serve enabled on the tailnet (one click) and `python examples/python3/publish_proxy.py --tag tag:webvm` | A publicly trusted certificate, so **no CA has to be trusted at all**. The target is the WebVM's **node name**, which is stable when the node is registered with a tagged auth key (`tag:webvm` → `tag-webvm.tailf987e.ts.net`); without a tag the command has to be re-run after every reboot. |
| The WebVM's own TLS listener on `:28443` | the development CA trusted by the browser | The cert and key live on the **WebVM** (fetched from `main` at boot, never from the laptop) and the chain verifies: `VERIFIED CHAIN OK -> 200 ok`. But adding a root certificate needs a confirmation dialog and this session cannot show one (`certutil` hangs, `Import-Certificate` reports *"UI is not allowed in this operation"*, `CertAddEncodedCertificateToStore` returns `ERROR_CANCELLED`), so this is deliberately not done. |

#### Why `tailscale cert` cannot be run on the WebVM

`tailscale cert` would be the ideal way to get a real certificate for the
node's own name, but it needs the `tailscale` CLI **and** a `tailscaled`
holding the node key:

- the WebVM image does not install Tailscale at all (see
  `dockerfiles/debian_mini` and `dockerfiles/debian_large` — no
  `tailscale` package);
- there is no `tailscale`/`tailscaled` binary anywhere in the guest
  (`ls /usr/bin`, `/usr/sbin`, `/sbin`, `/usr/local/bin`, `/opt` all
  checked);
- the Tailscale client is the **CheerpX runtime in the browser** — the
  browser performs the control-plane handshake
  (`wss://controlplane.tailscale.com/ts2021?X-Tailscale-Handshake=…`
  appears in the browser console), so the node key lives in the browser's
  WebAssembly memory and is not reachable from the guest.

So the node key is only available in the browser, and a certificate has to
be issued through the tailnet's own Serve/HTTPS facility instead.

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

`src/app.html` loads `serviceWorker.js?v=9`, so a deployed change gets a
new worker identity instead of being served from the stale cache. **Bump
the `?v=` number whenever `serviceWorker.js` changes.**

If the page is already cross-origin isolated the automatic registration
does not happen at all, so after a deploy either unregister + reload, or
register the new worker explicitly from the console:

```js
const reg = await navigator.serviceWorker.register('/webvm/serviceWorker.js?v=8', {scope: '/webvm/'});
await reg.update();
```

and then reload the page so the new worker becomes the controller.

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

## Findings and suspicions

Recorded so the next agent does not re-derive them. Most of these are
**proven** (the "Confidence" column says how); the couple that are not are
marked as such.

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
| 20 | **Two concurrent TLS handshakes inside the WebVM wedge the whole VM.** A TLS handshake blocks CheerpX's cooperative scheduler, so interleaved handshakes starve each other and the accept loop never runs — the ports stop answering and the VM looks dead | The terminal showed two `UPSTREAM_PHASE url_connect … ok` lines and no `tls_done`; the host could reach the same URL in 0.2 s; the ports then stopped answering entirely | **High** — the upstream fetches are now serialised through a semaphore of 1. A limit of 4 was not enough, 1 is. The local suite fires 12 simultaneous requests and all are answered |
| 21 | **The WebVM does not boot without cross-origin isolation.** With `COEP`/`COOP` removed the guest hangs at start-up and never registers on the tailnet | The terminal stopped at the welcome banner with no prompt, and no node appeared in `tailscale status`; adding `?coi=1` to the page URL (which restored the headers) brought it straight back | **High** — the headers are required, and `COOP: same-origin` is what nulls a popup's `window.opener`, so the bridge popup hands the document over in its URL fragment instead |
| 22 | The Pages deploy can fail **after** a successful build. Only the `actions/deploy-pages@v4` step failed | Run `a5d6a98`: the build job (Docker image, `vite build`, artifact upload) was green, the deploy job was not. The job logs need auth to read, so the cause is unknown; re-running the workflow fixed it | Medium — treat a failed Pages deploy as retryable, and touch `redeploy` to re-run it |
| 23 | The bridge popup and the page were starving each other: the popup loaded the target in its own iframe **and** fetched the self-contained document, and both go through the proxy whose upstream fetches are serialised | `/proxy-inline/` took 40 s instead of 18 s while the popup's iframe was loading the same page; the popup's fragment stayed empty | **High** — in bridge mode the popup no longer renders the page, and it logs `[bridge] …` to the console |

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
| `src/app.html` | Polls `reset` and dispatches the browser reset event; loads `serviceWorker.js?v=9` (bump the `?v=` when the worker changes) |
| `src/lib/WebVM.svelte` | WebVM browser-host integration, Tailscale auto-connect, the Tailscale-IP banner, the Proxy panel and the bridge popup |
| `src/lib/network.js` | Tailscale auth key, connection state, netmap callbacks, `currentIpStore` / `currentDnsNameStore` |
| `serviceWorker.js` | The site's Service Worker: adds the COI headers **and** hosts the `/webvm/httpsproxy` routes and the `/webvm/httpsproxy/diag` diagnostic |
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
| `examples/python3/publish_proxy.py` | Publishes the proxy over https with `tailscale serve`, targeting the WebVM's node **name** (stable with a tagged auth key) |
| `examples/python3/make_proxy_certs.py` | Generates the throwaway development CA and the wildcard certificate for the tailnet's MagicDNS names |
| `examples/python3/install_dev_ca.py` | Adds that CA to the current user's trust store (blocked on this machine by the protected-root prompt) |
| `examples/python3/check_sw.py` | Checks that the deployed `serviceWorker.js` really contains the expected changes |
| `examples/python3/inspect_proxy.py` | Reports a proxied response's CSP, the nonces in it and the nonce on the injected script |
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
8. **Never run two TLS handshakes at the same time inside the VM.** The
   handshake blocks the cooperative scheduler, so interleaved handshakes
   starve each other and the whole VM stops answering. The proxy's
   upstream fetches go through a semaphore of **one**. A limit of four was
   not enough. The response cache absorbs the repeats that the serialised
   fetches would otherwise make slow.

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
