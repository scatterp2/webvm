#!/usr/bin/env python3
"""Long-lived SOCKS5-over-Tailscale TCP bridge for the Kaggle notebook runtime.

The auth key is read only from Kaggle Secrets (WEBVM_TAILSCALE_AUTHKEY). It is
never included in notebook source, RX/TX files, logs, or telemetry.
"""
import io
import ipaddress
import json
import os
import shutil
import socket
import socketserver
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

TAG = "WVMQWENWS_6D91C42F"
TAILSCALE_HOSTNAME = "webvm-kaggle-tcp-link"
LOCAL_SOCKS_HOST = "127.0.0.1"
LOCAL_SOCKS_PORT = 8889
TAILNET_PORT = 8888
SOCKET_PATH = "/tmp/webvm-tailscaled.sock"
WORK = Path("/kaggle/working/webvm-tcp-link")
WORK.mkdir(parents=True, exist_ok=True)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(stage, **fields):
    record = {"time": _now(), "stage": stage, **fields}
    line = "WEBVM_TCP_LINK " + json.dumps(record, sort_keys=True)
    print(line, flush=True)
    # The existing runtime watcher polls this telemetry endpoint. Never send
    # the auth key or target connection details through it.
    content = (
        TAG + "\n"
        "source=WEBVM\n"
        "stage=PI_TCP_LINK_" + stage + "\n"
        "time=" + _now() + "\n"
        + json.dumps(fields, sort_keys=True) + "\n"
    )
    payload = json.dumps({
        "content": content[:8000],
        "language": "text",
        "expires_in": "1h",
    }).encode("utf-8")
    try:
        req = Request(
            "https://pste.us/api/v1/pastes",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "webvm-kaggle-tcp-link/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as response:
            response.read(1024)
    except Exception:
        # Telemetry is optional; never let it prevent the data path from starting.
        pass


def _read_secret():
    try:
        from kaggle_secrets import UserSecretsClient
        value = UserSecretsClient().get_secret("WEBVM_TAILSCALE_AUTHKEY")
        if isinstance(value, str) and value.strip().startswith("tskey-auth-"):
            return value.strip()
    except Exception:
        pass
    return None


def _install_tailscale():
    existing = shutil.which("tailscale")
    daemon = shutil.which("tailscaled")
    if existing and daemon:
        return existing, daemon

    _emit("INSTALL_START", source="official_tailscale_stable_amd64_archive")
    request = Request(
        "https://pkgs.tailscale.com/stable/tailscale_latest_amd64.tgz",
        headers={"User-Agent": "webvm-kaggle-tcp-link/1.0"},
    )
    with urlopen(request, timeout=90) as response:
        archive_bytes = response.read()
    target = WORK / "bin"
    target.mkdir(parents=True, exist_ok=True)
    found = set()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            basename = Path(member.name).name
            if basename not in ("tailscale", "tailscaled") or not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            destination = target / basename
            destination.write_bytes(stream.read())
            destination.chmod(0o700)
            found.add(basename)
    if found != {"tailscale", "tailscaled"}:
        raise RuntimeError("official Tailscale archive did not contain both required binaries")
    _emit("INSTALL_COMPLETE", binaries=sorted(found))
    return str(target / "tailscale"), str(target / "tailscaled")


def _recv_exact(sock, count):
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed the SOCKS5 handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _public_destination(host, port):
    if not 1 <= port <= 65535:
        raise ValueError("invalid destination port")
    addresses = sorted({
        item[4][0] for item in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    })
    public = []
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.is_global and not (
            address.is_multicast or address.is_unspecified or address.is_loopback
        ):
            public.append(value)
    if not public:
        raise PermissionError("destination did not resolve to a public IP address")
    return public[0]


def _read_line(sock, limit=8192):
    data = bytearray()
    while len(data) < limit:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("peer closed while reading a proxy request")
        data += byte
        if data.endswith(b"\r\n"):
            return bytes(data[:-2]).decode("iso-8859-1")
    raise ValueError("proxy request line/header exceeded the limit")


def _parse_host_port(value, default_port):
    value = value.strip()
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise ValueError("invalid bracketed IPv6 destination")
        host = value[1:close]
        tail = value[close + 1:]
        port = int(tail[1:]) if tail.startswith(":") else default_port
    elif value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text) if port_text else default_port
    else:
        host, port = value, default_port
    if not host or not 1 <= int(port) <= 65535:
        raise ValueError("invalid destination host or port")
    return host, int(port)


