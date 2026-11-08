"""Loopback TCP forwarder from WebVM to the private Kaggle PTY shell."""
import select
import socket
import socketserver
import threading
from pathlib import Path

ENV_FILE = Path("/data/kaggle-tcp-link.env")
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 2324
REMOTE_PORT = 18089


def _peer_ip():
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            prefix = 'export WEBVM_KAGGLE_TCP_LINK_IP="'
            if line.startswith(prefix) and line.endswith('"'):
                candidate = line[len(prefix):-1].strip()
                if candidate and all(ch.isdigit() or ch == "." for ch in candidate):
                    return candidate
    except OSError:
        pass
    return None


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        peer_ip = _peer_ip()
        if not peer_ip:
            try:
                self.request.sendall(
                    b"WebVM shell forwarder: Kaggle Tailscale peer is not online yet.\r\n"
                )
            except OSError:
                pass
            return
        try:
            upstream = socket.create_connection((peer_ip, REMOTE_PORT), timeout=8)
        except OSError as exc:
            try:
                self.request.sendall(
                    ("WebVM shell forwarder: connect to Kaggle failed: {}\r\n".format(exc)).encode()
                )
            except OSError:
                pass
            return
        self.request.setblocking(False)
        upstream.setblocking(False)
        try:
            while True:
                readable, _, exceptional = select.select(
                    [self.request, upstream], [], [self.request, upstream], 60
                )
                if exceptional:
                    return
                if not readable:
                    continue
                for source in readable:
                    try:
                        data = source.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(host=LOCAL_HOST, port=LOCAL_PORT):
    server = _Server((host, port), _ForwardHandler)
    threading.Thread(target=server.serve_forever,
                     name="webvm-kaggle-shell-forwarder", daemon=True).start()
    print("WEBVM_SHELL_FORWARDER_LISTENING host={} port={} remote_port={} env_file={}".format(
        host, port, REMOTE_PORT, ENV_FILE), flush=True)
    return server


if __name__ == "__main__":
    raise SystemExit("Import start_server() from pi_impl.py.")
