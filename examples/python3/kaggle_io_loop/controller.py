#!/usr/bin/env python3
"""Controller for the one-message Kaggle Dataset -> notebook -> output proof."""
import argparse, hashlib, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE / "agent.py"
DEFAULT_WORK = Path.cwd() / "kaggle_io_loop_run"
FIXED_PAYLOAD = "WEBVM_IO_LOOP_HELLO_V1"

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

def safe_text(value):
    value = str(value or "")
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", value)
    return value[-4000:]

class Logger:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
    def event(self, event, **fields):
        row = {"time": now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        print("IO_LOOP_CONTROLLER " + json.dumps(row, sort_keys=True, ensure_ascii=False), flush=True)

def command(logger, argv, *, timeout=120, check=False):
    start = time.monotonic()
    logger.event("API_REQUEST_START", command=argv, timeout_seconds=timeout)
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout, check=False, env=os.environ.copy())
        output = safe_text(result.stdout)
        logger.event("API_REQUEST_RESULT", command=argv, exit_code=result.returncode,
                     duration_ms=round((time.monotonic() - start) * 1000, 3), output_tail=output)
        if check and result.returncode != 0:
            raise RuntimeError("Command failed: " + " ".join(argv) + " (exit " + str(result.returncode) + ")")
        return result
    except subprocess.TimeoutExpired as exc:
        logger.event("API_REQUEST_TIMEOUT", command=argv, duration_ms=round((time.monotonic() - start) * 1000, 3),
                     output_tail=safe_text(exc.stdout))
        raise

def make_message():
    core = {"version": 1, "sequence": 1, "request_id": "io-loop-proof-0001",
            "type": "echo", "payload_length": len(FIXED_PAYLOAD.encode("utf-8")), "payload": FIXED_PAYLOAD}
    return {**core, "checksum": digest(core)}

def make_notebook(agent_source):
    return {
        "cells": [{"cell_type": "code", "id": "io-loop-agent", "execution_count": None, "metadata": {}, "outputs": [],
                   "source": agent_source.splitlines(keepends=True)}],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "kaggle": {"accelerator": None, "dataSources": [], "isInternetEnabled": False,
                       "isGpuEnabled": False, "language": "python", "sourceType": "notebook"},
        },
        "nbformat": 4, "nbformat_minor": 5,
    }

