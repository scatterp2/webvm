#!/usr/bin/env python3
"""Controller for the Kaggle RX/TX proof and optional live Tailscale TCP link."""
import argparse, hashlib, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE / "agent.py"
DEFAULT_WORK = Path.cwd() / "kaggle_io_loop_run"
FIXED_PAYLOAD = "WEBVM_IO_LOOP_HELLO_V1"

PROXY_DIAGNOSTIC_SOURCE = r'''# WebVM proxy URL and NAT capability diagnostic
import glob, json, os, re, shutil, socket, subprocess

def _proxy_log(event, **fields):
    print("WEBVM_PROXY_DIAGNOSTIC " + json.dumps(
        {"event": event, **fields}, sort_keys=True
    ), flush=True)

base_url = None
server_pid = None
pids = [12] + [
    int(p.split("/")[2]) for p in glob.glob("/proc/[0-9]*/cmdline")
    if p.split("/")[2].isdigit() and int(p.split("/")[2]) != 12
]
for pid in pids:
    try:
        with open("/proc/{}/cmdline".format(pid), "rb") as f:
            args = [x.decode(errors="replace") for x in f.read().split(b"\0") if x]
    except (OSError, PermissionError):
        continue
    if not any("jupyter" in x.lower() or "notebook" in x.lower() for x in args):
        continue
    for i, arg in enumerate(args):
        if arg.startswith(("--NotebookApp.base_url=", "--ServerApp.base_url=")):
            base_url = arg.split("=", 1)[1]
            server_pid = pid
            break
        if arg in ("--NotebookApp.base_url", "--ServerApp.base_url") and i + 1 < len(args):
            base_url = args[i + 1]
            server_pid = pid
            break
    if base_url:
        break

if base_url:
    match = re.search(r"/k/\d+/([^/]+)/proxy/?$", base_url)
    if match:
        _proxy_log("PROXY_URL_DISCOVERED", pid=server_pid,
                   proxy_host="kkb-production.jupyter-proxy.kaggle.net",
                   proxy_route_shape="/k/{}/<redacted>/proxy".format(re.search(r"/k/(\d+)/", base_url).group(1)),
                   token_present=bool(match.group(1)))
    else:
        _proxy_log("BASE_URL_FOUND_UNEXPECTED_FORMAT", pid=server_pid, base_url_shape="present_unrecognized_format")
else:
    _proxy_log("JUPYTER_BASE_URL_NOT_FOUND", checked_pid_12=True, proc_scan=True)

source_ip_configured = bool(os.environ.get("WEBVM_PROXY_SOURCE_IP", "").strip())
_proxy_log("NAT_RULE_NOT_USED", source_ip_configured=source_ip_configured,
           note="The TCP link uses authenticated Tailscale Serve; no PREROUTING redirect is required")

try:
    with socket.create_connection(("127.0.0.1", 8889), timeout=2):
        _proxy_log("LOCAL_TARGET_LISTENER", port=8889, listening=True)
except OSError as exc:
    _proxy_log("LOCAL_TARGET_LISTENER", port=8889, listening=False,
               error=type(exc).__name__ + ": " + str(exc))
'''

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
    """Run Kaggle CLI with a bounded proxy-bypass retry for read-only API calls."""
    start = time.monotonic()
    read_only = len(argv) >= 3 and (
        (argv[1:3] == ["datasets", "status"]) or
        (argv[1:3] == ["datasets", "download"]) or
        (argv[1:3] == ["kernels", "status"]) or
        (argv[1:3] == ["kernels", "output"])
    )
    attempts = [("configured_environment", os.environ.copy())]
    if read_only:
        direct_env = os.environ.copy()
        proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                       "http_proxy", "https_proxy", "all_proxy")
        removed = [name for name in proxy_names if direct_env.get(name)]
        for name in proxy_names:
            direct_env.pop(name, None)
        direct_env["NO_PROXY"] = "api.kaggle.com,www.kaggle.com,.kaggle.com"
        direct_env["no_proxy"] = direct_env["NO_PROXY"]
        attempts.append(("proxy_bypass", direct_env))
    last_timeout = None
    for attempt_name, env in attempts:
        attempt_start = time.monotonic()
        logger.event("API_REQUEST_START", command=argv, timeout_seconds=timeout,
                     attempt=attempt_name, proxy_vars_removed=(attempt_name == "proxy_bypass"))
        try:
            result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout, check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
            logger.event("API_REQUEST_TIMEOUT", command=argv,
                         duration_ms=round((time.monotonic() - attempt_start) * 1000, 3),
                         attempt=attempt_name, output_tail=safe_text(exc.stdout))
            continue
        output = safe_text(result.stdout)
        logger.event("API_REQUEST_RESULT", command=argv, exit_code=result.returncode,
                     duration_ms=round((time.monotonic() - attempt_start) * 1000, 3),
                     attempt=attempt_name, output_tail=output)
        if check and result.returncode != 0:
            raise RuntimeError("Command failed: " + " ".join(argv) + " (exit " + str(result.returncode) + ")")
        return result
    if last_timeout is not None:
        logger.event("API_REQUEST_EXHAUSTED", command=argv, attempts=len(attempts),
                     elapsed_ms=round((time.monotonic() - start) * 1000, 3),
                     read_only=read_only, reason="all permitted attempts timed out")
        raise TimeoutError("Kaggle read-only API command timed out in configured and proxy-bypass attempts: " + " ".join(argv))
    raise RuntimeError("Kaggle command failed without a result")

