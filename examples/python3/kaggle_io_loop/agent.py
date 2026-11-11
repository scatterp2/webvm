#!/usr/bin/env python3
"""Self-contained Kaggle notebook agent for the one-message RX/TX loop proof."""
import base64, hashlib, json, os, re, subprocess, sys, time
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
    import struct
    import subprocess
    import threading

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

    # Inspect the complete process listing, matching remote_probe.py.
    try:
        ps_result = subprocess.run(["ps", "-aux"], capture_output=True, text=True, timeout=10, check=True)
        process_lines = ps_result.stdout.splitlines()
        jupyter_lines = [line for line in process_lines if "jupyter" in line.lower() or "notebook" in line.lower()]
        route_match = None
        for candidate_lines in (jupyter_lines, process_lines):
            for line in candidate_lines:
                match = re.search(r"/k/([0-9]+)/([^/ ]+)/proxy", line)
                if match:
                    route_match = match
                    break
            if route_match:
                break
        report["process_list_method"] = "ps_aux"
        report["process_count"] = max(0, len(process_lines) - 1)
        report["jupyter_process_count"] = len(jupyter_lines)
        if route_match:
            report["jupyter_proxy_route_found"] = True
            report["jupyter_proxy_token_present"] = True
            report["jupyter_proxy_kernel_id"] = route_match.group(1)
    except (OSError, subprocess.SubprocessError) as exc:
        report["process_list_method"] = "ps_auxww"
        report["process_list_error"] = type(exc).__name__
    try:
        with socket.create_connection(("127.0.0.1", 8889), timeout=1):
            report["port_8889_listening"] = True
    except OSError:
        pass

    # DNS resolution can block indefinitely in the notebook process on some
    # restricted runtimes. Isolate each probe so the parent agent always reaches
    # TX_WRITTEN and the RX/TX controller can observe a response.
    probe_source = (
        "import json,socket,sys; "
        "host=sys.argv[1]; "
        "addresses=sorted({x[4][0] for x in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}); "
        "conn=socket.create_connection((host,443),timeout=3); conn.close(); "
        "print(json.dumps({'dns':True,'tcp':True,'address_count':len(addresses)}))"
    )
    # Capture packet headers while the existing DNS/TCP probes run.
    # This is an AF_PACKET/tcpdump-equivalent metadata capture; no payloads are stored.
    capture = {
        "available": False, "method": "AF_PACKET", "filter": "IPv4 TCP/443 and UDP/53",
        "metadata_only": True, "payload_capture": False,
    }
    capture_sock = None
    capture_thread = None
    capture_stop = threading.Event()
    capture_started = time.monotonic()
    capture_stats = {
        "tcp_443_packets": 0, "tcp_443_outbound": 0, "tcp_443_inbound": 0,
        "tcp_443_syn": 0, "tcp_443_syn_ack": 0, "tcp_443_rst": 0,
        "tcp_443_fin": 0, "tcp_443_syn_retransmit_hints": 0,
        "udp_53_packets": 0, "udp_53_outbound": 0, "udp_53_inbound": 0,
    }
    capture_samples = []
    syn_seen = set()
    try:
        interfaces = [name for name, _index in socket.if_nameindex() if name != "lo"]
        capture_interface = "eth0" if "eth0" in interfaces else (interfaces[0] if interfaces else None)
        if not capture_interface:
            raise OSError("no non-loopback interface found")
        capture_sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        capture_sock.bind((capture_interface, 0))
        capture_sock.settimeout(0.2)
        capture.update({"available": True, "interface": capture_interface})

        def _capture_packets():
            outgoing_type = getattr(socket, "PACKET_OUTGOING", 4)
            while not capture_stop.is_set():
                try:
                    frame, addr = capture_sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(frame) < 14:
                    continue
                ethertype = struct.unpack("!H", frame[12:14])[0]
                offset = 14
                if ethertype == 0x8100 and len(frame) >= 18:
                    ethertype = struct.unpack("!H", frame[16:18])[0]
                    offset = 18
                if ethertype != 0x0800 or len(frame) < offset + 20:
                    continue
                ip = frame[offset:]
                if ip[0] >> 4 != 4:
                    continue
                ihl = (ip[0] & 0x0f) * 4
                if len(ip) < ihl + 8:
                    continue
                total_len = struct.unpack("!H", ip[2:4])[0]
                proto = ip[9]
                src_ip = socket.inet_ntoa(ip[12:16])
                dst_ip = socket.inet_ntoa(ip[16:20])
                if proto == 6 and len(ip) >= ihl + 20:
                    tcp = ip[ihl:]
                    src_port, dst_port, seq, ack = struct.unpack("!HHII", tcp[:12])
                    if src_port != 443 and dst_port != 443:
                        continue
                    tcp_hlen = (tcp[12] >> 4) * 4
                    flags = tcp[13]
                    direction = "outbound" if len(addr) > 2 and addr[2] == outgoing_type else "inbound"
                    capture_stats["tcp_443_packets"] += 1
                    capture_stats["tcp_443_" + direction] += 1
                    if flags & 0x02:
                        capture_stats["tcp_443_syn"] += 1
                        if flags & 0x10:
                            capture_stats["tcp_443_syn_ack"] += 1
                        else:
                            key = (src_ip, dst_ip, src_port, dst_port, seq)
                            if key in syn_seen:
                                capture_stats["tcp_443_syn_retransmit_hints"] += 1
                            syn_seen.add(key)
                    if flags & 0x04:
                        capture_stats["tcp_443_rst"] += 1
                    if flags & 0x01:
                        capture_stats["tcp_443_fin"] += 1
                    if len(capture_samples) < 24:
                        capture_samples.append({
                            "protocol": "tcp", "direction": direction,
                            "src": "{}:{}".format(src_ip, src_port),
                            "dst": "{}:{}".format(dst_ip, dst_port),
                            "flags": "".join(letter for bit, letter in (
                                (0x02, "S"), (0x10, "A"), (0x08, "P"),
                                (0x01, "F"), (0x04, "R"), (0x20, "U")
                            ) if flags & bit),
                            "seq": seq, "ack": ack,
                            "payload_bytes": max(0, min(total_len, len(ip)) - ihl - tcp_hlen),
                        })
                elif proto == 17 and len(ip) >= ihl + 8:
                    udp = ip[ihl:]
                    src_port, dst_port = struct.unpack("!HH", udp[:4])
                    if src_port != 53 and dst_port != 53:
                        continue
                    direction = "outbound" if len(addr) > 2 and addr[2] == outgoing_type else "inbound"
                    capture_stats["udp_53_packets"] += 1
                    capture_stats["udp_53_" + direction] += 1
                    if len(capture_samples) < 24:
                        capture_samples.append({
                            "protocol": "udp", "direction": direction,
                            "src": "{}:{}".format(src_ip, src_port),
                            "dst": "{}:{}".format(dst_ip, dst_port),
                            "payload_bytes": max(0, min(total_len, len(ip)) - ihl - 8),
                        })

        capture_thread = threading.Thread(target=_capture_packets, name="kaggle-tcp-header-capture", daemon=True)
        capture_thread.start()
    except Exception as exc:
        capture["error_type"] = type(exc).__name__
        if capture_sock is not None:
            try:
                capture_sock.close()
            except OSError:
                pass
            capture_sock = None

    for host in ("kkb-production.jupyter-proxy.kaggle.net", "pste.us"):
        try:
            probe = subprocess.run(
                [sys.executable, "-I", "-c", probe_source, host],
                capture_output=True, text=True, timeout=6, check=False
            )
            if probe.returncode == 0:
                try:
                    report["outbound_tcp_443"][host] = json.loads(probe.stdout.strip())
                except (ValueError, TypeError):
                    report["outbound_tcp_443"][host] = {
                        "error": "InvalidProbeOutput", "dns_or_tcp": False
                    }
            else:
                report["outbound_tcp_443"][host] = {
                    "error": "ProbeFailed", "dns_or_tcp": False,
                    "detail": (probe.stderr or "")[-180:]
                }
        except subprocess.TimeoutExpired:
            report["outbound_tcp_443"][host] = {
                "error": "ProbeTimeout", "dns_or_tcp": False
            }
        except Exception as exc:
            report["outbound_tcp_443"][host] = {
                "error": type(exc).__name__, "dns_or_tcp": False
            }
    if capture_sock is not None:
        capture_stop.set()
        try:
            capture_sock.close()
        except OSError:
            pass
        if capture_thread is not None:
            capture_thread.join(timeout=1.0)
    capture["capture_window_seconds"] = round(time.monotonic() - capture_started, 3)
    capture.update(capture_stats)
    capture["samples"] = capture_samples
    report["packet_capture"] = capture
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
    # One message per execution; command payloads are base64 Python source from
    # the controlled repository. Keep the payload bounded and execution timed.
    processed_ids = set()
    delivery_key = (message["request_id"], message["sequence"])
    execution = None
    if delivery_key in processed_ids:
        delivery_state, response_payload = "duplicate", ""
    else:
        processed_ids.add(delivery_key)
        delivery_state = "processed_once"
        if message["type"] == "python_b64":
            encoded = message["payload"]
            if len(encoded) > 90000:
                raise ValueError("Python payload exceeds 64 KiB encoded safety limit")
            try:
                source = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
            except Exception as exc:
                raise ValueError("Invalid base64 UTF-8 Python payload: " + type(exc).__name__)
            if not source.strip():
                raise ValueError("Python payload is empty")
            started_exec = time.monotonic()
            route_result_path = Path("/kaggle/working/webvm_proxy_route.json")
            try:
                route_result_path.unlink()
            except FileNotFoundError:
                pass
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", source],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=45, check=False, env={"PATH": os.environ.get("PATH", "")}
                )
                execution = {
                    "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "exit_code": result.returncode,
                    "elapsed_ms": round((time.monotonic() - started_exec) * 1000, 2),
                    "output": (result.stdout or "")[-20000:],
                    "timed_out": False,
                }
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", "replace")
                execution = {
                    "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "exit_code": None,
                    "elapsed_ms": round((time.monotonic() - started_exec) * 1000, 2),
                    "output": str(output)[-20000:],
                    "timed_out": True,
                }
            response_payload = "EXEC:" + execution["sha256"] + ":" + str(execution["exit_code"])
        else:
            response_payload = "ACK:" + message["payload"]
    proxy_route = None
    route_result_path = Path("/kaggle/working/webvm_proxy_route.json")
    if route_result_path.is_file():
        try:
            candidate_route = json.loads(route_result_path.read_text(encoding="utf-8"))
            import re as _route_re
            route_host = candidate_route.get("host")
            route_path = candidate_route.get("path")
            route_kernel_id = str(candidate_route.get("kernel_id") or "")
            route_token = str(candidate_route.get("token") or "")
            route_match = _route_re.fullmatch(
                r"/k/([0-9]+)/([A-Za-z0-9._~-]+)/proxy/?", str(route_path or ""))
            if (route_host == "kkb-production.jupyter-proxy.kaggle.net"
                    and route_match
                    and route_match.group(1) == route_kernel_id
                    and route_match.group(2) == route_token):
                proxy_route = {
                    "host": route_host, "path": route_path,
                    "kernel_id": route_kernel_id, "token": route_token,
                }
                log("PROXY_ROUTE_TX_ATTACHED", host=route_host,
                    kernel_id=route_kernel_id, token_present=True)
            else:
                log("PROXY_ROUTE_TX_REJECTED", reason="route_validation_failed")
        except Exception as exc:
            log("PROXY_ROUTE_TX_REJECTED", reason=type(exc).__name__)
    network_diagnostics = collect_network_diagnostics()
    log("NETWORK_DIAGNOSTICS", report=network_diagnostics)
    response_core = {
        "network_diagnostics": network_diagnostics,
        "proxy_route": proxy_route,
        "execution": execution,
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
    # Do not print the token-bearing route into Kaggle execution logs.
    visible_response = dict(response)
    if visible_response.get("proxy_route"):
        route = visible_response["proxy_route"]
        visible_response["proxy_route"] = {
            "host": route.get("host"), "kernel_id": route.get("kernel_id"),
            "token_present": bool(route.get("token")),
            "path_shape": "/k/{}/[TOKEN]/proxy".format(route.get("kernel_id")),
        }
    print("IO_LOOP_RESULT " + json.dumps({"rx": message, "tx": visible_response}, sort_keys=True), flush=True)

if __name__ == "__main__":
    main()
