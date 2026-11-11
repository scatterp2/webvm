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

The RX/TX proof itself does not depend on notebook egress. The Stage 2 TCP service enables notebook internet because it must download Tailscale and open outbound TCP connections. Controller-side Kaggle API calls still require controller network access and credentials.

## Documented CLI references

- Dataset creation/versioning: https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets.md
- Dataset metadata: https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md
- Kernel metadata and dataset_sources: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
- Kernel status and output download: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md

Kaggle's current notebook documentation states 12 hours maximum execution time for CPU/GPU sessions (9 hours for TPU sessions) and up to 20 GB of saved output in /kaggle/working. Kaggle documents dynamic API rate limiting but does not publish one fixed universal numeric request quota in the API overview; handle HTTP 429 / Too Many Requests by pausing and retrying rather than tight-looping. These are platform-level documented limits, not limits measured by this proof. Dataset/API upload size ceilings and effective per-account request rates still need an empirical test and current policy check.


## Stage 2: live TCP bridge over Tailscale

The RX/TX proof above is only a file/message round trip. The live transport is a separate, long-running private Kaggle notebook, `USERNAME/webvm-tcp-link`, which runs an HTTP CONNECT + SOCKS5 proxy on local port 8889 and exposes it to the tailnet through Tailscale Serve on port 18088, keeping it distinct from Jupyter's port 8888. WebVM configures applications to use this explicit SOCKS5/HTTP proxy. This is not transparent packet interception: the Kaggle runtime runs unprivileged and cannot change NAT rules, so transparent `iptables` redirection is unavailable there.

### One-time setup

1. Run WebVM once after the Stage 2 code has deployed. It creates the private `webvm-tcp-link` notebook and reports whether the Tailscale secret is missing.
2. Open the private Kaggle notebook at `https://www.kaggle.com/code/USERNAME/webvm-tcp-link`.
3. In the notebook's Secrets settings, add `WEBVM_TAILSCALE_AUTHKEY` with a reusable ephemeral Tailscale auth key for the same tailnet as WebVM (so the node can rejoin after a Kaggle runtime restart). Do not paste the key into source code, the RX dataset, the TX response, or logs.
4. Reload WebVM or run `pi.py` again. It reuses the running service if one is already active. The notebook reads the key directly from Kaggle Secrets and keeps the service alive.
5. In WebVM's Networking panel, wait for `webvm-kaggle-tcp-link` and its Tailscale IPv4 address to appear. The browser writes `/data/kaggle-tcp-link.env`; new shell prompts load it and set `ALL_PROXY=socks5h://<peer-ip>:18088` if the user has not configured another proxy.

The notebook reports non-secret stages through the existing runtime telemetry: installation, local SOCKS5 TCP self-test, Tailscale Serve status, the peer IPv4 address, and any startup error. The auth key is never returned over RX/TX or included in telemetry.

### What this link provides

This is a real TCP stream path from WebVM over its Tailscale network to the Kaggle peer, then through either HTTP `CONNECT` or SOCKS5 `CONNECT` to public TCP destinations. It is not the old file-based RX/TX loop, and it does not rely on a guessed source IP or NAT redirect. UDP and arbitrary layer-3 packet forwarding are not provided by this SOCKS5 mode; applications must honor the proxy environment variables. HTTP clients use `HTTP_PROXY`/`HTTPS_PROXY`; SOCKS-aware clients use `ALL_PROXY`. The service remains available only while the Kaggle notebook is running and subject to Kaggle's session limits.

## Stage 3: actual WebVM-to-Kaggle-proxy marker and same-connection reply

This is the active end-to-end networking experiment; it is distinct from the Dataset artifact loop in Stage 1 and the Tailscale SOCKS5 bridge in Stage 2.

### Required sequence

1. Kaggle starts the receiver/capture before WebVM transmits and publishes a `RECEIVER_READY` event containing a per-run ID and the observation layer.
2. WebVM sends a unique marker encoded as hex in an HTTP request to the actual Kaggle Jupyter proxy host and route. Record destination host/route shape, request ID, byte count, start/end time, connection outcome, and HTTP status. Do not put proxy tokens or other secrets in logs.
3. Kaggle-side evidence must say whether the marker was observed and where: an HTTP handler or socket sees application bytes; a packet capture sees only packets that traverse the captured interface/namespace. The upstream HTTPS reverse proxy may terminate TLS outside the notebook namespace, so an HTTP response or error alone must not be described as notebook-side packet capture.
4. On the same still-open connection, Kaggle returns a second unique marker. WebVM verifies exact response bytes and logs the matching run ID.
5. Emit a final verdict: `PASS_PROXY_MARKER_ROUND_TRIP` only if the outbound marker is correlated with receiver-side evidence and the response marker arrives back on the same connection. Otherwise emit a stage-specific failure with actual exception/status/capture counts.

### Interpretation rules

- A successful Dataset upload/download proves only API-mediated file transfer.
- A successful kernel push or `RUNNING` status proves only submission/execution status.
- `peer_ip_not_ready` means the separate Tailscale peer-address lookup did not produce an address; it is not an attempted marker transmission.
- The existing SOCKS5 greeting test against tailnet port 18088 is a separate test. It must not be used as the result for the Kaggle proxy-address experiment.
- If the proxy rejects or drops the HTTP request before it reaches the notebook, report that as an observed proxy-path result and explain what the available evidence can and cannot establish. Do not claim the Kaggle notebook interface received bytes unless the notebook-side capture/handler actually observed them.
- Keep the test bounded, use non-sensitive marker data, avoid changing firewall/server state, and do not print tokens or credentials.

Implementation note: the WebVM bootstrap fetches `tcp_socks5.py` from the repository and injects a small, bounded marker-validation branch into the in-memory/local copy before the Kaggle notebook is generated. The repository copy of the proxy service is left unchanged. The branch records `MARKER_RECEIVED`, validates the frame hex and SHA-256, then intentionally returns HTTP 501 with the same marker ID and digest; the WebVM sender verifies that response on the same socket.
