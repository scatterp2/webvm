#!/usr/bin/env python3
"""Lightweight raw-TCP shell server for direct AI agent access.

Listens on 0.0.0.0:2323. Each connection gets a /bin/sh session.
Connect with: plink.exe -raw -P 2323 <tailscale-ip>

No telnet protocol negotiation — just raw bytes over TCP.
Designed for CheerpX/WebVM where subprocess works but pty.fork() may not.
"""
import os
import socket
import subprocess
import threading
import time

HOST = "0.0.0.0"
PORT = 2323


def handle_client(client_socket, address):
    """Bridge a TCP connection to a /bin/sh process."""
    print("TELNET_SHELL_CONNECTED client={}:{}".format(*address), flush=True)
    proc = None
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

        def relay_output():
            fd = proc.stdout.fileno()
            try:
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    client_socket.sendall(data)
            except OSError:
                pass

        def relay_input():
            try:
                while True:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass

        t_out = threading.Thread(target=relay_output, daemon=True)
        t_in = threading.Thread(target=relay_input, daemon=True)
        t_out.start()
        t_in.start()

        proc.wait()

    except Exception as exc:
        print("TELNET_SHELL_ERROR client={}:{} error={}: {}".format(
            address[0], address[1], type(exc).__name__, str(exc)[:200]),
            flush=True)
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            client_socket.close()
        except OSError:
            pass
        print("TELNET_SHELL_DISCONNECTED client={}:{}".format(*address), flush=True)


def start_telnet_shell(host=HOST, port=PORT):
    """Start the telnet shell server in a daemon thread. Returns the server socket."""
    for p in (port,):
        try:
            subprocess.run(
                ["fuser", "-k", "{}/tcp".format(p)],
                capture_output=True, timeout=5)
        except Exception:
            pass
    time.sleep(0.3)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print("TELNET_SHELL_LISTENING host={} port={}".format(host, port), flush=True)

    def accept_loop():
        while True:
            try:
                client, addr = server_socket.accept()
                threading.Thread(
                    target=handle_client,
                    args=(client, addr),
                    daemon=True,
                    name="telnet-client-{}".format(addr[0]),
                ).start()
            except Exception as exc:
                print("TELNET_SHELL_ACCEPT_ERROR error={}: {}".format(
                    type(exc).__name__, str(exc)[:200]), flush=True)

    thread = threading.Thread(target=accept_loop, name="telnet-shell-acceptor", daemon=True)
    thread.start()
    return server_socket


if __name__ == "__main__":
    sock = start_telnet_shell()
    print("Telnet shell server running on port {}".format(PORT), flush=True)
    print("Connect with: plink.exe -raw -P {} <tailscale-ip>".format(PORT), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