def validate_output(path, message, logger):
    if not path.is_file():
        logger.event("OUTPUT_VALIDATION_FAILED", reason="tx_response.json missing", path=str(path))
        raise RuntimeError("Kaggle output did not contain tx_response.json")
    response = json.loads(path.read_text(encoding="utf-8"))
    checksum = response.pop("checksum", None)
    if checksum != digest(response):
        logger.event("OUTPUT_VALIDATION_FAILED", reason="TX checksum mismatch")
        raise RuntimeError("TX checksum mismatch")
    checks = {
        "version": response.get("version") == message["version"],
        "sequence": response.get("sequence") == message["sequence"],
        "request_id": response.get("request_id") == message["request_id"],
        "ack_state": response.get("ack_state") == "processed_once",
        "received_checksum": response.get("received_checksum") == message["checksum"],
        "payload": response.get("payload") == "ACK:" + message["payload"],
        "payload_length": response.get("payload_length") == len(response.get("payload", "").encode("utf-8")),
    }
    logger.event("OUTPUT_VALIDATION", checks=checks, response=response)
    if not all(checks.values()):
        raise RuntimeError("TX response validation failed: " + repr(checks))
    response["checksum"] = checksum
    return response

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", required=True, help="Kaggle dataset ID, e.g. username/webvm-io-loop-rx")
    parser.add_argument("--kernel-id", required=True, help="Kaggle kernel ID, e.g. username/webvm-io-loop-proof")
    parser.add_argument("--dataset-exists", action="store_true", help="Publish a new version instead of creating the Dataset")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--prepare-only", action="store_true", help="Generate inputs/notebook and run local validation without Kaggle API calls")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", args.dataset_id):
        parser.error("invalid --dataset-id; expected owner/slug")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", args.kernel_id):
        parser.error("invalid --kernel-id; expected owner/slug")

    work = args.work_dir.resolve()
    dataset_dir, kernel_dir, output_dir = work / "dataset", work / "kernel", work / "output"
    log = Logger(work / "run.jsonl")
    work.mkdir(parents=True, exist_ok=True)
    round_trip_started = time.monotonic()
    log.event("RUN_BEGIN", dataset_id=args.dataset_id, kernel_id=args.kernel_id,
              timeout_seconds=args.timeout_seconds, poll_seconds=args.poll_seconds)
    message = make_message()
    write_json(dataset_dir / "rx_message.json", message)
    write_json(dataset_dir / "dataset-metadata.json", {
        "title": "WebVM IO Loop RX", "id": args.dataset_id,
        "licenses": [{"name": "CC0-1.0"}],
        "description": "Private harmless fixed-message RX resource for WebVM Kaggle IO loop proof.",
    })
    agent_source = AGENT.read_text(encoding="utf-8")
    dataset_slug = args.dataset_id.split("/", 1)[1]
    agent_source = agent_source.replace("/kaggle/input/webvm-io-loop-rx/rx_message.json", "/kaggle/input/" + dataset_slug + "/rx_message.json")
    compile(agent_source, str(AGENT), "exec")
    notebook_path = kernel_dir / "io_loop_proof.ipynb"
    metadata_path = kernel_dir / "kernel-metadata.json"
    write_json(notebook_path, make_notebook(agent_source))
    write_json(metadata_path, {
        "id": args.kernel_id, "title": "WebVM IO Loop Proof", "code_file": notebook_path.name,
        "language": "python", "kernel_type": "notebook", "is_private": True,
        "enable_gpu": False, "enable_internet": False, "machine_shape": "",
        "dataset_sources": [args.dataset_id], "competition_sources": [], "kernel_sources": [], "model_sources": [],
    })
    log.event("POC_FILES_WRITTEN", rx_bytes=(dataset_dir / "rx_message.json").stat().st_size,
              notebook_bytes=notebook_path.stat().st_size, payload=message["payload"],
              request_id=message["request_id"], sequence=message["sequence"], checksum=message["checksum"])
    if args.prepare_only:
        local_output = work / "local-tx-response.json"
        env = os.environ.copy()
        env["WEBVM_IO_LOOP_INPUT"] = str(dataset_dir / "rx_message.json")
        env["WEBVM_IO_LOOP_OUTPUT"] = str(local_output)
        result = subprocess.run([sys.executable, str(AGENT)], env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        log.event("LOCAL_AGENT_RESULT", exit_code=result.returncode, output_tail=safe_text(result.stdout))
        if result.returncode:
            raise RuntimeError("Local agent validation failed")
        validate_output(local_output, message, log)
        log.event("PREPARE_ONLY_PASS", note="Local protocol test passed; Kaggle API not called")
        return

    if args.dataset_exists:
        dataset_exists = True
    else:
        probe = command(log, ["kaggle", "datasets", "status", args.dataset_id], timeout=60, check=False)
        dataset_exists = probe.returncode == 0
        log.event("RX_DATASET_EXISTENCE_PROBE", dataset_id=args.dataset_id, exists=dataset_exists)
    publish_cmd = (["kaggle", "datasets", "version", "-p", str(dataset_dir),
                    "-m", "WebVM IO loop RX message sequence 1", "-r", "skip"]
                   if dataset_exists else
                   ["kaggle", "datasets", "create", "-p", str(dataset_dir), "-r", "skip"])
    command(log, publish_cmd, timeout=300, check=True)
    log.event("RX_RESOURCE_PUBLISHED", dataset_id=args.dataset_id, message_checksum=message["checksum"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata["dataset_sources"] != [args.dataset_id]:
        raise RuntimeError("Notebook dataset_sources does not match published RX Dataset")
    command(log, ["kaggle", "kernels", "push", "-p", str(kernel_dir)], timeout=300, check=True)
    log.event("KERNEL_SUBMITTED", kernel_id=args.kernel_id)
    deadline, final_status = time.monotonic() + args.timeout_seconds, None
    while time.monotonic() < deadline:
        status = command(log, ["kaggle", "kernels", "status", args.kernel_id], timeout=60, check=False)
        status_text = status.stdout or ""
        log.event("EXECUTION_STATUS", kernel_id=args.kernel_id, exit_code=status.returncode,
                  status=status_text.strip()[-1000:])
        upper = status_text.upper()
        if "KERNELWORKERSTATUS.COMPLETE" in upper or re.search(r"\bCOMPLETE\b", upper):
            final_status = "COMPLETE"
            break
        if "KERNELWORKERSTATUS.ERROR" in upper or "KERNELWORKERSTATUS.CANCEL" in upper:
            final_status = "ERROR"
            break
        time.sleep(max(1, args.poll_seconds))
    output_dir.mkdir(parents=True, exist_ok=True)
    if final_status != "COMPLETE":
        log.event("EXECUTION_NOT_COMPLETE", final_status=final_status or "TIMEOUT")
        # Preserve any available execution artifacts even on failure. This can
        # expose a traceback or partial notebook output that would otherwise be
        # lost when the controller exits at the status check.
        diagnostic_dir = output_dir / "diagnostics"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic = command(log, ["kaggle", "kernels", "output", args.kernel_id, "-p",
                                   str(diagnostic_dir), "-o"], timeout=300, check=False)
        diagnostic_files = sorted(p for p in diagnostic_dir.rglob("*") if p.is_file())
        files = [str(p.relative_to(diagnostic_dir)) for p in diagnostic_files]
        log.event("FAILURE_ARTIFACTS_DOWNLOAD", exit_code=diagnostic.returncode, files=files,
                  output_tail=safe_text(diagnostic.stdout))
        # The Kaggle CLI downloads the execution log as a local file. Include
        # its actual contents in WebVM telemetry so a remote run's traceback
        # is observable instead of reporting only the filename.
        for artifact in diagnostic_files:
            if artifact.suffix.lower() in (".log", ".txt", ".json", ".jsonl"):
                try:
                    artifact_text = artifact.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    log.event("FAILURE_ARTIFACT_READ_ERROR", file=str(artifact.relative_to(diagnostic_dir)),
                              error=type(exc).__name__ + ": " + str(exc))
                    continue
                log.event("FAILURE_ARTIFACT_CONTENT", file=str(artifact.relative_to(diagnostic_dir)),
                          bytes=artifact.stat().st_size, content=safe_text(artifact_text))
        # The CLI's output endpoint may not expose the execution traceback.
        # Fetch the published notebook definition as a second diagnostic and
        # record it locally, without ever treating it as successful execution.
        pull_dir = output_dir / "kernel_source"
        pull_dir.mkdir(parents=True, exist_ok=True)
        pull = command(log, ["kaggle", "kernels", "pull", args.kernel_id, "-p",
                             str(pull_dir), "-m"], timeout=120, check=False)
        pulled = sorted(str(p.relative_to(pull_dir)) for p in pull_dir.rglob("*") if p.is_file())
        log.event("FAILURE_KERNEL_SOURCE_PULL", exit_code=pull.returncode, files=pulled,
                  output_tail=safe_text(pull.stdout))
        raise RuntimeError("Kaggle kernel did not complete successfully: " + str(final_status or "timeout") +
                           "; diagnostics=" + str(diagnostic_dir) + "; kernel_source=" + str(pull_dir))
    command(log, ["kaggle", "kernels", "output", args.kernel_id, "-p", str(output_dir),
                  "-o", "--file-pattern", "^tx_response\\.json$"], timeout=300, check=True)
    response = validate_output(output_dir / "tx_response.json", message, log)
    log.event("LOOP_PROOF_PASS", request_id=message["request_id"], sequence=message["sequence"],
              rx_checksum=message["checksum"], tx_checksum=response["checksum"],
              round_trip_ms=round((time.monotonic() - round_trip_started) * 1000, 3),
              payload_bytes=message["payload_length"], output_bytes=(output_dir / "tx_response.json").stat().st_size,
              output_file=str(output_dir / "tx_response.json"))
    print("LOOP_PROOF_PASS output=" + str(output_dir / "tx_response.json") + " log=" + str(work / "run.jsonl"), flush=True)

if __name__ == "__main__":
    main()
