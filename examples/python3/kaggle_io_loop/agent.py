#!/usr/bin/env python3
"""Self-contained Kaggle notebook agent for the one-message RX/TX loop proof."""
import hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_VERSION = 1
DATASET_ID = "simonday/webvm-io-loop-rx"
DEFAULT_RX = Path("/kaggle/input/webvm-io-loop-rx/rx_message.json")
DEFAULT_TX = Path("/kaggle/working/tx_response.json")

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def log(event, **fields):
    print("IO_LOOP " + json.dumps({"time": utc_now(), "event": event, **fields}, sort_keys=True), flush=True)

def resolve_rx_path(rx_path):
    if rx_path.is_file():
        log("RX_PATH_FOUND", source="configured_path", path=str(rx_path))
        return rx_path

    input_root = Path("/kaggle/input")
    try:
        entries = sorted(
            {"name": p.name, "is_dir": p.is_dir()}
            for p in input_root.iterdir()
        ) if input_root.is_dir() else []
    except Exception as exc:
        entries = [{"listing_error": type(exc).__name__, "message": str(exc)}]
    log("RX_INPUT_ROOT_INSPECTED", root=str(input_root), exists=input_root.is_dir(), entries=entries)

    candidates = sorted(input_root.glob("*/rx_message.json")) if input_root.is_dir() else []
    if len(candidates) == 1:
        log("RX_PATH_DISCOVERED", source="mounted_input_search", path=str(candidates[0]))
        return candidates[0]
    if len(candidates) > 1:
        log("RX_PATH_AMBIGUOUS", source="mounted_input_search", paths=[str(p) for p in candidates])
        raise RuntimeError("Multiple rx_message.json files found under /kaggle/input")

    # Fallback: download through kagglehub in this notebook's own runtime.
    # This avoids assuming that a newly published dataset version was mounted
    # into the kernel's /kaggle/input snapshot when the kernel was submitted.
    try:
        import kagglehub
        log("KAGGLEHUB_DOWNLOAD_START", dataset_id=DATASET_ID)
        downloaded_dir = Path(kagglehub.dataset_download(DATASET_ID))
        log("KAGGLEHUB_DOWNLOAD_COMPLETE", dataset_id=DATASET_ID, path=str(downloaded_dir))
        downloaded_candidates = sorted(downloaded_dir.rglob("rx_message.json")) if downloaded_dir.exists() else []
        if len(downloaded_candidates) == 1:
            log("RX_PATH_DISCOVERED", source="kagglehub_download", path=str(downloaded_candidates[0]))
            return downloaded_candidates[0]
        log("KAGGLEHUB_RX_SEARCH_RESULT", path=str(downloaded_dir), candidate_count=len(downloaded_candidates),
            candidates=[str(p) for p in downloaded_candidates[:10]])
    except Exception as exc:
        log("KAGGLEHUB_DOWNLOAD_FAILED", dataset_id=DATASET_ID,
            error_type=type(exc).__name__, error=str(exc)[:1000])

    raise FileNotFoundError(
        "RX message not found. Configured path={!s}; no unique /kaggle/input/*/rx_message.json "
        "and kagglehub fallback did not locate it. See RX_INPUT_ROOT_INSPECTED and KAGGLEHUB_* events."
        .format(rx_path)
    )


def collect_network_diagnostics():
    """Return non-secret capability facts needed to choose a real TCP/IP transport."""
    import glob
    import shutil
    import socket
    import subprocess

    report = {
        "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "dev_net_tun": os.path.exists("/dev/net/tun"),
        "iptables_available": bool(shutil.which("iptables")),
        "tailscale_available": bool(shutil.which("tailscale")),
        "tailscaled_available": bool(shutil.which("tailscaled")),
        "jupyter_proxy_route_found": False,
        "jupyter_proxy_token_present": False,
        "jupyter_proxy_kernel_id": None,
        "port_8889_listening": False,
        "outbound_tcp_443": {},
    }
    try:
        report["ipv4_forwarding"] = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
    except OSError:
        report["ipv4_forwarding"] = "unavailable"

    iptables = shutil.which("iptables")
    if iptables:
        try:
            probe = subprocess.run([iptables, "-t", "nat", "-S", "PREROUTING"],
                                   capture_output=True, text=True, timeout=5)
            report["iptables_nat_prerouting"] = {
                "returncode": probe.returncode,
                "permitted": probe.returncode == 0,
                "stderr": (probe.stderr or "")[-300:],
            }
        except Exception as exc:
            report["iptables_nat_prerouting"] = {"error": type(exc).__name__}

    for proc_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            args = [v.decode("utf-8", "replace") for v in Path(proc_path).read_bytes().split(b"\0") if v]
        except OSError:
            continue
        if not any("jupyter" in value.lower() or "notebook" in value.lower() for value in args):
            continue
        base_url = None
        for index, value in enumerate(args):
            if value.startswith(("--NotebookApp.base_url=", "--ServerApp.base_url=")):
                base_url = value.split("=", 1)[1]
                break
            if value in ("--NotebookApp.base_url", "--ServerApp.base_url") and index + 1 < len(args):
                base_url = args[index + 1]
                break
        if base_url:
            match = re.search(r"/k/(\d+)/([^/]+)/proxy/?$", base_url)
            if match:
                report["jupyter_proxy_route_found"] = True
                report["jupyter_proxy_token_present"] = bool(match.group(2))
                report["jupyter_proxy_kernel_id"] = match.group(1)
            else:
                # Report only the route shape; never return a URL that could contain a token.
                report["jupyter_base_url_shape"] = "present_unrecognized_format"
            break

    try:
        with socket.create_connection(("127.0.0.1", 8889), timeout=1):
            report["port_8889_listening"] = True
    except OSError:
        pass

    for host in ("kkb-production.jupyter-proxy.kaggle.net", "pste.us"):
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM
            )})
            with socket.create_connection((host, 443), timeout=3):
                report["outbound_tcp_443"][host] = {
                    "dns": True, "tcp": True, "address_count": len(addresses)
                }
        except Exception as exc:
            report["outbound_tcp_443"][host] = {
                "error": type(exc).__name__, "dns_or_tcp": False
            }
    return report

