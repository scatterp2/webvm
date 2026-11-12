# AI Agent Handoff / Operating Notes

> Keep this section current. These notes let an AI agent continue the work without asking the user to repeat repository context.

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
- WebVM boots → `pi.py` starts proxy (non-blocking) → fetches `pi_impl.py` from main → `main()` starts telnet shell
- `telnet_shell.py` listens on `0.0.0.0:2323` (raw TCP, no telnet negotiation)
- Each connection spawns `/bin/sh` via subprocess, bridged to the socket with threads
- Tailscale auto-connects → gets a 100.x.x.x address
- AI agent connects: `plink.exe -raw -P 2323 <tailscale-ip>`

**Verified:** TCP connection to port 2323 over Tailscale returns a shell prompt.

**Key files:**
- `examples/python3/telnet_shell.py` — the daemon (fetched from main at boot, local fallback)
- `examples/python3/pi_impl.py` `main()` — starts the daemon
- `examples/python3/pi.py` `start_proxy()` — non-blocking (fire-and-forget) so telnet shell starts quickly

**CheerpX boot notes:**
- `start_proxy()` MUST be non-blocking. CheerpX's cooperative scheduler makes blocking waits extremely slow — a 60-iteration sleep loop starved all other processes for many minutes.
- `pi_impl.py` is fetched from GitHub `main` at runtime, so fixes take effect on browser reset without a full deploy.
- `pi.py` is baked into the disk image — changes require a full rebuild via GitHub Actions deploy.
- Watch for `import` scoping in `main()`: a local `import time` anywhere in the function causes Python to treat `time` as local throughout, breaking earlier `time.time()` calls with `UnboundLocalError`.

### Goal 3: Make the proxy work  (FIX WRITTEN, needs live re-verification)

**Status:** The old design was broken in two independent ways. Both are now
fixed in code; the live proof still has to be re-run against a freshly reset
WebVM (see "Verification status" below).

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

| Item | Status |
|---|---|
| `examples/python3/test_proxy_local.py` — full local suite on the host | **PASS** (`/health`, `/proxy-status`, iframe page, proxied `https://example.com/` returns 200 + title `Example Domain` + marker injected + no `x-frame-options`, forward-proxy absolute-URI mode) |
| Wrapper answers `/health` inside WebVM on `:28082` | PASS (observed `HTTP/1.1 200 OK` + `ok`) |
| Wrapper answers `/health` inside WebVM on `:28080` (legacy subprocess) | PASS but flaky — it produced the `Errno 107` failure |
| Forward proxy `:28081` / `:28083` inside WebVM | **NOT YET VERIFIED** |
| Real site fetched through the proxy **inside WebVM** | **NOT YET VERIFIED** — needs a reset so the new `pi_impl.py` is fetched |
| `PROXY_SELF_TEST_PASS` in the WebVM terminal | **NOT YET VERIFIED** |
| `x-frame-options` / CSP stripping observed against a real target | **NOT YET VERIFIED** |
| Kaggle loaded through the proxy in an iframe | **NOT YET VERIFIED** (Goal 4) |

### Goal 4: Load Kaggle via proxy in iframe (browser integration)

**Status:** Not started. Depends on the Goal 3 re-verification above.

**Design:**
- The wrapper page on `:28082` renders an iframe whose `src` is
  `/proxy/<url-encoded target>`; the default target is
  `https://www.kaggle.com/`. A different target can be chosen with
  `/?target=<url>`.
- The iframe and the parent page share the wrapper origin, so the parent
  can inspect and manipulate the iframe DOM — this is the DOM access that
  the proxy exists to provide.
- The injected script proves JavaScript executes inside the proxied page.

**Open problem — mixed content.** The user asked for the iframe to live on
the **WebVM page** (`https://scatterp2.github.io/webvm/`), which is HTTPS.
An `http://` iframe on an HTTPS page is blocked as mixed content by
Chrome. Options, none verified yet:
1. Serve the proxy over HTTPS through Tailscale Serve
   (`tailscale serve --bg 28082` inside the VM gives
   `https://<node>.<tailnet>.ts.net/...` with a real certificate).
