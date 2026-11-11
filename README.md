# AI Agent Handoff / Operating Notes

> Keep this section current when changing the reset mechanism or Qwen experiment. These notes are intended to let an AI agent continue the work without asking the user to repeat repository context.

## Current objective: browser-mediated Kaggle control through the reachable VM

The previous RX/TX/Jupyter-proxy-token approach is **blocked and must not be treated as a working transport**.

### What was tried and what was not achieved

- The Kaggle API dataset/kernel RX/TX loop can transport files and command results through submitted notebook executions, but it does not expose a persistent, direct connection to Kaggle.
- We could not access/read the running Kaggle files or process state through that RX/TX mechanism in the way required to obtain the live tokenized Jupyter proxy route.
- Because the proxy route/token was not successfully obtained through RX/TX, the intended request to the tokenized Kaggle Jupyter proxy was **not established as a working end-to-end path**. Do not describe it as proven or assume the token is available in an RX/TX artifact.
- Repeatedly waiting for `peer_ip_not_ready`, trying the bare proxy hostname, or repeating the same token-discovery/RX/TX attempt does not resolve this blocker. Do not resume that line of work without new evidence that the route can actually be obtained.
- A successful dataset upload, kernel submission, `RUNNING` status, or ordinary file round-trip proves only that stage—not that WebVM can reach Kaggle's proxy or that a marker was observed at the receiver.

### New direction

Investigate a browser-mediated design instead of trying to extract the Kaggle proxy token from the notebook:

1. Treat the reachable VM address `100.92.188.123` as a candidate location for a proxy/control service; verify reachability and which ports/protocols are actually available before assuming anything.
2. Let a real browser render Kaggle and run its JavaScript, rather than attempting to launch another browser inside WebVM.
3. Investigate either a proxied Kaggle page in an iframe (subject to framing policy, redirects, and authentication limitations) or a separately opened Kaggle window controlled through an explicit browser-side bridge.
4. Use a request/response channel between the browser-side controller and the VM so a command can wait for its matching result.
5. Keep the existing Kaggle API RX/TX loop available as a known file-transport mechanism, but do not mistake it for the browser bridge or a persistent connection.

A reverse proxy only mediates requests routed through it; it cannot automatically inspect or control an already-open cross-origin Kaggle tab. A proxy also does not automatically preserve Kaggle authentication when its origin changes. Any iframe, popup, cookie, header, or browser-control behaviour must be tested and recorded rather than assumed.

### Evidence requirements

Report separately: (a) VM address/port reachability from the intended browser, (b) proxy-to-Kaggle fetch and response headers, (c) whether the page renders, (d) whether authentication works, and (e) whether a browser command receives a matching response through the VM. Do not claim success for a later stage based only on an earlier one.

Relevant files:
- `reset` — plain-text reset generation; changing it triggers the deployed WebVM reset poller.
- `src/app.html` — polls `reset` and dispatches the browser reset event.
- `src/lib/WebVM.svelte` — WebVM browser-host integration point.
- `src/lib/network.js` — existing browser networking integration.
- `examples/python3/pi.py` — automatically starts the runtime implementation.
- `examples/python3/pi_impl.py` — runtime orchestration and WebVM-side sender.
- `examples/python3/kaggle_io_loop/controller.py` — Kaggle API submission, status/output retrieval and JSONL logs.
- `examples/python3/kaggle_io_loop/agent.py` — notebook-side receiver/diagnostics and response generation.
- `examples/python3/kaggle_io_loop/README.md` — RX/TX protocol and test instructions.

Do not launch a Tailscale bridge for this objective, do not wait on `peer_ip_not_ready`, and do not edit configuration/credential files as part of this investigation. The Qwen weight-space interpretation experiment remains separate and is not the active task.

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