def main():
    started = time.monotonic()
    rx_path = Path(os.environ.get("WEBVM_IO_LOOP_INPUT", str(DEFAULT_RX)))
    tx_path = Path(os.environ.get("WEBVM_IO_LOOP_OUTPUT", str(DEFAULT_TX)))
    log("AGENT_START", rx_path=str(rx_path), tx_path=str(tx_path))
    if "WEBVM_IO_LOOP_INPUT" in os.environ:
        if not rx_path.is_file():
            raise FileNotFoundError("Explicit WEBVM_IO_LOOP_INPUT path does not exist: " + str(rx_path))
        log("RX_PATH_FOUND", source="explicit_environment", path=str(rx_path))
    else:
        rx_path = resolve_rx_path(rx_path)
    raw = rx_path.read_bytes()
    message = json.loads(raw.decode("utf-8"))
    required = {"version", "sequence", "request_id", "type", "payload_length", "payload", "checksum"}
    if not isinstance(message, dict) or not required.issubset(message):
        raise ValueError("RX schema invalid; required fields missing")
    if message["version"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported protocol version")
    if not isinstance(message["sequence"], int) or message["sequence"] < 1:
        raise ValueError("Invalid sequence number")
    if not isinstance(message["request_id"], str) or not message["request_id"]:
        raise ValueError("Invalid request_id")
    if not isinstance(message["payload"], str):
        raise ValueError("Payload must be a string")
    payload_length = len(message["payload"].encode("utf-8"))
    if message["payload_length"] != payload_length:
        raise ValueError("Payload length mismatch")
    signed = {k: message[k] for k in ("version", "sequence", "request_id", "type", "payload_length", "payload")}
    actual_checksum = sha256(signed)
    if message["checksum"] != actual_checksum:
        raise ValueError("RX checksum mismatch")
    log("RX_VALIDATED", source_path=str(rx_path), sequence=message["sequence"], request_id=message["request_id"],
        payload_length=payload_length, checksum=actual_checksum)
    # One message per execution in stage 1. No durable exactly-once claim across restarts.
    processed_ids = set()
    delivery_key = (message["request_id"], message["sequence"])
    if delivery_key in processed_ids:
        delivery_state, response_payload = "duplicate", ""
    else:
        processed_ids.add(delivery_key)
        delivery_state, response_payload = "processed_once", "ACK:" + message["payload"]
    network_diagnostics = collect_network_diagnostics()
    log("NETWORK_DIAGNOSTICS", report=network_diagnostics)
    response_core = {
        "network_diagnostics": network_diagnostics,
        "version": PROTOCOL_VERSION, "sequence": message["sequence"],
        "request_id": message["request_id"], "type": "ack",
        "payload_length": len(response_payload.encode("utf-8")), "payload": response_payload,
        "ack_state": delivery_state, "received_checksum": actual_checksum, "received_at": utc_now(),
    }
    response = dict(response_core)
    response["agent_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    response["checksum"] = sha256(response)
    tx_path.parent.mkdir(parents=True, exist_ok=True)
    tx_path.write_text(json.dumps(response, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log("TX_WRITTEN", path=str(tx_path), sequence=response["sequence"], request_id=response["request_id"],
        ack_state=delivery_state, bytes=tx_path.stat().st_size, checksum=response["checksum"],
        elapsed_ms=response["agent_elapsed_ms"])
    print("IO_LOOP_RESULT " + json.dumps({"rx": message, "tx": response}, sort_keys=True), flush=True)

if __name__ == "__main__":
    main()
