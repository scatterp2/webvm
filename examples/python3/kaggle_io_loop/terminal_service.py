#!/usr/bin/env python3
"""Local telnet-style command endpoint backed by the Kaggle RX/TX protocol.

Bind to loopback only. Each command is encoded as a Python RX payload, submitted
to the Kaggle proof kernel, and returned from the downloaded TX artifact.
This is a command queue, not a low-latency persistent shell: Kaggle scheduling
and dataset publication can make each command take minutes.
"""
import json
import socket
import socketserver
import threading
import time
import uuid


HOST = "127.0.0.1"
PORT = 2323
MAX_COMMAND_BYTES = 2048


def _send(sock, text):
    sock.sendall(text.encode("utf-8", "replace"))


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _send(self.request,
              "WebVM Kaggle RX/TX terminal\r\n"
              "Commands execute on Kaggle; each command submits RX and waits for TX.\r\n"
              "This is asynchronous/slow, not a persistent shell. Type exit to close.\r\n"
              "kaggle$ ")
        while True:
            raw = self.rfile.readline(MAX_COMMAND_BYTES + 2)
            if not raw:
                return
            command = raw.decode("utf-8", "replace").strip()
            if not command:
                _send(self.request, "kaggle$ ")
                continue
            if command.lower() in ("exit", "quit", "logout"):
                _send(self.request, "Disconnected.\r\n")
                return
            if len(raw) > MAX_COMMAND_BYTES:
                _send(self.request, "ERROR: command exceeds 2048 bytes\r\nkaggle$ ")
                continue
            _send(self.request, "\r\n[RX/TX] submitting command to Kaggle; waiting for TX response...\r\n")
            started = time.monotonic()
            try:
                result = self.server.execute_command(command, self.request)
                elapsed = time.monotonic() - started
                _send(self.request, result.rstrip() + "\r\n")
                _send(self.request, "[RX/TX] round trip finished in {:.1f}s\r\n".format(elapsed))
            except Exception as exc:
                _send(self.request, "[RX/TX ERROR] {}: {}\r\n".format(
                    type(exc).__name__, str(exc)[:400]))
            _send(self.request, "kaggle$ ")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _self_test(host, port):
    """Prove a real TCP client command returns through RX -> Kaggle -> TX."""
    token = "WEBVM_TERMINAL_SELFTEST_OK"
    command = "printf '" + token + "\\n'"
    started = time.monotonic()
    received = bytearray()
    try:
        with socket.create_connection((host, port), timeout=15) as client:
            client.settimeout(30)
            # Consume the banner/prompt first, then issue one harmless command.
            banner = bytearray()
            while b"kaggle$ " not in banner:
                chunk = client.recv(4096)
                if not chunk:
                    raise RuntimeError("connection closed before initial prompt")
                banner.extend(chunk)
                if len(banner) > 16384:
                    raise RuntimeError("terminal banner exceeded expected size")
            client.sendall((command + "\n").encode("utf-8"))
            deadline = started + 1200
            while time.monotonic() < deadline:
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise RuntimeError("connection closed before TX result")
                received.extend(chunk)
                response = received.decode("utf-8", "replace")
                if token in response and "[exit code 0]" in response:
                    print("WEBVM_TERMINAL_RESULT_SELFTEST_PASS transport=tcp-loopback-rx-kaggle-tx "
                          "elapsed_seconds={:.1f} output_marker={} exit_code=0".format(
                              time.monotonic() - started, token), flush=True)
                    return
                if "[RX/TX ERROR]" in response or "[exit code " in response:
                    raise RuntimeError("terminal returned error/nonzero result: " + response[-1200:])
            raise TimeoutError("no successful terminal TX result within 1200 seconds; received=" +
                               received.decode("utf-8", "replace")[-1200:])
    except Exception as exc:
        print("WEBVM_TERMINAL_RESULT_SELFTEST_FAIL error={}: {} elapsed_seconds={:.1f}".format(
            type(exc).__name__, str(exc)[:1200], time.monotonic() - started), flush=True)


def start_server(execute_command, host=HOST, port=PORT, self_test=True):
    server = _Server((host, port), _Handler)
    server.execute_command = execute_command
    thread = threading.Thread(target=server.serve_forever,
                              name="webvm-rx-tx-terminal", daemon=True)
    thread.start()
    print("RX_TX_TERMINAL_LISTENING host={} port={} transport=Kaggle-RX-TX".format(
        host, port), flush=True)
    if self_test:
        threading.Thread(target=_self_test, args=(host, port),
                         name="webvm-rx-tx-terminal-selftest", daemon=True).start()
        print("WEBVM_TERMINAL_SELFTEST_STARTED command=printf-marker transport=loopback-tcp", flush=True)
    return server


def make_payload(command):
    # JSON string syntax is also valid as a Python string literal.
    encoded_command = json.dumps(command, ensure_ascii=True)
    return (
        "import json, subprocess\n"
        "command = " + encoded_command + "\n"
        "try:\n"
        "    result = subprocess.run(['/bin/bash', '-lc', command], "
        "capture_output=True, text=True, timeout=35, cwd='/kaggle/working')\n"
        "    response = {'exit_code': result.returncode, "
        "'stdout': (result.stdout or '')[-3500:], "
        "'stderr': (result.stderr or '')[-1500:], 'timed_out': False}\n"
        "except subprocess.TimeoutExpired as exc:\n"
        "    response = {'exit_code': 124, "
        "'stdout': str(exc.stdout or '')[-3500:], "
        "'stderr': str(exc.stderr or '')[-1500:], 'timed_out': True}\n"
        "print('WEBVM_TERMINAL_RESULT=' + json.dumps(response, ensure_ascii=True), flush=True)\n"
    )


if __name__ == "__main__":
    raise SystemExit("Import start_server() from pi_impl.py so the Kaggle API environment is reused.")
