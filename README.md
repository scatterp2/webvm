# AI Agent Handoff / Operating Notes

> Keep this section current. These notes let an AI agent continue the work without asking the user to repeat repository context.

## Active Goals (in priority order)

### Goal 1: Fix Tailscale auto-connect (CRITICAL)

**Problem:** The WebVM needs to auto-connect to Tailscale using an auth key without user interaction. The current implementation uses timing hacks (3-second delay, 15-second Promise.race) that are fragile.

**Upstream fix (leaningtech/webvm PR #235, commit 16c9fad):** In `handleConnect()`, add an early-return branch when `authKey` is set — skip the popup entirely:
```js
if(networkInterface.authKey) {
  networkData.connectionState.set("LOGINSTARTING");
  cx.networkLogin();
  return;
}
```
Also add `networkData` to the import from `$lib/network.js`.

**Our codebase state:**
- `src/lib/network.js` line 4: hardcoded `DEFAULT_AUTH_KEY` — falls back when no `#authKey=` in URL hash
- `src/lib/WebVM.svelte` `handleConnect()` (line ~434): still uses the old popup pattern
- `src/lib/WebVM.svelte` `tryAutoConnect()` (line ~396): custom auto-connect that calls `cx.networkLogin()` after a 3s delay, then races `startLogin()` with a 15s timeout. This is a workaround — it needs to be aligned with the upstream pattern.

**What to do:**
1. Apply the upstream PR #235 pattern to `handleConnect()` so manual clicks work cleanly with auth keys
2. Simplify `tryAutoConnect()` to match the same pattern — set connectionState, call networkLogin(), done
3. Verify the auth key (`tskey-auth-kqUVzBTdMV11CNTRL-...`) is still valid on the Tailscale admin dashboard
4. Test by deploying (bump `reset` file) and checking browser console for connection state transitions

**Auth key format:** Passed via URL fragment `#authKey=<key>`. Tailscale ephemeral auth keys start with `tskey-auth-`. The key is read in `network.js` from `window.location.hash`.

### Goal 2: Add lightweight telnet listener for direct AI access

**Problem:** Currently the only way to interact with the running WebVM is by pasting text into the terminal via the browser. An AI agent (Claude) needs to connect directly to run commands.

**Solution:** Add a telnet server inside WebVM that listens on a known port. The AI connects from the host machine using `plink.exe` (PuTTY) or any raw TCP client. This is lightweight — no SSH overhead, just a raw shell over TCP.

**Architecture:**
- WebVM boots → Tailscale connects → gets a 100.x.x.x address
- A simple telnet daemon starts on port 23 (or 2323) bound to 0.0.0.0
- The AI runs: `plink.exe -raw -P 2323 <tailscale-ip>` to get a shell
- Commands and output flow over raw TCP

**Existing code:** `examples/python3/kaggle_io_loop/terminal_service.py` already implements a telnet-style server on port 2323, but it routes commands through the heavy Kaggle RX/TX loop. It needs to be simplified to just exec commands locally.

**What to do:**
1. Create a lightweight telnet daemon that spawns `/bin/bash` for each connection
2. Bind to `0.0.0.0:2323` so it's reachable via the Tailscale address
3. Start it from `pi.py` or `pi_impl.py` at boot
4. Test with `plink.exe -raw -P 2323 <tailscale-ip>` from the host

### Goal 3: Make the Web Adjuster proxy work

**Problem:** The proxy infrastructure exists but needs to be verified end-to-end.

**Architecture (already built):**
- `examples/python3/web_adjuster/launcher.py` — two-port setup:
  - Port 28080: wrapper HTTP server with an HTML page containing an iframe
  - Port 28081: Web Adjuster (pip package `adjuster`) — generic HTTP proxy
- The wrapper reverse-proxies `/proxy/*` requests to the adjuster on :28081
- The adjuster is configured with `--default-site=www.kaggle.com --open-proxy --real-proxy`
- `--bodyAppend=<script>` injects a proof marker (`WEBVM_ADJUSTER_INJECTED`)

**What strips iframe-blocking headers:**
- `launcher.py` `FRAME_BLOCKED_HEADERS` removes `x-frame-options`
- `_reverse_proxy()` also strips `content-security-policy` when it contains `frame-ancestors`

**Boot sequence:** `pi.py` calls `start_proxy()` which launches `launcher.py` as a subprocess → waits for port 28080 → then `pi_impl.py` `main()` calls `start_web_adjuster_sidecar()` (which just logs that it reuses the existing launcher).

**What to verify:**
1. The adjuster pip package installs correctly in WebVM's Debian i386 environment
2. Port 28081 comes up and proxies requests to kaggle.com
3. X-Frame-Options and CSP frame-ancestors headers are stripped from responses
4. The injection script appears in proxied HTML bodies

### Goal 4: Load Kaggle via proxy in iframe (browser integration)

**Problem:** Once the proxy works (Goal 3), load Kaggle through it in an iframe on the WebVM page so an AI agent can interact with Kaggle through the proxy.

**Design:**
- The wrapper page on :28080 already has an iframe pointing to `/proxy/`
- The iframe loads `www.kaggle.com` through the adjuster proxy
- Same-origin policy is satisfied because both the parent page and iframe serve from :28080
- The injected script proves JavaScript execution works inside the proxied page

**Challenges:**
- Kaggle authentication: cookies won't carry through the proxy origin change. May need to manually set cookies or use Kaggle's API token auth.
- Kaggle's SPA may make additional requests that need to route through the proxy
- Some resources may use absolute URLs that bypass the proxy

**How to test:**
1. Get the WebVM Tailscale IP (e.g., `100.x.x.x`)
2. Open `http://<tailscale-ip>:28080/` in the built-in browser
3. Check that the iframe loads Kaggle content
4. Check browser console for the injection proof marker
5. Check that iframe interactions work (clicking, navigation)

**What NOT to do:**
- Don't try to extract the Kaggle Jupyter proxy token through RX/TX — that approach is blocked (see history below)
- Don't assume Kaggle authentication works through the proxy — test it explicitly

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
| `examples/python3/web_adjuster/launcher.py` | Proxy wrapper (:28080) + adjuster (:28081) |
| `examples/python3/kaggle_io_loop/terminal_service.py` | Existing telnet server (routes through RX/TX — needs simplification) |
| `examples/python3/kaggle_io_loop/controller.py` | Kaggle API submission, status/output retrieval |
| `examples/python3/kaggle_io_loop/agent.py` | Notebook-side receiver/diagnostics |
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