2. Keep the iframe host page on `http://<tailscale-ip>:28082/` (works
   immediately, but is not the WebVM page).
3. Terminate TLS in the proxy itself with a self-signed certificate (the
   browser will warn).

**Challenges:**
- Kaggle authentication: cookies are rewritten to the proxy origin, so
  logging in through the proxied page should work, but it must be tested
  explicitly — never assumed.
- Kaggle's SPA makes many additional requests; all absolute URLs are
  rewritten to route back through the proxy, but relative and API calls
  still need checking.
- Kaggle is a heavy SPA — the proxy is single-threaded per connection and
  CheerpX is slow, so a full page load may take a long time.

**How to test:**
1. Get the WebVM Tailscale IP (`tailscale status`, or `discover.py`).
2. Open `http://<tailscale-ip>:28082/` in the built-in browser.
3. Check that the iframe loads the target page.
4. Check the page status text for `JS/DOM INJECTION CONFIRMED`.
5. Check the browser console and the WebVM terminal for `PROXIED` lines.

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
| 7 | The WebVM Tailscale IP changes on every boot, so a hard-coded IP in a test script goes stale immediately | `100.98.161.16` → gone; `100.71.2.31` → gone; now `100.122.41.24` | **High** — always discover the live IP first |
| 8 | The browser tab can fail to boot the app with `Failed to fetch dynamically imported module` (SvelteKit chunk load failure), leaving a black screen | Browser console on the first load of the session | Medium. A plain re-navigation fixed it. Related to the `cross-world service worker resource mismatch` warning |

## Host-side tooling (kept in the repo)

These make it possible to work on the WebVM without pasting text into the
browser terminal:

| Script | Purpose |
|---|---|
| `examples/python3/discover.py` | Reads `tailscale status`, finds the live WebVM node and probes its ports |
| `examples/python3/vmshell.py` | Runs shell commands **inside** the WebVM over the telnet shell (port 2323); each command is delimited by a unique marker |
| `examples/python3/probe.py` | Raw HTTP/TCP probes against a given host and port list |
| `examples/python3/proxy_test.py` | End-to-end proxy test: `/health`, iframe page, `/proxy-status` polling, then a real site fetched **through** the proxy, reporting headers and the injection marker |
| `examples/python3/test_proxy_local.py` | The same proxy exercised locally on the host (no WebVM needed) |

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
| `src/app.html` | Polls `reset` and dispatches the browser reset event |
| `src/lib/WebVM.svelte` | WebVM browser-host integration, Tailscale auto-connect, handleConnect |
| `src/lib/network.js` | Tailscale auth key, connection state, netmap callbacks |
| `examples/python3/pi.py` | Boot launcher — starts proxy, loads pi_impl, runs main() |
| `examples/python3/pi_impl.py` | Runtime orchestration, endpoint self-tests, Kaggle submission |
| `examples/python3/web_adjuster/launcher.py` | The proxy itself: raw-socket HTTP server, wrapper `:28082` + forward proxy `:28081`/`:28083`, header stripping, URL rewriting, script injection |
| `examples/python3/telnet_shell.py` | Raw-TCP shell on `:2323` (Goal 2) — also the reference for the CheerpX-compatible socket pattern |
| `examples/python3/kaggle_io_loop/terminal_service.py` | Existing telnet server (routes through RX/TX — needs simplification) |
| `examples/python3/kaggle_io_loop/controller.py` | Kaggle API submission, status/output retrieval |
| `examples/python3/kaggle_io_loop/agent.py` | Notebook-side receiver/diagnostics |
| `examples/python3/test_proxy_local.py` | Local (host-side) end-to-end proxy test — no WebVM required |
| `examples/python3/discover.py`, `proxy_test.py`, `probe.py`, `vmshell.py` | Host-side tools: find the live VM IP, test the proxy, probe ports, run commands inside the VM |
| `redeploy` | Append a line to trigger GitHub Pages rebuild |

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
