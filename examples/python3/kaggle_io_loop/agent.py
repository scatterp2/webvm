#!/usr/bin/env python3
"""Self-contained Kaggle notebook agent for the one-message RX/TX loop proof."""
import hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_VERSION = 1
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

def main():
    started = time.monotonic()
    rx_path = Path(os.environ.get("WEBVM_IO_LOOP_INPUT", str(DEFAULT_RX)))
    tx_path = Path(os.environ.get("WEBVM_IO_LOOP_OUTPUT", str(DEFAULT_TX)))
    log("AGENT_START", rx_path=str(rx_path), tx_path=str(tx_path))
    if not rx_path.is_file():
        raise FileNotFoundError("RX message not found at " + str(rx_path))
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
    log("RX_VALIDATED", sequence=message["sequence"], request_id=message["request_id"],
        payload_length=payload_length, checksum=actual_checksum)
    # One message per execution in stage 1. No durable exactly-once claim across restarts.
    processed_ids = set()
    delivery_key = (message["request_id"], message["sequence"])
    if delivery_key in processed_ids:
        delivery_state, response_payload = "duplicate", ""
    else:
        processed_ids.add(delivery_key)
        delivery_state, response_payload = "processed_once", "ACK:" + message["payload"]
    response_core = {
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
