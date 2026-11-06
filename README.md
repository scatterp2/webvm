# AI Agent Handoff / Operating Notes

> Keep this section current when changing the reset mechanism or Qwen experiment. These notes are intended to let an AI agent continue the work without asking the user to repeat repository context.

## Current objective: reproduce the Qwen weight-space interpretation experiment

The goal is to reproduce the experiment described by [RikiyaT/lm_understand-logs](https://github.com/RikiyaT/lm_understand-logs), using **Qwen/Qwen3.8-27B** on Kaggle and the four game prompts **Pac-Man, Pong, Breakout, and Freeway**. Capture the model's responses and execution details so they can be compared against the source experiment. This is an interpretation/replication experiment: the current code sends prompts to the model; it does **not** directly edit Qwen's learned weights or the other model's parameters.

Relevant files:
- `examples/python3/pi.py` — prepares the Kaggle notebook, embeds the current runtime and all four source prompts at submission time, explicitly requests a T4 GPU, submits the run, checks status, and downloads the real Kaggle logs/output after completion or failure.
- `examples/python3/gpu_test.py` — legacy standalone bootstrap that fetches the runtime from raw GitHub. The main `pi.py` submission path no longer uses it, because the latest run showed raw-GitHub DNS resolution failing inside Kaggle.
- `examples/python3/kaggle_runtime.py` — experiment implementation, GPU preflight diagnostics, inference, and result logging. The submission script embeds this source into the Kaggle notebook and embeds the prompts from the four source files.
- `examples/python3/pi_feedback.py` — wraps the submission script and publishes a redacted rolling log snapshot to pste.us for remote inspection.
- `.github/workflows/runtime-feedback-monitor.yml` — polls pste.us for the unique tag `WVMQWENWS_6D91C42F` and prints matching diagnostic logs into GitHub Actions.
- `monitor-check-2026-11-06.txt` — reminder to inspect actual Kaggle execution output, not just heartbeat messages.

## How the WebVM reset is triggered and verified

The reset source of truth is the plain-text `reset` file on **`main`**. Put a new generation number in that file (for example, change `1591` to `1592`) and commit it. The deployed page in `src/app.html` fetches that raw GitHub file with cache-busting every **15 seconds**, and also checks on page startup and `pageshow`. It compares the generation against `localStorage` key `webvm-reset-last-source-version`; a new generation is added to the URL as `?reset=<generation>` and dispatched as a `webvm-reset-request` event. The WebVM reset handler in `src/lib/WebVM.svelte` clears the disk cache and reloads the page.

Important failure-handling rule: the page-head code records a generation before dispatching it to avoid reload loops. If the disk-cache reset then fails, the failure handler must clear that same generation marker so the next poll can retry it. Do not treat the “already handled” log as proof that the disk cache was reset; verify the complete sequence in the browser console:
1. `New generation N; requesting a disk-cache reset.`
2. Disk-cache reset begins.
3. Disk-cache reset completes and the WebVM reloads.
4. After reload, the URL includes `?reset=N` and the same generation does not cause a reset loop.

The user has confirmed that changing the reset file to generation **1592** triggered the browser restart automatically. Do not ask the user to press Reset for normal generation changes; use the committed generation number and verify the resulting logs.

## How to inspect runtime logs and what the latest failure showed

Use the **`runtime-feedback-monitor` branch** for the Kaggle experiment and log-monitoring code. Inspect the latest GitHub Actions run for **Runtime feedback monitor**, open the `poll-pste-feedback` job, and search its output for `FEEDBACK_MATCH`, `GPU_PREFLIGHT`, `MODEL_LOADING`, `INFERENCE_STARTED`, `ANSWER_RESULT`, and `RUN_FAILED`. The monitor reads pste.us entries tagged `WVMQWENWS_6D91C42F`; pste is a transport for diagnostics, not the ultimate source of truth. The submission script also fetches `kaggle kernels logs simonday/automated-gpu-test` and the kernel output files so the real Kaggle run can be diagnosed even when the Kaggle-side upload to pste fails.

Two concrete failures have been observed in Kaggle runs: (1) a batch run had no `/dev/nvidia*` devices and installed `torch 2.11.0+cpu` (`torch.version.cuda=None`), so it stopped before loading Qwen; (2) a later run failed before the experiment started because Kaggle could not resolve `raw.githubusercontent.com` while executing the legacy bootstrap. The submission now explicitly requests `--accelerator NvidiaTeslaT4`, sets `machine_shape`, embeds the runtime and all four prompts into the notebook before submission, and collects Kaggle logs/output on success, failure, or timeout. The next fresh run must verify GPU allocation and Hugging Face/package network access; a successful GitHub Actions workflow alone does not prove model inference succeeded.

**Agent workflow:** inspect the newest logs first; identify the earliest concrete failure; make a focused fix; commit it; wait for the workflow to finish; inspect its logs again; repeat until GPU preflight passes, the model loads, all four prompts are attempted, and the outputs are captured. Do not ask the user to retrieve logs that the repository tooling can fetch itself.

---

# Todo 
check out new code colab_worker.ipynb libpnp stuff needs merging to jupyter.ipynb the llamacpp rpc could only stay as an option in a framework and then
should not be default behavior 
# Browser Compute Project

A WebVM-based browser compute environment with WebGPU virtual compute capabilities.

## Overview

This [project](https://scatterp2.github.io/webvm/#authKey=tskey-auth-ktAfFfhKgK11CNTRL-MXoh6jmnuvSQ7XtfUYwQySr2q3wEkhfb) extends [WebVM](https://webvm.io) — a serverless Linux virtual machine running in your browser via WebAssembly — with additional compute capabilities and automation tooling.

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