class _TcpProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            first = self.request.recv(1, socket.MSG_PEEK)
            if first == b"\x05":
                self._handle_socks5()
            else:
                self._handle_http()
        except Exception:
            # Do not log destination URLs or request headers.
            try:
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            except OSError:
                pass

    def _handle_socks5(self):
        client = self.request
        remote = None
        try:
            version, nmethods = _recv_exact(client, 2)
            methods = _recv_exact(client, nmethods)
            if version != 5 or 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")

            version, command, reserved, atyp = _recv_exact(client, 4)
            if version != 5 or command != 1 or reserved != 0:
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            if atyp == 1:
                host = socket.inet_ntop(socket.AF_INET, _recv_exact(client, 4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
            elif atyp == 3:
                length = _recv_exact(client, 1)[0]
                host = _recv_exact(client, length).decode("idna")
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = int.from_bytes(_recv_exact(client, 2), "big")
            target_ip = _public_destination(host, port)
            remote = socket.create_connection((target_ip, port), timeout=12)
            remote.settimeout(None)
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            _bridge(client, remote)
        except PermissionError:
            try:
                client.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        except Exception:
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        finally:
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass

    def _handle_http(self):
        client = self.request
        remote = None
        try:
            request_line = _read_line(client)
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            method, target, version = parts
            headers = []
            total_header_bytes = 0
            while True:
                line = _read_line(client)
                total_header_bytes += len(line) + 2
                if total_header_bytes > 65536:
                    raise ValueError("proxy headers exceeded the limit")
                if not line:
                    break
                if ":" not in line:
                    raise ValueError("malformed HTTP proxy header")
                name, value = line.split(":", 1)
                if name.lower() not in ("proxy-connection", "proxy-authorization", "proxy-authenticate"):
                    headers.append((name, value.strip()))

            if method.upper() == "CONNECT":
                host, port = _parse_host_port(target, 443)
                target_ip = _public_destination(host, port)
                remote = socket.create_connection((target_ip, port), timeout=12)
                remote.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: WebVM-TCP-Link\r\n\r\n")
                _bridge(client, remote)
                return

            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                client.sendall(b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n")
                return
            host = parsed.hostname
            port = parsed.port or 80
            target_ip = _public_destination(host, port)
            remote = socket.create_connection((target_ip, port), timeout=12)
            remote.settimeout(None)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            if not any(name.lower() == "host" for name, _ in headers):
                headers.append(("Host", host if port == 80 else "{}:{}".format(host, port)))
            headers = [(name, value) for name, value in headers if name.lower() != "connection"]
            headers.append(("Connection", "close"))
            outbound = [method + " " + path + " " + version]
            outbound.extend(name + ": " + value for name, value in headers)
            outbound.extend(["", ""])
            remote.sendall("\r\n".join(outbound).encode("iso-8859-1"))
            _bridge(client, remote)
        except PermissionError:
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _socks_self_test():
    with socket.create_connection((LOCAL_SOCKS_HOST, LOCAL_SOCKS_PORT), timeout=4) as sock:
        sock.settimeout(8)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 method negotiation failed")
        host = b"example.com"
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        reply = _recv_exact(sock, 4)
        if reply[0] != 5 or reply[1] != 0:
            raise RuntimeError("SOCKS5 TCP CONNECT self-test failed with reply code " + str(reply[1]))
        # Consume the bind address in the successful SOCKS5 reply.
        if reply[3] == 1:
            _recv_exact(sock, 4)
        elif reply[3] == 4:
            _recv_exact(sock, 16)
        elif reply[3] == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        _recv_exact(sock, 2)
    return True


def _http_connect_self_test():
    with socket.create_connection((LOCAL_SOCKS_HOST, LOCAL_SOCKS_PORT), timeout=4) as sock:
        sock.settimeout(8)
        sock.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\n"
            b"Host: example.com:443\r\n"
            b"Proxy-Connection: keep-alive\r\n\r\n"
        )
        response_line = _read_line(sock)
        if not response_line.startswith("HTTP/1.1 200 "):
            raise RuntimeError("HTTP CONNECT self-test failed: " + response_line[:100])
        while _read_line(sock):
            pass
    return True


def main():
    auth_key = _read_secret()
    if not auth_key:
        _emit(
            "AUTH_KEY_MISSING",
            required_kaggle_secret="WEBVM_TAILSCALE_AUTHKEY",
            note="Create an ephemeral Tailscale auth key and add it as a Kaggle Notebook Secret.",
        )
        return

    tailscale, tailscaled = _install_tailscale()
    state_path = WORK / "tailscale.state"
    daemon_log = (WORK / "tailscaled.log").open("ab")
    daemon = subprocess.Popen(
        [
            tailscaled,
            "--tun=userspace-networking",
            "--state=" + str(state_path),
            "--socket=" + SOCKET_PATH,
            "--port=0",
        ],
        stdin=subprocess.DEVNULL,
        stdout=daemon_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not Path(SOCKET_PATH).exists():
            if daemon.poll() is not None:
                raise RuntimeError("tailscaled exited before creating its control socket")
            time.sleep(0.25)
        if not Path(SOCKET_PATH).exists():
            raise TimeoutError("tailscaled control socket did not appear")

        def cli(*args, timeout=90):
            return subprocess.run(
                [tailscale, "--socket=" + SOCKET_PATH, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )

        up = cli(
            "up",
            "--auth-key=" + auth_key,
            "--hostname=" + TAILSCALE_HOSTNAME,
            "--accept-dns=false",
            "--accept-routes=false",
        )
        if up.returncode != 0:
            safe_output = (up.stdout or "").replace(auth_key, "[REDACTED]")[-1500:]
            raise RuntimeError("tailscale up failed: " + safe_output)

        server = _ThreadedServer((LOCAL_SOCKS_HOST, LOCAL_SOCKS_PORT), _TcpProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _socks_self_test()
        _emit("SOCKS5_SELF_TEST_PASS", local_port=LOCAL_SOCKS_PORT, target="public TCP 443")
        _http_connect_self_test()
        _emit("HTTP_CONNECT_SELF_TEST_PASS", local_port=LOCAL_SOCKS_PORT, target="public TCP 443")

        serve = cli(
            "serve",
            "--bg",
            "--tcp=" + str(TAILNET_PORT),
            "tcp://" + LOCAL_SOCKS_HOST + ":" + str(LOCAL_SOCKS_PORT),
        )
        if serve.returncode != 0:
            raise RuntimeError("tailscale serve TCP forwarder failed: " + (serve.stdout or "")[-1500:])

        ip_result = cli("ip", "-4", timeout=15)
        addresses = [line.strip() for line in (ip_result.stdout or "").splitlines()
                     if line.strip().startswith("100.")]
        if ip_result.returncode != 0 or not addresses:
            raise RuntimeError("Tailscale did not report a usable IPv4 address")
        _emit(
            "READY",
            hostname=TAILSCALE_HOSTNAME,
            tailscale_ip=addresses[0],
            tcp_port=TAILNET_PORT,
            local_socks5_port=LOCAL_SOCKS_PORT,
            transport="TCP over Tailscale; SOCKS5 CONNECT",
        )

        while daemon.poll() is None:
            time.sleep(20)
        raise RuntimeError("tailscaled exited after the TCP link became ready")
    except KeyboardInterrupt:
        _emit("STOPPING", reason="notebook interrupted")
    except Exception as exc:
        _emit("FAILED", error_type=type(exc).__name__, message=str(exc)[:1200])
    finally:
        try:
            if "server" in locals():
                server.shutdown()
                server.server_close()
        except Exception:
            pass
        try:
            if daemon.poll() is None:
                daemon.terminate()
        except Exception:
            pass
        try:
            daemon_log.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