def make_message():
    # A per-run request ID proves the published Dataset version is current.
    request_id = "io-loop-proof-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    core = {"version": 1, "sequence": 1, "request_id": request_id,
            "type": "echo", "payload_length": len(FIXED_PAYLOAD.encode("utf-8")), "payload": FIXED_PAYLOAD}
    return {**core, "checksum": digest(core)}

def make_notebook(agent_source):
    return {
        "cells": [{"cell_type": "code", "id": "io-loop-agent", "execution_count": None, "metadata": {}, "outputs": [],
                   "source": agent_source.splitlines(keepends=True)}],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "kaggle": {"accelerator": None, "dataSources": [], "isInternetEnabled": True,
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
    parser.add_argument("--serve-tcp-link", action="store_true",
                        help="Submit a long-running Tailscale-backed TCP/SOCKS5 service and return after submission")
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
    if args.serve_tcp_link and not args.prepare_only:
        existing = command(log, ["kaggle", "kernels", "status", args.kernel_id],
                           timeout=60, check=False)
        existing_text = (existing.stdout or "").upper()
        active_state = any(state in existing_text for state in
                           ("RUNNING", "QUEUED", "PENDING", "INITIALIZING", "SUBMITTED"))
        if existing.returncode == 0 and active_state:
            log.event("TCP_LINK_ALREADY_RUNNING", kernel_id=args.kernel_id,
                      status_shape="active",
                      note="Keeping the existing queued or running TCP link; not pushing another version")
            print("TCP_LINK_ALREADY_RUNNING kernel_id=" + args.kernel_id, flush=True)
            return
    if args.serve_tcp_link:
        message = None
        agent_source = ""
    else:
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
    if args.serve_tcp_link:
        service_path = HERE / "tcp_socks5.py"
        if not service_path.is_file():
            raise RuntimeError("TCP link service source is missing: " + str(service_path))
        service_source = service_path.read_text(encoding="utf-8")
        compile(service_source, str(service_path), "exec")
        notebook_source = PROXY_DIAGNOSTIC_SOURCE + "\n\n" + service_source
    else:
        notebook_source = PROXY_DIAGNOSTIC_SOURCE + "\n\n" + agent_source
    compile(notebook_source, str(notebook_path), "exec")
    write_json(notebook_path, make_notebook(notebook_source))
    write_json(metadata_path, {
        "id": args.kernel_id, "title": ("WebVM TCP Link" if args.serve_tcp_link else "WebVM IO Loop Proof"), "code_file": notebook_path.name,
        "language": "python", "kernel_type": "notebook", "is_private": True,
        "enable_gpu": False, "enable_internet": True, "machine_shape": "",
        "dataset_sources": ([] if args.serve_tcp_link else [args.dataset_id]), "competition_sources": [], "kernel_sources": [], "model_sources": [],
    })
    if args.serve_tcp_link:
        log.event("POC_FILES_WRITTEN", notebook_bytes=notebook_path.stat().st_size,
                  service="tailscale-serve-tcp-to-http-connect-socks5")
    else:
        log.event("POC_FILES_WRITTEN", rx_bytes=(dataset_dir / "rx_message.json").stat().st_size,
                  notebook_bytes=notebook_path.stat().st_size, payload=message["payload"],
                  request_id=message["request_id"], sequence=message["sequence"], checksum=message["checksum"])
    if args.prepare_only and args.serve_tcp_link:
        log.event("PREPARE_ONLY_PASS", note="TCP link notebook generated; Kaggle API not called")
        return
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

    if not args.serve_tcp_link:
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
        # Dataset version creation is asynchronous. Wait until the latest version
        # can be downloaded and matches this run before submitting the notebook.
        verify_dir = output_dir / "published_rx_verify"
        verify_dir.mkdir(parents=True, exist_ok=True)
        verify_deadline = time.monotonic() + 240
        published_verified = False
        while time.monotonic() < verify_deadline:
            verify = command(log, ["kaggle", "datasets", "download", args.dataset_id,
                                   "-f", "rx_message.json", "-p", str(verify_dir),
                                   "--unzip", "-o"], timeout=90, check=False)
            candidates = sorted(verify_dir.rglob("rx_message.json"))
            if verify.returncode == 0 and candidates:
                try:
                    downloaded_message = json.loads(candidates[-1].read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    log.event("RX_PUBLICATION_VERIFY_RETRY", reason=type(exc).__name__ + ": " + str(exc))
                else:
                    if downloaded_message == message:
                        published_verified = True
                        log.event("RX_PUBLICATION_VERIFIED", dataset_id=args.dataset_id,
                                  request_id=message["request_id"], checksum=message["checksum"],
                                  file=str(candidates[-1]))
                        break
                    log.event("RX_PUBLICATION_VERIFY_RETRY", expected_request_id=message["request_id"],
                              observed_request_id=downloaded_message.get("request_id"),
                              expected_checksum=message["checksum"],
                              observed_checksum=downloaded_message.get("checksum"))
            else:
                log.event("RX_PUBLICATION_VERIFY_RETRY", exit_code=verify.returncode,
                          output_tail=safe_text(verify.stdout), files=[str(p) for p in candidates])
            time.sleep(10)
        if not published_verified:
            raise RuntimeError("Published Dataset did not expose this run's rx_message.json within 240 seconds; refusing to submit with stale or missing input")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata["dataset_sources"] != [args.dataset_id]:
            raise RuntimeError("Notebook dataset_sources does not match published RX Dataset")
    command(log, ["kaggle", "kernels", "push", "-p", str(kernel_dir)], timeout=300, check=True)
    log.event("KERNEL_SUBMITTED", kernel_id=args.kernel_id)
    if args.serve_tcp_link:
        log.event("TCP_LINK_KERNEL_SUBMITTED", kernel_id=args.kernel_id,
                  service="tailscale-serve-tcp-to-http-connect-socks5", local_port=8889,
                  tailnet_port=8888, wait_for_completion=False)
        print("TCP_LINK_KERNEL_SUBMITTED kernel_id=" + args.kernel_id +
              " local_port=8889 tailnet_port=8888", flush=True)

        # A missing Kaggle Secret makes the notebook finish quickly. Retrieve
        # its status artifact in that case; if it stays running, leave the
        # live TCP service alone and let runtime telemetry report readiness.
        startup_started = time.monotonic()
        startup_deadline = startup_started + 75
        last_status_text = ""
        while time.monotonic() < startup_deadline:
            status_result = command(
                log, ["kaggle", "kernels", "status", args.kernel_id],
                timeout=15, check=False)
            last_status_text = (status_result.stdout or "").upper()
            active_state = any(state in last_status_text for state in
                               ("RUNNING", "QUEUED", "PENDING", "INITIALIZING", "SUBMITTED"))
            terminal_state = any(state in last_status_text for state in
                                 ("COMPLETE", "ERROR", "FAILED", "CANCELLED"))
            if terminal_state and not active_state:
                status_dir = output_dir / "tcp_link_status"
                status_dir.mkdir(parents=True, exist_ok=True)
                for stale_status in status_dir.rglob("tcp_link_status.json"):
                    try:
                        stale_status.unlink()
                    except OSError:
                        pass
                output_result = command(
                    log,
                    ["kaggle", "kernels", "output", args.kernel_id, "-p",
                     str(status_dir), "-o", "--file-pattern", r"^tcp_link_status\.json$"],
                    timeout=120, check=False)
                status_files = sorted(
                    path for path in status_dir.rglob("tcp_link_status.json") if path.is_file())
                if status_files:
                    try:
                        status_doc = json.loads(status_files[0].read_text(encoding="utf-8"))
                        log.event("TCP_LINK_STARTUP_STATUS", kernel_id=args.kernel_id,
                                  status=status_doc.get("stage", "UNKNOWN"),
                                  details=status_doc)
                        print("TCP_LINK_STARTUP_STATUS " +
                              json.dumps(status_doc, sort_keys=True)[:2500], flush=True)
                    except (OSError, ValueError, TypeError) as exc:
                        log.event("TCP_LINK_STATUS_PARSE_ERROR",
                                  error=type(exc).__name__ + ": " + str(exc))
                else:
                    log.event("TCP_LINK_STATUS_ARTIFACT_MISSING",
                              kernel_id=args.kernel_id,
                              output_exit_code=output_result.returncode,
                              output_tail=safe_text(output_result.stdout)[-1000:])
                return
            if active_state and time.monotonic() - startup_started >= 35:
                log.event("TCP_LINK_KERNEL_RUNNING", kernel_id=args.kernel_id,
                          waited_seconds=round(time.monotonic() - startup_started, 1),
                          status_shape="active")
                print("TCP_LINK_KERNEL_RUNNING kernel_id=" + args.kernel_id, flush=True)
                return
            time.sleep(5)
        log.event("TCP_LINK_STARTUP_PENDING", kernel_id=args.kernel_id,
                  waited_seconds=round(time.monotonic() - startup_started, 1),
                  status_tail=last_status_text[-300:])
        print("TCP_LINK_STARTUP_PENDING kernel_id=" + args.kernel_id, flush=True)
        return
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

    # Download the executed notebook separately so the proxy/NAT diagnostic
    # emitted before the RX/TX agent is visible in the remote runtime log.
    notebook_dir = output_dir / "notebook_diagnostics"
    notebook_dir.mkdir(parents=True, exist_ok=True)
    notebook_download = command(
        log, ["kaggle", "kernels", "output", args.kernel_id, "-p",
              str(notebook_dir), "-o", "--file-pattern", r"^io_loop_proof\.ipynb$"],
        timeout=180, check=False)
    notebook_files = sorted(p for p in notebook_dir.rglob("*.ipynb") if p.is_file())
    log.event("NOTEBOOK_DIAGNOSTIC_DOWNLOAD", exit_code=notebook_download.returncode,
              files=[str(p.relative_to(notebook_dir)) for p in notebook_files],
              output_tail=safe_text(notebook_download.stdout))
    diagnostic_lines = []
    for notebook_file in notebook_files:
        try:
            notebook = json.loads(notebook_file.read_text(encoding="utf-8"))
            for cell in notebook.get("cells", []):
                for output in cell.get("outputs", []):
                    text_value = output.get("text", "")
                    if isinstance(text_value, list):
                        text_value = "".join(text_value)
                    data = output.get("data", {})
                    plain = data.get("text/plain", "") if isinstance(data, dict) else ""
                    if isinstance(plain, list):
                        plain = "".join(plain)
                    for value in (text_value, plain):
                        for line in str(value).splitlines():
                            if "WEBVM_PROXY_DIAGNOSTIC" in line:
                                diagnostic_lines.append(line[:1500])
        except (OSError, ValueError, TypeError) as exc:
            log.event("NOTEBOOK_DIAGNOSTIC_PARSE_ERROR",
                      file=str(notebook_file.relative_to(notebook_dir)),
                      error=type(exc).__name__ + ": " + str(exc))
    log.event("PROXY_DIAGNOSTIC_RESULTS", found=bool(diagnostic_lines),
              line_count=len(diagnostic_lines), lines=diagnostic_lines[-20:])
    log.event("LOOP_PROOF_PASS", request_id=message["request_id"], sequence=message["sequence"],
              rx_checksum=message["checksum"], tx_checksum=response["checksum"],
              round_trip_ms=round((time.monotonic() - round_trip_started) * 1000, 3),
              payload_bytes=message["payload_length"], output_bytes=(output_dir / "tx_response.json").stat().st_size,
              output_file=str(output_dir / "tx_response.json"))
    print("LOOP_PROOF_PASS output=" + str(output_dir / "tx_response.json") + " log=" + str(work / "run.jsonl"), flush=True)

if __name__ == "__main__":
    main()
