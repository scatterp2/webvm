"""Interactive PTY shell endpoint for the private Kaggle Tailscale link."""
import os
import pty
import select
import signal
import subprocess
import socketserver
import threading
from pathlib import Path

SHELL_LOCAL_HOST = "127.0.0.1"
SHELL_LOCAL_PORT = 23232
SHELL_TAILNET_PORT = 18089


class _RemoteShellHandler(socketserver.BaseRequestHandler):
    def handle(self):
        master, slave = pty.openpty()
        process = None
        try:
            env = os.environ.copy()
            env.setdefault("TERM", "xterm")
            cwd = "/kaggle/working" if Path("/kaggle/working").is_dir() else "/tmp"
            process = subprocess.Popen(
                ["/bin/bash", "--login", "-i"],
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                close_fds=True, start_new_session=True,
            )
            os.close(slave)
            slave = -1
            os.set_blocking(master, False)
            while process.poll() is None:
                readable, _, _ = select.select([self.request, master], [], [], 0.5)
                for source in readable:
                    if source is self.request:
                        data = self.request.recv(65536)
                        if not data:
                            return
                        os.write(master, data)
                    else:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            data = b""
                        if not data:
                            return
                        self.request.sendall(data)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            if slave >= 0:
                try:
                    os.close(slave)
                except OSError:
                    pass
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            try:
                os.close(master)
            except OSError:
                pass


def start_remote_shell(cli, emit, server_class):
    server = server_class((SHELL_LOCAL_HOST, SHELL_LOCAL_PORT), _RemoteShellHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    emit("SHELL_LISTENER_READY", local_host=SHELL_LOCAL_HOST, local_port=SHELL_LOCAL_PORT)
    result = cli(
        "serve", "--bg", "--yes", "--tcp=" + str(SHELL_TAILNET_PORT),
        "tcp://" + SHELL_LOCAL_HOST + ":" + str(SHELL_LOCAL_PORT),
    )
    if result.returncode != 0:
        server.shutdown()
        server.server_close()
        raise RuntimeError("tailscale serve shell forwarder failed: " + (result.stdout or "")[-1500:])
    emit("SHELL_TAILNET_FORWARD_READY", tailscale_port=SHELL_TAILNET_PORT,
         local_port=SHELL_LOCAL_PORT)
    return server
