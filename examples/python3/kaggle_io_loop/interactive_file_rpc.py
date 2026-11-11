#!/usr/bin/env python3
"""Interactive Kaggle file-response RPC.

Run this in the *interactive* Kaggle notebook runtime, not a submitted
"Save & Run All" worker. A GET/POST request stays open while its command runs;
the HTTP response body is the exact contents written to the corresponding
result file. Nothing returns ENOENT while the command is still running.

GET form:
  /<hex-encoded-base64-command>.txt

The path's hex decodes to an ASCII Base64 payload, which decodes to a UTF-8
shell command. For a stable 128-hex request ID, POST the Base64 command as the
request body to /<request-id>.txt.

The service binds to loopback and is intended to be reached through the
interactive notebook's authenticated proxy, not exposed as a public listener.
"""
import base64
import binascii
import hashlib
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("/kaggle/working/hex_rpc_test")
RESULTS = ROOT / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
HOST = "127.0.0.1"
PORT = 8765
MAX_REQUEST_BYTES = 32768
COMMAND_TIMEOUT_SECONDS = 60
REQUEST_ID_RE = re.compile(r"[0-9a-fA-F]{128}")

def decode_command(encoded):
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("empty or oversized command payload")
    try:
        command = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("command must be Base64-encoded UTF-8") from exc
    if not command.strip():
        raise ValueError("empty command")
    return command

class Handler(BaseHTTPRequestHandler):
    server_version = "KaggleFileRPC/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging command payloads or request paths.
        print("FILE_RPC " + (fmt % args).split(" ")[0], flush=True)

    def do_GET(self):
        parsed = urlsplit(self.path)
        name = parsed.path.rsplit("/", 1)[-1]
        if not name.endswith(".txt"):
            self.send_error(404, "request path must end in .txt")
            return

        # Preferred test form: a random 128-hex ID names the result file,
        # while cmd_b64 carries the Base64 command in the *same* HTTP request.
        request_id = name[:-4]
        if REQUEST_ID_RE.fullmatch(request_id):
            from urllib.parse import parse_qs
            encoded_values = parse_qs(parsed.query, keep_blank_values=True).get("cmd_b64", [])
            if len(encoded_values) != 1:
                self.send_error(400, "128-hex request ID requires exactly one cmd_b64 parameter")
                return
            try:
                command = decode_command(encoded_values[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            self._execute_and_return(name, command)
            return

        # Alternate form: filename itself is hex(Base64(command)).
        encoded_hex = name[:-4]
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,16384}", encoded_hex):
            self.send_error(400, "filename must be a 128-hex request ID or hex-encoded Base64 command")
            return
        try:
            encoded = bytes.fromhex(encoded_hex).decode("ascii")
            command = decode_command(encoded)
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        self._execute_and_return(name, command)

    def do_POST(self):
        path = urlsplit(self.path).path
        name = path.rsplit("/", 1)[-1]
        request_id = name[:-4] if name.endswith(".txt") else ""
        if not REQUEST_ID_RE.fullmatch(request_id):
            self.send_error(400, "POST filename must be 128 hexadecimal characters plus .txt")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length < 1 or length > MAX_REQUEST_BYTES:
            self.send_error(413, "command payload missing or oversized")
            return
        try:
            command = decode_command(self.rfile.read(length).decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_error(400, str(exc))
            return
        self._execute_and_return(name, command)

    def _execute_and_return(self, filename, command):
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
            output = result.stdout or b""
            status = 200
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            if isinstance(output, str):
                output = output.encode("utf-8", "replace")
            output += b"\nERROR: command timed out after 60 seconds\n"
            status = 504
            exit_code = None
        except Exception as exc:
            output = ("ERROR: {}: {}\n".format(type(exc).__name__, exc)).encode("utf-8", "replace")
            status = 500
            exit_code = None

        # The result file is the response body; write it atomically first.
        result_path = RESULTS / filename
        temp_path = RESULTS / ("." + filename + ".tmp")
        temp_path.write_bytes(output)
        os.replace(temp_path, result_path)

        digest = hashlib.sha256(output).hexdigest()
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        print(
            "FILE_RPC_RESULT filename={} status={} exit_code={} bytes={} sha256={} elapsed_ms={}".format(
                filename, status, exit_code, len(output), digest, elapsed_ms
            ),
            flush=True,
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(output)))
        self.send_header("X-File-RPC-Exit-Code", str(exit_code) if exit_code is not None else "unknown")
        self.send_header("X-File-RPC-SHA256", digest)
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(output)
        self.wfile.flush()
        self.close_connection = True

def start_file_rpc_server():
    if globals().get("_hex_file_rpc_server") is not None:
        print("FILE_RPC_ALREADY_RUNNING host={} port={}".format(HOST, PORT), flush=True)
        return _hex_file_rpc_server
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="kaggle-file-rpc", daemon=True)
    thread.start()
    globals()["_hex_file_rpc_server"] = server
    print("FILE_RPC_READY host={} port={} results={}".format(HOST, PORT, RESULTS), flush=True)
    return server

if __name__ == "__main__":
    start_file_rpc_server()
