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

Limits such as maximum runtime, output-size ceilings and request rate are not asserted here: check current Kaggle documentation/account policy and measure empirically rather than guessing.
