"""
GPU Coordinator
===============
Monitors Colab RPC over Tailscale.
When it dies: restarts via direct Colab API — no browser, no reCAPTCHA.

Chain on restart:
  1. Pull Google session cookies from Chrome (already logged in)
  2. Compute nbh  (reverse-engineered + verified against live HAR)
  3. GET  /tun/m/assign            → GPU runtime URL + JWT
  4. POST /api/sessions            → session_id + kernel_id
  5. WSS  /api/kernels/.../channels → execute cells via Jupyter protocol
  6. Poll TCP until rpc-server is back on Tailscale
  7. Start llama-server locally

Install:
  pip install browser-cookie3 requests websocket-client

Run:
  python coordinator.py
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import requests
import websocket
import browser_cookie3

# ═══════════════════════════════════════════════════════════════
#  CONFIG  ← only section you need to touch
# ═══════════════════════════════════════════════════════════════

BACKENDS = [
    {
        "name":         "colab_a",
        "rpc_host":     "colab-gpu",        # matches --hostname= in your notebook
        "rpc_port":     50052,
        "notebook_url": "https://colab.research.google.com/drive/1HmpP6bd88jOLF0djxRDikMORUW3e7pw_",
        "priority":     1,
    },
    # Add more accounts below when you expand:
    # {
    #     "name":         "colab_b",
    #     "rpc_host":     "colab-gpu-b",
    #     "rpc_port":     50052,
    #     "notebook_url": "https://colab.research.google.com/drive/OTHER_FILE_ID",
    #     "priority":     2,
    # },
]

TAILSCALE_KEY = "tskey-auth-k6GrXeZKyC11CNTRL-NGMc2XNo77GQaQJXRiyd7GiTYD8L9Rc2"

LLAMA = {
    "binary":       "/mnt/nvme/opt/llama-cpp-turboquant/build/bin/llama-server",
    "model":        "/mnt/nvme/opt/llama-cpp-turboquant/models/Qwen3-Coder-Next-UD-IQ3_XXS.gguf",

    # Layer split local:remote — 3060 12GB : T4 15GB
    "tensor_split": "4,5",

    "n_gpu_layers": 999,
    "ctx_size":     4096,
    "server_port":  8080,
    "extra_args":   [
        "--flash-attn",
    ],
}

POLL_INTERVAL = 30    # seconds between RPC health checks
TCP_TIMEOUT   = 5     # seconds per TCP probe
STATUS_PORT   = 7860  # GET http://127.0.0.1:7860/status

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("coord")

# ═══════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════

state = {
    "active_backend": None,
    "llama_proc":     None,
    "last_check":     None,
    "backends":       {b["name"]: "unknown" for b in BACKENDS},
    "events":         [],
    "restarting":     False,
}

# ═══════════════════════════════════════════════════════════════
#  NBH
#  Reverse-engineered from Colab's external_binary.js:
#    PGc(id)  = Rb( SHA256( JSON.stringify( QGc(id) ) ), 3 )
#    QGc(id)  = [["fileId"], [id]]
#    Rb(d, 3) = base64url(d).replace('=', '.')
#
#  Verified against live HAR:
#    file_id "1HmpP6bd88jOLF0djxRDikMORUW3e7pw_"
#    nbh     "_D1wwfQykAxbjS6b-4pw7_5TfzXIXjT7uDsi42NrqXA."  ✓
# ═══════════════════════════════════════════════════════════════

def compute_nbh(file_id: str) -> str:
    payload = json.dumps([["fileId"], [file_id]], separators=(",", ":"))
    digest  = hashlib.sha256(payload.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().replace("=", ".")


def file_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/drive/")[-1].split("?")[0]

# ═══════════════════════════════════════════════════════════════
#  GOOGLE COOKIES
#  Read directly from Chrome — no login, no browser launch.
# ═══════════════════════════════════════════════════════════════

def get_google_cookies() -> dict:
    jar     = browser_cookie3.brave(domain_name=".google.com")
    cookies = {c.name: c.value for c in jar}
    required = {"__Secure-3PSID", "SID", "HSID", "SSID"}
    found    = required & set(cookies)
    if not found:
        raise RuntimeError(
            "No Google auth cookies in Brave. "
            "Make sure Brave is open and you're logged into Google."
        )
    log.info(f"[cookies] {len(cookies)} loaded ({len(found)} auth cookies present)")
    return cookies

# ═══════════════════════════════════════════════════════════════
#  COLAB API
# ═══════════════════════════════════════════════════════════════

_HEADERS = {
    "accept":         "*/*",
    "accept-language":"en-US,en;q=0.9",
    "referer":        "https://colab.research.google.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def assign_runtime(file_id: str, cookies: dict) -> dict:
    """
    GET /tun/m/assign
    Returns runtime_url and jwt.

    reCAPTCHA is a UI gate only — confirmed from HAR:
    the assign request carries zero reCAPTCHA tokens.
    The endpoint only validates session cookies.
    """
    nbh = compute_nbh(file_id)
    url = (
        "https://colab.research.google.com/tun/m/assign"
        "?authuser=0&variant=GPU&accelerator=T4&match_any=1"
        f"&nbh={nbh}"
    )
    log.info(f"[api] assign  nbh={nbh[:24]}...")
    resp = requests.get(url, headers=_HEADERS, cookies=cookies, timeout=30)
    resp.raise_for_status()

    # Colab prefixes with )]}'\n to block JSON hijacking — strip it
    body = json.loads(resp.text.lstrip(")]}'\n").strip())
    log.info(f"[api] assign response: {json.dumps(body)}")

    # Warm runtime — got it immediately
    if "runtimeProxyInfo" in body:
        info = body["runtimeProxyInfo"]
        log.info(f"[api] runtime assigned (warm): {info['url']}")
        return {"runtime_url": info["url"], "jwt": info["token"]}

    # Cold start — Colab returns a queue token, poll until GPU is allocated
    queue_token = body.get("token")
    if not queue_token:
        raise RuntimeError(f"No runtimeProxyInfo and no queue token: {body}")

    log.info("[api] cold start — polling for GPU allocation (up to 20 min)...")
    deadline    = time.time() + 1200
    attempt     = 0
    last_token  = queue_token   # Colab issues a new token each response — always use the latest
    while time.time() < deadline:
        attempt += 1
        time.sleep(5)
        poll_url = (
            "https://colab.research.google.com/tun/m/assign"
            f"?authuser=0&variant=GPU&accelerator=T4&match_any=1"
            f"&nbh={nbh}&token={last_token}"
        )
        log.info(f"[api] poll {attempt}...")
        pr   = requests.get(poll_url, headers=_HEADERS, cookies=cookies, timeout=30)
        pr.raise_for_status()
        pb   = json.loads(pr.text.lstrip(")]}'\n").strip())
        log.info(f"[api] poll response: {json.dumps(pb)}")
        if "runtimeProxyInfo" in pb:
            info = pb["runtimeProxyInfo"]
            log.info(f"[api] runtime assigned (cold): {info['url']}")
            return {"runtime_url": info["url"], "jwt": info["token"]}
        # Update token for next poll
        if "token" in pb:
            last_token = pb["token"]

    raise RuntimeError("Timed out waiting for GPU allocation after 5 min")


def create_session(runtime_url: str, jwt: str, file_id: str) -> tuple:
    """
    POST /api/sessions
    Returns (session_id, kernel_id).
    Both confirmed from live HAR response shape.
    """
    url  = (
        f"{runtime_url}/api/sessions"
        f"?authuser=0&colab-runtime-proxy-token={jwt}"
    )
    body = {
        "name":   "cell1.ipynb",
        "path":   f"fileId={file_id}",
        "type":   "notebook",
        "kernel": {"name": "python3"},
    }
    log.info("[api] creating kernel session...")
    resp = requests.post(url, json=body, timeout=30)
    resp.raise_for_status()

    data       = resp.json()
    session_id = data["id"]
    kernel_id  = data["kernel"]["id"]
    log.info(f"[api] session={session_id[:8]}... kernel={kernel_id[:8]}...")
    return session_id, kernel_id


def execute_cells(runtime_url: str, jwt: str, session_id: str, kernel_id: str, cells: list):
    """
    Jupyter wire protocol over WebSocket.

    URL shape confirmed from live HAR:
      wss://{host}/api/kernels/{kernel_id}/channels
        ?session_id={session_id}
        &colab-runtime-proxy-token={jwt}

    Waits for execute_reply on each cell before sending the next,
    except the last cell (the blocking rpc-server loop — fire and forget).
    """
    wss_host = runtime_url.replace("https://", "wss://")
    ws_url   = (
        f"{wss_host}/api/kernels/{kernel_id}/channels"
        f"?session_id={session_id}"
        f"&colab-runtime-proxy-token={jwt}"
    )

    log.info("[ws] connecting...")
    ws = websocket.create_connection(ws_url, timeout=30)
    log.info("[ws] connected")

    for i, code in enumerate(cells):
        is_last = (i == len(cells) - 1)
        msg_id  = uuid.uuid4().hex

        msg = {
            "header": {
                "msg_id":   msg_id,
                "username": "coordinator",
                "session":  session_id,
                "msg_type": "execute_request",
                "version":  "5.3",
                "date":     time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()),
            },
            "parent_header": {},
            "metadata":      {},
            "content": {
                "code":             code,
                "silent":           False,
                "store_history":    True,
                "user_expressions": {},
                "allow_stdin":      False,
                "stop_on_error":    True,
            },
            "buffers": [],
            "channel": "shell",
        }

        tag = "(fire-and-forget — blocking cell)" if is_last else ""
        log.info(f"[ws] execute cell {i+1}/{len(cells)} {tag}")
        ws.send(json.dumps(msg))

        if is_last:
            break   # rpc-server runs forever, no reply expected

        # Wait for execute_reply before sending next cell
        deadline = time.time() + 180   # 3 min per cell (build can be slow)
        replied  = False
        while time.time() < deadline:
            try:
                ws.settimeout(5)
                raw   = ws.recv()
                reply = json.loads(raw)

                # Print stream output so we can see build progress
                if reply.get("msg_type") == "stream":
                    txt = reply.get("content", {}).get("text", "").strip()
                    if txt:
                        log.info(f"[colab] {txt}")

                ph = reply.get("parent_header", {})
                if ph.get("msg_id") == msg_id and reply.get("msg_type") == "execute_reply":
                    status = reply["content"].get("status", "?")
                    log.info(f"[ws] cell {i+1} → {status}")
                    if status == "error":
                        tb = reply["content"].get("traceback", [])
                        for line in tb:
                            log.error(f"[colab] {line}")
                    replied = True
                    break

            except websocket.WebSocketTimeoutException:
                continue

        if not replied:
            log.warning(f"[ws] cell {i+1} timed out — continuing anyway")

    ws.close()
    log.info("[ws] all cells dispatched — rpc-server booting on Colab")


def _build_cells(backend: dict) -> list:
    """
    Returns the 4 notebook cells as strings.
    Mirrors your working notebook exactly.
    """

    # Cell 2 needs the tailscale command — build it as a plain string
    # to avoid f-string-in-f-string quoting hell
    ts_cmd = (
        "tailscale up"
        " --authkey=" + TAILSCALE_KEY +
        " --hostname=" + backend["rpc_host"] +
        " --accept-dns=false"
    )

    cell1 = (
        "import subprocess, sys\n"
        "subprocess.run(['apt-get','update','-y'], check=True, capture_output=True)\n"
        "subprocess.run(\n"
        "    ['apt-get','install','-y','build-essential','cmake','ninja-build'],\n"
        "    check=True, capture_output=True\n"
        ")\n"
        "print('deps ok')"
    )

    cell2 = (
        "import subprocess, time\n"
        "subprocess.run(\n"
        "    'curl -fsSL https://tailscale.com/install.sh | sh',\n"
        "    shell=True, check=True, capture_output=True\n"
        ")\n"
        "subprocess.Popen(\n"
        "    'nohup tailscaled --tun=userspace-networking"
        " --socks5-server=localhost:1055 > tailscaled.log 2>&1 &',\n"
        "    shell=True\n"
        ")\n"
        "time.sleep(5)\n"
        f"subprocess.run('{ts_cmd}', shell=True, check=True)\n"
        "ip = subprocess.check_output(['tailscale','ip','-4']).decode().strip()\n"
        "print('Tailscale IP:', ip)"
    )

    cell3 = (
        "import subprocess, os\n"
        "if not os.path.exists('/tmp/llama'):\n"
        "    subprocess.run(\n"
        "        'git clone --depth=1"
        " https://github.com/ggerganov/llama.cpp /tmp/llama',\n"
        "        shell=True, check=True\n"
        "    )\n"
        "os.chdir('/tmp/llama')\n"
        "subprocess.run(\n"
        "    'cmake -B build -DGGML_CUDA=ON -DLLAMA_RPC=ON -G Ninja',\n"
        "    shell=True, check=True\n"
        ")\n"
        "subprocess.run(\n"
        "    'cmake --build build -j --target rpc-server',\n"
        "    shell=True, check=True\n"
        ")\n"
        "print('rpc-server built')"
    )

    # Blocking — runs forever, heartbeat keeps Colab from timing out
    cell4 = (
        "import subprocess, threading, time\n"
        "proc = subprocess.Popen(\n"
        "    ['/tmp/llama/build/bin/rpc-server', '-H', '0.0.0.0', '-p', '50052'],\n"
        "    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True\n"
        ")\n"
        "def heartbeat():\n"
        "    while proc.poll() is None:\n"
        "        time.sleep(30)\n"
        "        print(f'[{time.strftime(\"%H:%M:%S\")}]"
        " rpc-server alive pid={proc.pid}', flush=True)\n"
        "threading.Thread(target=heartbeat, daemon=True).start()\n"
        "for line in proc.stdout:\n"
        "    print(line, end='', flush=True)"
    )

    return [cell1, cell2, cell3, cell4]


# ─── Full restart ────────────────────────────────────────────────────────────

def _colab_restart(backend: dict):
    file_id = file_id_from_url(backend["notebook_url"])
    log.info(f"[restart] '{backend['name']}'  fileId={file_id}")
    try:
        cookies              = get_google_cookies()
        runtime              = assign_runtime(file_id, cookies)
        session_id, kern_id  = create_session(
            runtime["runtime_url"], runtime["jwt"], file_id
        )
        execute_cells(
            runtime["runtime_url"], runtime["jwt"],
            session_id, kern_id,
            _build_cells(backend),
        )
    except Exception as exc:
        log.error(f"[restart] FAILED: {exc}")
        state["restarting"] = False

# ═══════════════════════════════════════════════════════════════
#  RPC HEALTH CHECK  (Tailscale TCP probe)
# ═══════════════════════════════════════════════════════════════

def probe_tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def find_best_backend():
    for b in sorted(BACKENDS, key=lambda x: x["priority"]):
        alive = probe_tcp(b["rpc_host"], b["rpc_port"])
        prev  = state["backends"].get(b["name"])
        state["backends"][b["name"]] = "online" if alive else "offline"
        if alive and prev != "online":
            log.info(f"[rpc] '{b['name']}' ONLINE")
        elif not alive and prev == "online":
            log.warning(f"[rpc] '{b['name']}' OFFLINE")
        if alive:
            return b
    return None

# ═══════════════════════════════════════════════════════════════
#  llama-server LIFECYCLE
# ═══════════════════════════════════════════════════════════════

def _llama_cmd(backend: dict) -> list:
    cmd = [
        LLAMA["binary"],
        "-m",          LLAMA["model"],
        "--rpc",       f"{backend['rpc_host']}:{backend['rpc_port']}",
        "-ngl",        str(LLAMA["n_gpu_layers"]),
        "--ctx-size",  str(LLAMA["ctx_size"]),
        "--port",      str(LLAMA["server_port"]),
        "--host",      "127.0.0.1",
    ]
    if LLAMA.get("tensor_split"):
        cmd += ["-ts", LLAMA["tensor_split"]]
    return cmd + LLAMA.get("extra_args", [])


def start_llama(backend: dict):
    stop_llama()
    cmd = _llama_cmd(backend)
    log.info(f"[llama] starting  rpc={backend['rpc_host']}:{backend['rpc_port']}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    state["llama_proc"]     = proc
    state["active_backend"] = backend["name"]
    _event(f"llama-server started via {backend['name']}")

    def _pipe():
        for line in proc.stdout:
            log.info(f"[llama] {line.rstrip()}")
    Thread(target=_pipe, daemon=True).start()


def stop_llama():
    proc = state.get("llama_proc")
    if proc and proc.poll() is None:
        log.info("[llama] stopping...")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    state["llama_proc"]     = None
    state["active_backend"] = None


def llama_alive() -> bool:
    p = state.get("llama_proc")
    return p is not None and p.poll() is None

# ═══════════════════════════════════════════════════════════════
#  MONITOR LOOP
# ═══════════════════════════════════════════════════════════════

async def monitor():
    log.info("=" * 62)
    log.info("  GPU Coordinator — Direct Colab API, no browser")
    log.info(f"  {len(BACKENDS)} backend(s)  |  poll every {POLL_INTERVAL}s")
    log.info(f"  Status → http://127.0.0.1:{STATUS_PORT}/status")
    log.info("=" * 62)

    while True:
        state["last_check"] = time.time()

        if state["restarting"]:
            log.info("[monitor] restart in progress — polling for RPC...")
            await asyncio.sleep(POLL_INTERVAL)
            b = find_best_backend()
            if b:
                log.info(f"[monitor] RPC back via '{b['name']}'")
                state["restarting"] = False
                notify(f"✅ Colab GPU back ({b['name']})")
                start_llama(b)
            continue

        log.info("[monitor] probing backends...")
        backend = find_best_backend()

        if backend:
            if state["active_backend"] != backend["name"]:
                notify(f"✅ GPU online ({backend['name']})")
                start_llama(backend)
            elif not llama_alive():
                log.warning("[monitor] llama-server died — restarting")
                _event("llama-server crash restart")
                start_llama(backend)
            else:
                log.info(f"[monitor] OK — running via '{backend['name']}'")
        else:
            if llama_alive():
                stop_llama()
                _event("all backends offline")

            target = sorted(BACKENDS, key=lambda x: x["priority"])[0]
            log.warning(f"[monitor] all offline — restarting '{target['name']}' via Colab API")
            notify("⚠️ GPU offline — auto-restarting via Colab API...")
            state["restarting"] = True
            _event(f"Colab API restart: {target['name']}")
            Thread(target=_colab_restart, args=(target,), daemon=True).start()

        await asyncio.sleep(POLL_INTERVAL)

# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def notify(msg: str):
    try:
        if platform.system() == "Windows":
            from win10toast import ToastNotifier
            ToastNotifier().show_toast("GPU Coordinator", msg, duration=8, threaded=True)
        elif platform.system() == "Darwin":
            os.system(f'osascript -e \'display notification "{msg}" with title "GPU Coordinator"\'')
        else:
            os.system(f'notify-send "GPU Coordinator" "{msg}"')
    except Exception:
        pass
    log.info(f"[notify] {msg}")


def _event(msg: str):
    state["events"].append({"t": time.strftime("%H:%M:%S"), "msg": msg})
    state["events"] = state["events"][-30:]

# ═══════════════════════════════════════════════════════════════
#  STATUS API   GET http://127.0.0.1:7860/status
# ═══════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/status"):
            llama_url = (
                f"http://127.0.0.1:{LLAMA['server_port']}" if llama_alive() else None
            )
            body = json.dumps({
                "llama_running":   llama_alive(),
                "llama_url":       llama_url,
                "openai_endpoint": f"{llama_url}/v1" if llama_url else None,
                "active_backend":  state["active_backend"],
                "restarting":      state["restarting"],
                "backends":        state["backends"],
                "last_check":      state["last_check"],
                "events":          state["events"],
            }, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass

# ═══════════════════════════════════════════════════════════════
#  ENTRY
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", STATUS_PORT), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()

    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        log.info("shutting down...")
        stop_llama()
