#!/usr/bin/env python3
"""Run shell commands inside the WebVM over the telnet shell (port 2323).

Each command is followed by a unique echo marker so output can be delimited
reliably even when the shell prompt is delayed.

Usage: python vmshell.py <host> [-p port] "command one" "command two" ...
"""
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "100.122.41.24"
PORT = 2323

args = sys.argv[2:]
TIMEOUT = 45.0
if args and args[0] == "-p":
    PORT = int(args[1])
    args = args[2:]
if args and args[0] == "-t":
    TIMEOUT = float(args[1])
    args = args[2:]
if args and args[0] == "--file":
    # Read one command per line from a local file.  Avoids all shell
    # quoting problems for commands that contain quotes or semicolons.
    with open(args[1], "r", encoding="utf-8") as handle:
        COMMANDS = [line.rstrip("\n") for line in handle if line.strip()]
else:
    COMMANDS = args or ["id"]


def read_until(sock, marker, hard_timeout=45.0):
    buf = b""
    marker_b = marker.encode()
    start = time.time()
    while time.time() - start < hard_timeout:
        sock.settimeout(0.5)
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if marker_b in buf:
            break
    return buf.decode("utf-8", "replace")


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect((HOST, PORT))
    time.sleep(1.0)
    try:
        s.settimeout(2.0)
        s.recv(8192)
    except (socket.timeout, OSError):
        pass

    for i, cmd in enumerate(COMMANDS):
        marker = "__VM_DONE_{}__".format(i)
        s.sendall(("{}; echo {}\n".format(cmd, marker)).encode())
        out = read_until(s, marker, hard_timeout=TIMEOUT)
        # Drop everything up to the echoed command, then cut at the marker.
        if "\n" in out:
            out = out.split("\n", 1)[1]
        out = out.split(marker)[0]
        lines = [ln.rstrip() for ln in out.replace("\r", "").splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        print("### {}".format(cmd))
        print("\n".join(lines) if lines else "(no output)")
        print()

    s.close()


if __name__ == "__main__":
    main()
