#!/usr/bin/env python3
"""Run a bounded raw-frame transmit/capture test through the WebVM RX/TX command channel.

This uses AF_PACKET directly because tcpdump is not installed in the Kaggle runtime.
It captures packet metadata for existing Jupyter :8888 flows without reading application
payloads, and sends one private experimental-EtherType marker frame to verify raw TX/RX.
It does not inject bytes into an established TCP session or change firewall/server state.
"""
import json, os, socket, struct, time, fcntl

INTERFACE = "eth0"
CAPTURE_SECONDS = 4.0
MARKER = ("WEBVM_RAW_FRAME_TEST_" + str(time.time_ns())).encode("ascii")
CUSTOM_ETHERTYPE = 0x88B5

def mac_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", ifname.encode()[:15])
        result = fcntl.ioctl(s.fileno(), 0x8927, request)
        return result[18:24]
    finally:
        s.close()

def tcp_metadata(frame):
    if len(frame) < 14:
        return None
    ethertype = struct.unpack("!H", frame[12:14])[0]
    offset = 14
    if ethertype == 0x8100 and len(frame) >= 18:
        ethertype = struct.unpack("!H", frame[16:18])[0]
        offset = 18
    if ethertype != 0x0800 or len(frame) < offset + 20:
        return None
    ip = frame[offset:]
    if ip[0] >> 4 != 4:
        return None
    ihl = (ip[0] & 0x0f) * 4
    if len(ip) < ihl + 20 or ip[9] != 6:
        return None
    total_len = struct.unpack("!H", ip[2:4])[0]
    src_ip = socket.inet_ntoa(ip[12:16])
    dst_ip = socket.inet_ntoa(ip[16:20])
    tcp = ip[ihl:]
    src_port, dst_port, seq, ack = struct.unpack("!HHII", tcp[:12])
    tcp_hlen = (tcp[12] >> 4) * 4
    flags = tcp[13]
    payload_len = max(0, min(total_len, len(ip)) - ihl - tcp_hlen)
    if src_port != 8888 and dst_port != 8888:
        return None
    return {
        "src": f"{src_ip}:{src_port}",
        "dst": f"{dst_ip}:{dst_port}",
        "flags": "".join(letter for bit, letter in (
            (0x02, "S"), (0x10, "A"), (0x08, "P"),
            (0x01, "F"), (0x04, "R"), (0x20, "U")
        ) if flags & bit),
        "seq": seq,
        "ack": ack,
        "payload_bytes": payload_len,
    }

report = {
    "probe": "webvm-raw-frame-tx-rx-v1",
    "time": time.time(),
    "interface": INTERFACE,
    "tcpdump_installed": False,
    "capture_method": "AF_PACKET raw socket",
    "firewall_changed": False,
    "jupyter_8888_touched": False,
}

capture = None
tx = None
try:
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    capture.bind((INTERFACE, 0))
    capture.settimeout(0.15)
    local_mac = mac_address(INTERFACE)
    report["raw_capture_socket"] = "PASS"
    report["interface_mac_available"] = True

    tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(CUSTOM_ETHERTYPE))
    tx.bind((INTERFACE, socket.htons(CUSTOM_ETHERTYPE)))
    # Address the frame to this interface's own MAC and use an experimental
    # EtherType, avoiding the live TCP/Jupyter protocol entirely.
    frame = local_mac + local_mac + struct.pack("!H", CUSTOM_ETHERTYPE) + MARKER
    if len(MARKER) < 46:
        frame += b"\x00" * (46 - len(MARKER))
    sent = tx.send(frame)
    report["raw_frame_tx"] = "PASS"
    report["raw_frame_bytes"] = sent
except Exception as exc:
    report["raw_capture_or_tx_error"] = type(exc).__name__ + ": " + str(exc)[:240]
finally:
    if tx is not None:
        tx.close()

start = time.monotonic()
seen_marker = 0
tcp_packets = 0
tcp_rows = []
other_packets = 0
if capture is not None:
    while time.monotonic() - start < CAPTURE_SECONDS:
        try:
            packet, addr = capture.recvfrom(65535)
        except socket.timeout:
            continue
        except Exception as exc:
            report["capture_loop_error"] = type(exc).__name__ + ": " + str(exc)[:180]
            break
        if MARKER in packet:
            seen_marker += 1
        row = tcp_metadata(packet)
        if row is not None:
            tcp_packets += 1
            if len(tcp_rows) < 30:
                tcp_rows.append(row)
        else:
            other_packets += 1
    capture.close()

report["capture_window_seconds"] = round(time.monotonic() - start, 3)
report["raw_marker_captured"] = seen_marker > 0
report["raw_marker_capture_count"] = seen_marker
report["tcp_8888_packet_count"] = tcp_packets
report["other_packet_count"] = other_packets
report["tcp_8888_samples"] = tcp_rows
report["result"] = (
    "PASS_RAW_TX_AND_CAPTURE" if report.get("raw_frame_tx") == "PASS" and seen_marker > 0
    else "PARTIAL_OR_FAILED_RAW_TX_CAPTURE"
)
print("REMOTE_PROBE_RESULT " + json.dumps(report, sort_keys=True), flush=True)
