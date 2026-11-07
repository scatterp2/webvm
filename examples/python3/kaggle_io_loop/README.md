# Kaggle API-mediated RX/TX loop proof (stage 1)

This is a minimal, harmless one-message proof. It does not implement a tunnel, proxy, or stream layer.

## What it tests

1. The controller writes a fixed message into a Kaggle Dataset resource (rx_message.json) and publishes it with the documented Kaggle CLI dataset API.
2. A self-contained notebook reads the mounted dataset input, validates protocol version, UTF-8 payload length, request ID, sequence and SHA-256 checksum.
3. The notebook writes one tx_response.json output file containing the acknowledgement and a checksum.
4. The controller waits for kernel status, downloads output using kaggle kernels output, and validates the response.
5. The controller logs each Kaggle CLI/API operation, duration, exit code, status poll and output validation to run.jsonl.

## Run from the controller (WebVM)

Install the Kaggle CLI in the controller environment and configure credentials there only. Do not put Kaggle credentials in the notebook, dataset, repository or diagnostic logs.

First run, creating a new private dataset:
    python3 examples/python3/kaggle_io_loop/controller.py --dataset-id YOUR_KAGGLE_USERNAME/webvm-io-loop-rx --kernel-id YOUR_KAGGLE_USERNAME/webvm-io-loop-proof

If the private dataset already exists, publish a new version instead:
    python3 examples/python3/kaggle_io_loop/controller.py --dataset-id YOUR_KAGGLE_USERNAME/webvm-io-loop-rx --kernel-id YOUR_KAGGLE_USERNAME/webvm-io-loop-proof --dataset-exists

Artifacts are kept in ./kaggle_io_loop_run/ by default. The controller fails if status times out, the output file is missing, or any response/checksum/sequence/request ID does not match.

## Current protocol

Message fields: version, sequence, request_id, type, payload_length, payload, checksum. The TX adds ack_state, received_checksum, receive time and agent elapsed time. SHA-256 covers canonical JSON of all protocol fields except the checksum itself.

This first stage intentionally sends one fixed, harmless message and downloads one output file. Duplicate detection is in-memory for this one execution; it does not claim durable exactly-once delivery across restarts. Retries, bounded queues, payload ceilings, rate/latency characterization and repeated live exchanges are follow-up tests.

## Critical live-update question

A Dataset attached as dataset_sources is a run input snapshot. The documented CLI supports publishing a new Dataset version (kaggle datasets version) and downloading Dataset versions, but these commands alone do not document a way to refresh an already-running notebook's mounted /kaggle/input tree. This stage proves the controller→published resource→notebook input→output artifact→controller loop for one run; it does not prove that a running notebook sees later Dataset versions without restart. Stage 2 must test that separately and report a failure if the mount remains unchanged.

Notebook internet is disabled in this proof because no outbound network access is required. No DNS result is expected from the notebook in that configuration. The controller-side Kaggle API calls still require controller network access and credentials.

## Documented CLI references

- Dataset creation/versioning: https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets.md
- Dataset metadata: https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md
- Kernel metadata and dataset_sources: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
- Kernel status and output download: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md

Kaggle's current notebook documentation states 12 hours maximum execution time for CPU/GPU sessions (9 hours for TPU sessions) and up to 20 GB of saved output in /kaggle/working. Kaggle documents dynamic API rate limiting but does not publish one fixed universal numeric request quota in the API overview; handle HTTP 429 / Too Many Requests by pausing and retrying rather than tight-looping. These are platform-level documented limits, not limits measured by this proof. Dataset/API upload size ceilings and effective per-account request rates still need an empirical test and current policy check.


## Stage 2: live TCP bridge over Tailscale

The RX/TX proof above is only a file/message round trip. The live transport is a separate, long-running private Kaggle notebook, `USERNAME/webvm-tcp-link`, which runs a TCP-only SOCKS5 proxy on local port 8889 and exposes it to the tailnet through Tailscale Serve on port 8888. No `iptables` PREROUTING rule is required.

### One-time setup

1. Run WebVM once after the Stage 2 code has deployed. It creates the private `webvm-tcp-link` notebook and reports whether the Tailscale secret is missing.
2. Open the private Kaggle notebook at `https://www.kaggle.com/code/USERNAME/webvm-tcp-link`.
3. In the notebook's Secrets settings, add `WEBVM_TAILSCALE_AUTHKEY` with a fresh, preferably ephemeral Tailscale auth key for the same tailnet as WebVM. Do not paste the key into source code, the RX dataset, the TX response, or logs.
4. Reload WebVM or run `pi.py` again. It reuses the running service if one is already active. The notebook reads the key directly from Kaggle Secrets and keeps the service alive.
5. In WebVM's Networking panel, wait for `webvm-kaggle-tcp-link` and its Tailscale IPv4 address to appear. The browser writes `/data/kaggle-tcp-link.env`; new shell prompts load it and set `ALL_PROXY=socks5h://<peer-ip>:8888` if the user has not configured another proxy.

The notebook reports non-secret stages through the existing runtime telemetry: installation, local SOCKS5 TCP self-test, Tailscale Serve status, the peer IPv4 address, and any startup error. The auth key is never returned over RX/TX or included in telemetry.

### What this link provides

This is a real TCP stream path from WebVM over its Tailscale network to the Kaggle peer, then through a SOCKS5 `CONNECT` proxy to public TCP destinations. It is not the old file-based RX/TX loop, and it does not rely on a guessed source IP or NAT redirect. UDP and arbitrary layer-3 packet forwarding are not provided by this SOCKS5 mode; applications must honor the SOCKS/HTTP proxy environment variables. The service remains available only while the Kaggle notebook is running and subject to Kaggle's session limits.
