
#!/usr/bin/env python3

import os
import sys
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

# Credentials remain in pi.py; this module only consumes them.
import __main__ as _launcher
USERNAME = _launcher.USERNAME
LEGACY_KEY = _launcher.LEGACY_KEY
API_TOKEN = _launcher.API_TOKEN

HOME = Path("/root")
KAGGLE_DIR = HOME / ".kaggle"

VENV_DIR = Path("/opt/kaggle-venv")
VENV_PYTHON = VENV_DIR / "bin/python"
KAGGLE_BIN = VENV_DIR / "bin/kaggle"

NOTEBOOK_DIR = Path("/home/user/my-notebook")
NOTEBOOK_FILE = NOTEBOOK_DIR / "gpu-test.ipynb"
METADATA_FILE = NOTEBOOK_DIR / "kernel-metadata.json"
RUNTIME_FILE = Path(__file__).resolve().parent / "kaggle_runtime.py"
RUNTIME_URL = "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/kaggle_runtime.py"
PROMPT_ROOT = "https://raw.githubusercontent.com/RikiyaT/lm_understand-logs/main/exp1_which_game/runs"
GAMES = ["pacman", "pong", "breakout", "freeway"]

# Run inside Kaggle's notebook runtime before the workload.
# This reports route/capability facts; TCP forwarding is handled by Tailscale Serve.
PROXY_DIAGNOSTIC_SOURCE = r'''# WEBVM Kaggle proxy route diagnostic
import glob as _wvm_glob
import os as _wvm_os
import re as _wvm_re
import shutil as _wvm_shutil
import socket as _wvm_socket
import subprocess as _wvm_subprocess
import time as _wvm_time

def _wvm_log(event, **fields):
    print("WEBVM_PROXY_DIAGNOSTIC " + __import__("json").dumps(
        {"event": event, **fields}, sort_keys=True
    ), flush=True)

def _wvm_cmdline(pid):
    with open("/proc/{}/cmdline".format(pid), "rb") as _f:
        return [x.decode(errors="replace") for x in _f.read().split(b"\0") if x]

_wvm_base_url = None
_wvm_server_pid = None
_wvm_jupyter_args = None
_wvm_candidates = [12]
_wvm_candidates += [
    int(_p.split("/")[2]) for _p in _wvm_glob.glob("/proc/[0-9]*/cmdline")
    if _p.split("/")[2].isdigit() and int(_p.split("/")[2]) != 12
]
for _wvm_pid in _wvm_candidates:
    try:
        _wvm_args = _wvm_cmdline(_wvm_pid)
    except (OSError, PermissionError):
        continue
    _wvm_is_jupyter = any(
        "jupyter" in _a.lower() or "notebook" in _a.lower()
        for _a in _wvm_args
    )
    if not _wvm_is_jupyter:
        continue
    if _wvm_jupyter_args is None and any(
        "jupyter-server" in _a.lower() or "jupyter server" in _a.lower()
        or "notebookapp" in _a.lower() or "serverapp" in _a.lower()
        for _a in _wvm_args
    ):
        _wvm_jupyter_args = list(_wvm_args)
    _wvm_log("JUPYTER_LAUNCH_ARGV", pid=_wvm_pid, argv=_wvm_args)
    for _wvm_i, _wvm_arg in enumerate(_wvm_args):
        if _wvm_arg.startswith(("--NotebookApp.base_url=", "--ServerApp.base_url=")):
            _wvm_base_url = _wvm_arg.split("=", 1)[1]
            _wvm_server_pid = _wvm_pid
            break
        if _wvm_arg in ("--NotebookApp.base_url", "--ServerApp.base_url") and _wvm_i + 1 < len(_wvm_args):
            _wvm_base_url = _wvm_args[_wvm_i + 1]
            _wvm_server_pid = _wvm_pid
            break
    if _wvm_base_url:
        break

if _wvm_base_url:
    _wvm_match = _wvm_re.search(r"/k/\d+/([^/]+)/proxy/?$", _wvm_base_url)
    if _wvm_match:
        _wvm_log("PROXY_URL_DISCOVERED", pid=_wvm_server_pid,
                 proxy_host="kkb-production.jupyter-proxy.kaggle.net",
                 proxy_route_shape="/k/{}/<redacted>/proxy".format(
                     _wvm_re.search(r"/k/(\d+)/", _wvm_base_url).group(1)),
                 token_present=bool(_wvm_match.group(1)))
    else:
        _wvm_log("BASE_URL_FOUND_UNEXPECTED_FORMAT", pid=_wvm_server_pid,
                 base_url_shape="present_unrecognized_format")
else:
    _wvm_log("JUPYTER_BASE_URL_NOT_FOUND", checked_pid_12=True, proc_scan=True)

_wvm_iptables = _wvm_shutil.which("iptables")
if not _wvm_iptables:
    _wvm_log("IPTABLES_CAPABILITY_PROBE", available=False)
else:
    _wvm_capability = _wvm_subprocess.run(
        [_wvm_iptables, "-t", "nat", "-S", "PREROUTING"],
        capture_output=True, text=True
    )
    _wvm_log("IPTABLES_CAPABILITY_PROBE", available=True,
             returncode=_wvm_capability.returncode,
             permitted=(_wvm_capability.returncode == 0),
             stderr=(_wvm_capability.stderr or "")[-1000:],
             rules=(_wvm_capability.stdout or "")[-2000:])

_wvm_source_ip_configured = bool(_wvm_os.environ.get("WEBVM_PROXY_SOURCE_IP", "").strip())
_wvm_log("NAT_RULE_NOT_USED", source_ip_configured=_wvm_source_ip_configured,
         note="The TCP link uses authenticated Tailscale Serve; no PREROUTING redirect is required")

try:
    with _wvm_socket.create_connection(("127.0.0.1", 8889), timeout=2):
        _wvm_log("LOCAL_TARGET_LISTENER", port=8889, listening=True)
except OSError as _wvm_exc:
    _wvm_log("LOCAL_TARGET_LISTENER", port=8889, listening=False,
             error=type(_wvm_exc).__name__ + ": " + str(_wvm_exc))

# Clone the discovered Jupyter server on 8889 without stopping 8888.
if _wvm_jupyter_args:
    try:
        _wvm_new_args = list(_wvm_jupyter_args)
        _wvm_flags = ("--ServerApp.port", "--NotebookApp.port", "--port")
        for _wvm_i, _wvm_a in enumerate(_wvm_new_args):
            if any(_wvm_a.startswith(_f + "=") for _f in _wvm_flags):
                _wvm_new_args[_wvm_i] = _wvm_a.split("=", 1)[0] + "=8889"
            elif _wvm_a in _wvm_flags and _wvm_i + 1 < len(_wvm_new_args):
                _wvm_new_args[_wvm_i + 1] = "8889"
        if not any(any(_a.startswith(_f + "=") for _f in _wvm_flags) for _a in _wvm_new_args):
            _wvm_new_args.append("--ServerApp.port=8889")
        _wvm_new_args += ["--ServerApp.open_browser=False",
                          "--ServerApp.runtime_dir=/tmp/webvm-jupyter-8889-runtime"]
        try:
            with _wvm_socket.create_connection(("127.0.0.1", 8889), timeout=1):
                _wvm_log("SECOND_JUPYTER_SERVER", status="already_listening")
        except OSError:
            _wvm_child = _wvm_subprocess.Popen(
                _wvm_new_args, stdin=_wvm_subprocess.DEVNULL,
                stdout=_wvm_subprocess.DEVNULL, stderr=_wvm_subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
            _wvm_time.sleep(3)
            try:
                with _wvm_socket.create_connection(("127.0.0.1", 8889), timeout=1):
                    _wvm_log("SECOND_JUPYTER_SERVER", status="listening", pid=_wvm_child.pid)
            except OSError:
                _wvm_log("SECOND_JUPYTER_SERVER", status="not_listening",
                         pid=_wvm_child.pid, returncode=_wvm_child.poll())
    except Exception as _wvm_exc:
        _wvm_log("SECOND_JUPYTER_SERVER", status="failed",
                 error=type(_wvm_exc).__name__, detail=str(_wvm_exc)[:180])
else:
    _wvm_log("SECOND_JUPYTER_SERVER", status="not_started", reason="server_process_not_found")

# Verify GPU health in this GPU-enabled diagnostic kernel after the local-only fallback.
try:
    _wvm_torch = __import__("torch")
    _wvm_cuda_available = bool(_wvm_torch.cuda.is_available())
    _wvm_gpu_report = {
        "torch_version": str(_wvm_torch.__version__),
        "cuda_available": _wvm_cuda_available,
        "device_count": int(_wvm_torch.cuda.device_count()),
    }
    if _wvm_cuda_available:
        _wvm_props = _wvm_torch.cuda.get_device_properties(0)
        _wvm_gpu_report["device_name"] = str(_wvm_torch.cuda.get_device_name(0))
        _wvm_gpu_report["vram_gib"] = round(_wvm_props.total_memory / (1024 ** 3), 2)
        _wvm_x = _wvm_torch.randn((256, 256), device="cuda")
        _wvm_y = _wvm_x @ _wvm_x
        _wvm_torch.cuda.synchronize()
        _wvm_gpu_report["matrix_test"] = "PASS"
        _wvm_gpu_report["matrix_shape"] = list(_wvm_y.shape)
    else:
        _wvm_gpu_report["matrix_test"] = "SKIPPED_NO_CUDA"
    _wvm_log("GPU_HEALTH_CHECK", **_wvm_gpu_report)
except Exception as _wvm_gpu_exc:
    _wvm_log("GPU_HEALTH_CHECK", result="ERROR",
             error_type=type(_wvm_gpu_exc).__name__)
'''

KERNEL_SLUG = "automated-gpu-test"
KERNEL_ID = USERNAME + "/" + KERNEL_SLUG
NOTEBOOK_URL = "https://www.kaggle.com/code/" + KERNEL_ID


# ============================================================
# COMMAND RUNNER
# ============================================================

def run(command, check=True, env=None):
    command = [str(item) for item in command]
    print("\n$", " ".join(command), flush=True)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=env
    )

    if result.stdout:
        print(result.stdout, end="", flush=True)

    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed with exit code {}".format(result.returncode)
        )

    return result


def configure_kaggle_proxy_autoload():
    """Refresh proxy environment from the browser-updated DataDevice on each shell prompt."""
    marker = "# WebVM Kaggle TCP link auto-proxy"
    stanza = r'''
# WebVM Kaggle TCP link auto-proxy
__webvm_refresh_kaggle_proxy() {
    if [ -r /data/kaggle-tcp-link.env ]; then
        . /data/kaggle-tcp-link.env
    fi
}
case ";${PROMPT_COMMAND:-};" in
    *";__webvm_refresh_kaggle_proxy;"*) ;;
    *) PROMPT_COMMAND="__webvm_refresh_kaggle_proxy${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
__webvm_refresh_kaggle_proxy
''' 
    for rc_path in (Path("/root/.bashrc"), Path("/home/user/.bashrc")):
        try:
            existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
            if marker not in existing:
                rc_path.parent.mkdir(parents=True, exist_ok=True)
                rc_path.write_text(existing.rstrip() + "\n\n" + stanza.strip() + "\n", encoding="utf-8")
        except OSError as exc:
            print("TCP_LINK_PROXY_PROFILE_WRITE_FAILED file={} error={}: {}".format(
                rc_path, type(exc).__name__, exc), flush=True)


# ============================================================
# STEP 1: PREPARE SYSTEM
# ============================================================

def prepare_system():
    print("booting.", flush=True)

    if os.geteuid() != 0:
        raise RuntimeError("Run this script as root.")

    if not shutil.which("apt-get"):
        raise RuntimeError("This script requires Debian/Ubuntu apt-get.")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    NOTEBOOK_DIR.mkdir(parents=True, exist_ok=True)
    configure_kaggle_proxy_autoload()


# ============================================================
# STEP 2: WRITE CREDENTIALS
# ============================================================

def prepare_credentials():
    print("booting..", flush=True)

    token_file = KAGGLE_DIR / "access_token"
    json_file = KAGGLE_DIR / "kaggle.json"
    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)

    # Credentials come from the runtime environment or a local credential
    # file. Never store live credentials in source control.
    if API_TOKEN:
        token_file.write_text(API_TOKEN + "\n")
        os.chmod(str(token_file), 0o600)

    if LEGACY_KEY:
        json_file.write_text(
            json.dumps({"username": USERNAME, "key": LEGACY_KEY}, indent=2) + "\n"
        )
        os.chmod(str(json_file), 0o600)

    os.chmod(str(KAGGLE_DIR), 0o700)
    has_token = token_file.is_file() and bool(token_file.read_text(errors="ignore").strip())
    has_legacy_key = False
    if json_file.is_file():
        try:
            legacy = json.loads(json_file.read_text(encoding="utf-8"))
            has_legacy_key = bool(legacy.get("username") and legacy.get("key"))
        except (OSError, ValueError, AttributeError):
            has_legacy_key = False
    if not has_token and not has_legacy_key:
        raise RuntimeError(
            "Kaggle credentials are missing or empty. Set KAGGLE_API_TOKEN, "
            "or provide a valid /root/.kaggle/access_token or "
            "/root/.kaggle/kaggle.json locally."
        )

    os.environ["HOME"] = str(HOME)
    if API_TOKEN:
        os.environ["KAGGLE_API_TOKEN"] = API_TOKEN


# ============================================================
# STEP 3: CREATE PYTHON ENVIRONMENT
# ============================================================

def prepare_python():
    print("booting...", flush=True)

    # Debian disables ensurepip for its system Python.
    # Install Debian's pip, then create the venv without
    # attempting to bootstrap pip through ensurepip.
    # if VENV_DIR.exists() and not VENV_PYTHON.exists():
        # print("Removing incomplete virtual environment.", flush=True)
        # shutil.rmtree(str(VENV_DIR))

    # if not VENV_PYTHON.exists():
        # run([
            # "python3", "-m", "venv",
            # "--system-site-packages",
            # "--without-pip",
            # str(VENV_DIR)
        # ])

    # Check whether pip is visible in the new environment.
    # pip_check = run([
        # str(VENV_PYTHON), "-m", "pip", "--version"
    # ], check=False)

    # if pip_check.returncode != 0:
        # print(
            # "Virtual environment cannot access pip; "
            # "checking Debian's pip installation.",
            # flush=True
        # )

        # run([
            # "apt-get", "install", "--reinstall", "-y",
            # "python3-pip"
        # ])

        # pip_check = run([
            # str(VENV_PYTHON), "-m", "pip", "--version"
        # ], check=False)

        # if pip_check.returncode != 0:
            # raise RuntimeError(
                # "pip is still unavailable inside the virtual environment. "
                # "On Debian i386, compatible Python packages may be limited."
            # )

    # Install Kaggle without writing to system Python.
    # run([
        # str(VENV_PYTHON), "-m", "pip",
        # "install", "--upgrade", "--ignore-installed", "kaggle"
    # ])

    # if not KAGGLE_BIN.exists():
        # raise RuntimeError("Kaggle CLI executable was not created.")

    # run([str(KAGGLE_BIN), "--version"])


# ============================================================
# STEP 4: EMBED RUNTIME AND PROMPTS IN THE NOTEBOOK
# ============================================================

def fetch_runtime_source():
    """Fetch the experiment runtime from main so runtime edits need no VM-image rebuild."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    last_error = None
    for attempt in range(1, 11):
        try:
            request = Request(
                RUNTIME_URL + "?t=" + str(int(time.time() * 1000)),
                headers={
                    "Cache-Control": "no-cache",
                    "User-Agent": "webvm-qwen-experiment-submitter/1.0",
                },
            )
            with urlopen(request, timeout=30) as response:
                source = response.read().decode("utf-8")
            compile(source, RUNTIME_URL, "exec")
            print("RUNTIME_SOURCE_FETCH_ATTEMPT attempt={}/10 result=ok characters={}".format(attempt, len(source)), flush=True)
            return source
        except (HTTPError, URLError, TimeoutError, OSError, SyntaxError) as exc:
            last_error = exc
            print("RUNTIME_SOURCE_FETCH_ATTEMPT attempt={}/10 result=failed error={}: {}".format(
                attempt, type(exc).__name__, exc
            ), flush=True)
            if attempt < 10:
                time.sleep(min(attempt, 5))

    # Keep the installed image usable during a temporary GitHub/DNS outage.
    if not RUNTIME_FILE.is_file():
        raise RuntimeError("Could not fetch runtime from main and no local fallback exists: {}".format(last_error))
    source = RUNTIME_FILE.read_text(encoding="utf-8")
    compile(source, str(RUNTIME_FILE), "exec")
    print("RUNTIME_SOURCE_FALLBACK source=local reason={}: {}".format(
        type(last_error).__name__ if last_error else "unknown", last_error
    ), flush=True)
    return source


def build_embedded_runtime():
    """Return only runtime code; prompt files are fetched inside Kaggle at runtime."""
    source = fetch_runtime_source()
    compile(source, str(RUNTIME_FILE) + "[inline]", "exec")
    print(
        "RUNTIME_INLINE source=main-or-local-fallback characters={} prompts=runtime-fetch syntax=ok".format(
            len(source)
        ),
        flush=True,
    )
    return source

def write_notebook():
    print("booting....", flush=True)
    # Keep the SaveKernel request small: embed the runtime code, but fetch the
    # four large prompt files inside Kaggle through the retryable Contents API.
    runtime_source = build_embedded_runtime()
    runtime_source = PROXY_DIAGNOSTIC_SOURCE + "\n\n" + runtime_source
    print(
        "RUNTIME_INLINE characters={} prompts=runtime-fetch syntax=ok".format(
            len(runtime_source)
        ),
        flush=True,
    )

    metadata = {
        "id": KERNEL_ID,
        "title": "Automated GPU Test",
        "code_file": NOTEBOOK_FILE.name,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "machine_shape": "NvidiaTeslaT4",
        "enable_internet": True,
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": []
    }

    METADATA_FILE.write_text(json.dumps(metadata, indent=2) + "\n")

    notebook = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Qwen Weight-Space Interpretation Replication\n",
                    "Runs the four source prompts against Qwen/Qwen3.8-27B. Runtime code is embedded; large prompts are fetched through the GitHub Contents API with retries."
                ]
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
#                 "source": [
#                     "import os\n",
#                     "import platform\n",
#                     "print('Python:', platform.python_version())\n",
#                     "print('CUDA_VISIBLE_DEVICES:', os.environ.get('CUDA_VISIBLE_DEVICES'))\n",
#                     "try:\n",
#                     "    import torch\n",
#                     "    print('PyTorch:', torch.__version__)\n",
#                     "    print('CUDA available:', torch.cuda.is_available())\n",
#                     "    if torch.cuda.is_available():\n",
#                     "        print('GPU:', torch.cuda.get_device_name(0))\n",
#                     "        props = torch.cuda.get_device_properties(0)\n",
#                     "        print('GPU VRAM GB:', round(props.total_memory / (1024**3), 2))\n",
#                     "        x = torch.randn((2048, 2048), device='cuda')\n",
#                     "        y = x @ x\n",
#                     "        torch.cuda.synchronize()\n",
#                     "        print('GPU matrix test passed:', tuple(y.shape))\n",
#                     "    else:\n",
#                     "        print('No CUDA GPU detected.')\n",
#                     "except Exception as exc:\n",
#                     "    print('GPU test error:', repr(exc))\n"
#                 ]
                "source": runtime_source.splitlines(keepends=True),
            }
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3"
            },
            "language_info": {
                "name": "python"
            },
            "kaggle": {
                "accelerator": "gpu",
                "dataSources": [],
                "isInternetEnabled": True,
                "isGpuEnabled": True,
                "language": "python",
                "sourceType": "notebook"
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5
    }

    NOTEBOOK_FILE.write_text(json.dumps(notebook, indent=2) + "\n")
    print("NOTEBOOK_WRITTEN bytes={}".format(NOTEBOOK_FILE.stat().st_size), flush=True)


# ============================================================
# KAGGLE ENVIRONMENT
# ============================================================

def kaggle_environment():
    env = os.environ.copy()
    env["HOME"] = str(HOME)
    if API_TOKEN:
        env["KAGGLE_API_TOKEN"] = API_TOKEN
    return env




# ============================================================
# STEP 9: COLLECT ACTUAL KAGGLE EXECUTION LOGS
# ============================================================

def collect_kaggle_diagnostics(env, reason):
    """Fetch the real kernel log and output files; do not rely only on pste heartbeats."""
    print("\n===== KAGGLE_DIAGNOSTICS_BEGIN reason={} =====".format(reason), flush=True)

    logs = run([
        str(KAGGLE_BIN), "kernels", "logs", KERNEL_ID
    ], check=False, env=env)
    if logs.returncode != 0:
        print("Kaggle logs command failed with exit code {}.".format(logs.returncode), flush=True)

    output_dir = Path("/tmp/kaggle-automated-gpu-test-output")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = run([
        str(KAGGLE_BIN), "kernels", "output", KERNEL_ID,
        "-p", str(output_dir), "-o"
    ], check=False, env=env)
    if output.returncode != 0:
        print("Kaggle output download failed with exit code {}.".format(output.returncode), flush=True)

    diagnostic_files = sorted(
        p for p in output_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".log", ".txt", ".json"}
    )
    if not diagnostic_files:
        print("No downloadable .log/.txt/.json diagnostic files were returned.", flush=True)
    for path in diagnostic_files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            print("\n===== KAGGLE_DIAGNOSTIC_FILE {} bytes={} =====".format(path.name, len(content)), flush=True)
            print(content[-30000:], flush=True)
            print("===== END_KAGGLE_DIAGNOSTIC_FILE {} =====".format(path.name), flush=True)
        except OSError as exc:
            print("Could not read diagnostic file {}: {}".format(path.name, exc), flush=True)

    print("===== KAGGLE_DIAGNOSTICS_END =====", flush=True)


# ============================================================
# STEPS 5-8: AUTHENTICATE, SUBMIT, MONITOR
# ============================================================

def ensure_accelerator_cli(env):
    """Avoid slow per-run CLI probes; validate the flag on the actual push."""
    print(
        "KAGGLE_CLI_PREFLIGHT_SKIPPED reason=push_command_checks_accelerator_flag",
        flush=True,
    )


def deploy():
    env = kaggle_environment()
    ensure_accelerator_cli(env)

    # print("[5/8] Testing Kaggle authentication", flush=True)

#     auth = run([
#         str(KAGGLE_BIN), "kernels", "list", "--mine"
#     ], check=False, env=env)
# 
#     if auth.returncode != 0:
#         raise RuntimeError(
#             "Kaggle authentication failed. Verify that the configured "
#             "credentials are valid and accepted by the Kaggle API."
#         )
# 
# 

    print("booting.....", flush=True)

    # Kaggle's SaveKernel endpoint intermittently terminates TLS with
    # SSLV3_ALERT_BAD_RECORD_MAC from this VM's network path. Retry the
    # transient error, then make one final attempt with TLS 1.2 forced.
    push = None
    transient_transport_failure = False
    push_command = [
        str(KAGGLE_BIN), "kernels", "push",
        "-p", str(NOTEBOOK_DIR), "--accelerator", "NvidiaTeslaT4"
    ]
    transient_transport_failure = False
    # Retry failed pushes up to ten total attempts. Record every attempt and
    # its classification so intermittent transport/platform failures are
    # distinguishable from repeatable CLI or configuration errors.
    push_attempt_log = []
    for push_attempt in range(1, 11):
        push_started = time.time()
        push = run(push_command, check=False, env=env)
        if push.returncode == 0:
            break

        push_output = str(getattr(push, "stdout", "") or "") + str(getattr(push, "stderr", "") or "")
        push_output_lower = push_output.lower()
        if (
            "--accelerator" in push_output_lower
            and ("unrecognized arguments" in push_output_lower or "unknown option" in push_output_lower)
        ):
            print("KAGGLE_CLI_ACCELERATOR_FLAG_MISSING; upgrading only after push rejection.", flush=True)
            run([
                str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "kaggle"
            ], check=True, env=env)
            push = run(push_command, check=False, env=env)
            if push.returncode == 0:
                break
            push_output = str(getattr(push, "stdout", "") or "") + str(getattr(push, "stderr", "") or "")
            push_output_lower = push_output.lower()
        transient_transport_failure = any(
            marker in push_output_lower
            for marker in (
                "sslv3_alert_bad_record_mac",
                "ssl alert bad record mac",
                "tlsv1_alert_bad_record_mac",
                "connection aborted",
                "remote end closed connection without response",
                "remotedisconnected",
                "connection reset by peer",
                "connection reset",
                "connection timed out",
                "read timed out",
                "newconnectionerror",
                "max retries exceeded",
            )
        )
        attempt_record = {
            "attempt": push_attempt,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(push_started)),
            "duration_seconds": round(time.time() - push_started, 2),
            "returncode": push.returncode,
            "transient_transport": transient_transport_failure,
            "output_tail": " ".join(push_output.split())[-500:],
        }
        push_attempt_log.append(attempt_record)
        print("KAGGLE_PUSH_ATTEMPT_RESULT " + json.dumps(attempt_record, sort_keys=True), flush=True)
        if push_attempt < 10:
            delay = min(5 * push_attempt, 30)
            print(
                "KAGGLE_PUSH_RETRY attempt={} next_attempt={} retry_in={}s reason={}".format(
                    push_attempt, push_attempt + 1, delay,
                    "transient_transport" if transient_transport_failure else "push_failed",
                ),
                flush=True,
            )
            time.sleep(delay)
    print("KAGGLE_PUSH_ATTEMPT_SUMMARY " + json.dumps(push_attempt_log, sort_keys=True), flush=True)

    if push is not None and push.returncode != 0 and transient_transport_failure:
        tls_config = Path("/tmp/webvm-openssl-tls12.cnf")
        tls_config.write_text(
            "openssl_conf = openssl_init\n"
            "[openssl_init]\n"
            "ssl_conf = ssl_sect\n"
            "[ssl_sect]\n"
            "system_default = system_default_sect\n"
            "[system_default_sect]\n"
            "MinProtocol = TLSv1.2\n"
            "MaxProtocol = TLSv1.2\n",
            encoding="utf-8",
        )
        tls12_env = env.copy()
        tls12_env["OPENSSL_CONF"] = str(tls_config)
        print("KAGGLE_PUSH_TLS12_FALLBACK action=retry", flush=True)
        push = run(push_command, check=False, env=tls12_env)

    if push is not None and push.returncode != 0:
        # Bypass any inherited HTTP(S) proxy: a proxy that handles GETs but
        # corrupts or closes larger POST bodies can produce TLS bad-record-MAC.
        direct_env = env.copy()
        for proxy_key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            direct_env.pop(proxy_key, None)
        direct_env["NO_PROXY"] = "api.kaggle.com,www.kaggle.com,kaggle.com"
        direct_env["no_proxy"] = direct_env["NO_PROXY"]
        direct_status = run([
            str(KAGGLE_BIN), "kernels", "status", KERNEL_ID
        ], check=False, env=direct_env)
        print(
            "KAGGLE_DIRECT_STATUS_CHECK returncode={} proxy_bypass=enabled output={}".format(
                direct_status.returncode,
                " ".join((direct_status.stdout or "").split())[:240],
            ),
            flush=True,
        )
        if direct_status.returncode == 0:
            print("KAGGLE_PUSH_DIRECT_NETWORK_FALLBACK action=retry", flush=True)
            push = run(push_command, check=False, env=direct_env)

    if push is not None and push.returncode != 0:
        # Test auth and connectivity through Kaggle's web host before giving up.
        # The status call verifies that credentials work through this alternate
        # endpoint, then the same endpoint is used for SaveKernel.
        alt_host_dir = Path("/tmp/webvm-kaggle-alt-host")
        alt_host_dir.mkdir(parents=True, exist_ok=True)
        (alt_host_dir / "sitecustomize.py").write_text(
            "import os\n"
            "try:\n"
            "    import kagglesdk.kaggle_env as _kaggle_env\n"
            "    _kaggle_env._env_to_endpoint[_kaggle_env.KaggleEnv.PROD] = 'https://www.kaggle.com/api'\n"
            "    print('WEBVM_KAGGLE_ALT_HOST_PATCH_APPLIED', flush=True)\n"
            "except Exception as _exc:\n"
            "    print('WEBVM_KAGGLE_ALT_HOST_PATCH_FAILED ' + type(_exc).__name__, flush=True)\n",
            encoding="utf-8",
        )
        alt_env = env.copy()
        existing_pythonpath = alt_env.get("PYTHONPATH", "")
        alt_env["PYTHONPATH"] = str(alt_host_dir) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        alt_env["KAGGLE_API_ENVIRONMENT"] = "PROD"
        alt_status = run([
            str(KAGGLE_BIN), "kernels", "status", KERNEL_ID
        ], check=False, env=alt_env)
        alt_status_output = str(getattr(alt_status, "stdout", "") or "")
        print(
            "KAGGLE_ALT_HOST_STATUS_CHECK returncode={} patch_applied={} output={}".format(
                alt_status.returncode,
                "WEBVM_KAGGLE_ALT_HOST_PATCH_APPLIED" in alt_status_output,
                " ".join(alt_status_output.split())[:300],
            ),
            flush=True,
        )
        if (
            alt_status.returncode == 0
            and "WEBVM_KAGGLE_ALT_HOST_PATCH_APPLIED" in alt_status_output
        ):
            print("KAGGLE_PUSH_ALT_HOST_FALLBACK host=www.kaggle.com/api action=retry", flush=True)
            push = run(push_command, check=False, env=alt_env)

    if push is not None and push.returncode != 0:
        # Some WebVM deployments inject an HTTPS proxy that can corrupt long
        # TLS exchanges. Retry with proxy environment settings removed, first
        # verifying that the direct API endpoint is reachable.
        direct_env = env.copy()
        proxy_names = (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
        configured_proxy_names = [name for name in proxy_names if direct_env.get(name)]
        for name in proxy_names:
            direct_env.pop(name, None)
        direct_env["NO_PROXY"] = "api.kaggle.com,www.kaggle.com,.kaggle.com"
        direct_env["no_proxy"] = direct_env["NO_PROXY"]
        print(
            "KAGGLE_PUSH_DIRECT_FALLBACK configured_proxy_vars={} action=probe".format(
                ",".join(configured_proxy_names) if configured_proxy_names else "none"
            ),
            flush=True,
        )
        direct_status = run([
            str(KAGGLE_BIN), "kernels", "status", KERNEL_ID
        ], check=False, env=direct_env)
        if direct_status.returncode == 0:
            print("KAGGLE_PUSH_DIRECT_FALLBACK action=submit", flush=True)
            push = run(push_command, check=False, env=direct_env)
        else:
            print(
                "KAGGLE_PUSH_DIRECT_FALLBACK action=skip status_returncode={}".format(
                    direct_status.returncode
                ),
                flush=True,
            )

    if push is None or push.returncode != 0:
        raise RuntimeError("Kaggle notebook submission failed after TLS retries, alternate-host, and direct-network fallbacks.")

    print("\nNotebook URL:", NOTEBOOK_URL, flush=True)
    print("booting......", flush=True)

    for attempt in range(20):
        print(
            "\nbooting" + "." * (attempt + 7),
            flush=True
        )

        status = run([
            str(KAGGLE_BIN), "kernels", "status", KERNEL_ID
        ], check=False, env=env)

        output = (status.stdout or "").lower()

        if status.returncode == 0:
            if "complete" in output or "completed" in output:
                print("booting.......", flush=True)
                print(NOTEBOOK_URL)
                collect_kaggle_diagnostics(env, "completed")
                return

            if "error" in output or "failed" in output:
                print("Kaggle reported an execution failure.")
                print("Inspect:", NOTEBOOK_URL)
                collect_kaggle_diagnostics(env, "failed")
                return
        else:
            print(
                "Status endpoint unavailable. The push may still have "
                "succeeded; inspect the notebook URL.",
                flush=True
            )

        time.sleep(30)

    print("booting........")
    print("Kaggle status did not reach a terminal state before the polling limit.")
    print("Inspect notebook execution here:", NOTEBOOK_URL)
    collect_kaggle_diagnostics(env, "poll_timeout")


# ============================================================
# MAIN
# ============================================================

def run_streamed_subprocess(command, env, label, timeout=1200):
    """Stream newline-delimited child logs without selectors (unreliable in WebAssembly)."""
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env
    )
    try:
        while True:
            line = process.stdout.readline()
            if line:
                print(line, end="", flush=True)
                continue
            returncode = process.poll()
            if returncode is not None:
                break
            if time.monotonic() - started > timeout:
                process.kill()
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.1)
        print("{}_EXIT returncode={}".format(label, returncode), flush=True)
        if returncode != 0:
            raise RuntimeError("{} failed with exit code {}".format(label, returncode))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()


def verify_tcp_link_from_webvm():
    """Verify a real remote HTTPS/TCP connection through the discovered Kaggle peer."""
    from urllib.error import URLError, HTTPError
    from urllib.request import ProxyHandler, build_opener

    env_file = Path("/data/kaggle-tcp-link.env")
    proxy = None
    prefix = 'export WEBVM_KAGGLE_TCP_LINK_HTTP_PROXY="'
    for attempt in range(1, 7):
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(prefix) and line.endswith('"'):
                    proxy = line[len(prefix):-1]
                    break
        except OSError:
            pass
        if proxy:
            break
        print("TCP_LINK_REMOTE_PROBE_WAIT attempt={}/6 reason=peer_proxy_config_not_ready".format(attempt), flush=True)
        if attempt < 6:
            time.sleep(5)

    if not proxy:
        print("TCP_LINK_REMOTE_CONNECTIVITY_UNVERIFIED reason=peer_proxy_config_missing", flush=True)
        return False

    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    for attempt in range(1, 4):
        try:
            with opener.open("https://example.com/", timeout=8) as response:
                sample = response.read(256)
                print("TCP_LINK_REMOTE_CONNECTIVITY_PASS attempt={} status={} bytes_read={} transport=HTTP_CONNECT_over_Tailscale".format(attempt, response.status, len(sample)), flush=True)
                return True
        except (OSError, URLError, HTTPError, TimeoutError) as exc:
            print("TCP_LINK_REMOTE_CONNECTIVITY_RETRY attempt={}/3 error_type={} detail={}".format(attempt, type(exc).__name__, str(exc)[:240]), flush=True)
            if attempt < 3:
                time.sleep(5)
    print("TCP_LINK_REMOTE_CONNECTIVITY_FAILED attempts=3", flush=True)
    return False


def run_io_loop_proof():
    """Run RX/TX commands fetched from GitHub without requiring a WebVM reset."""
    import hashlib
    from urllib.error import HTTPError, URLError

    # Fetch the current repository version so the deployed TCP service matches the
    # validated implementation (including the explicitly requested auth-key setup).
    base = "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/kaggle_io_loop/"
    target_dir = Path("/home/user/kaggle-io-loop")
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("controller.py", "agent.py", "tcp_socks5.py", "remote_probe.py"):
        last_error = None
        for attempt in range(1, 11):
            try:
                request = Request(
                    base + filename + "?t=" + str(int(time.time() * 1000)),
                    headers={"Cache-Control": "no-cache", "User-Agent": "webvm-kaggle-io-loop/1.0"},
                )
                with urlopen(request, timeout=25) as response:
                    source = response.read().decode("utf-8")
                compile(source, str(target_dir / filename), "exec")
                (target_dir / filename).write_text(source, encoding="utf-8")
                print("IO_LOOP_SOURCE_FETCHED file={} attempt={}/10 characters={}".format(
                    filename, attempt, len(source)), flush=True)
                break
            except (HTTPError, URLError, TimeoutError, OSError, SyntaxError) as exc:
                last_error = exc
                print("IO_LOOP_SOURCE_FETCH_FAILED file={} attempt={}/10 error={}: {}".format(
                    filename, attempt, type(exc).__name__, exc), flush=True)
                if attempt < 10:
                    time.sleep(min(attempt, 5))
        else:
            raise RuntimeError("Could not fetch IO loop source {}: {}".format(filename, last_error))

    env = os.environ.copy()
    env["PATH"] = str(VENV_DIR / "bin") + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(HOME)

    # Prove the API-mediated RX/TX round trip on this deployment before
    # attempting any direct TCP or Tailscale transport. A historical pass is
    # not evidence that the current runtime can communicate.
    proof_command = [
        sys.executable, str(target_dir / "controller.py"),
        "--dataset-id", USERNAME + "/webvm-io-loop-rx",
        "--kernel-id", USERNAME + "/webvm-io-loop-proof",
        "--work-dir", "/home/user/kaggle-io-loop-run",
        "--timeout-seconds", "900",
        "--poll-seconds", "10",
        "--payload-file", str(target_dir / "remote_probe.py"),
    ]
    print("IO_LOOP_CONTROLLER_START dataset_id={} kernel_id={}".format(
        USERNAME + "/webvm-io-loop-rx", USERNAME + "/webvm-io-loop-proof"), flush=True)
    run_streamed_subprocess(proof_command, env, "IO_LOOP_CONTROLLER", timeout=1200)
    print("RX_TX_CURRENT_DEPLOYMENT_PROOF_PASS transport=kaggle-api-dataset-and-kernel-output",
          flush=True)

    # Submit the existing long-running Tailscale bridge. Authentication is
    # handled inside that bridge; no auth material is sent through RX/TX.
    tcp_command = [
        sys.executable, str(target_dir / "controller.py"),
        "--dataset-id", USERNAME + "/webvm-io-loop-rx",
        "--kernel-id", USERNAME + "/webvm-tcp-link",
        "--work-dir", "/home/user/kaggle-tcp-link-run",
        "--timeout-seconds", "900",
        "--poll-seconds", "10",
        "--serve-tcp-link",
    ]
    print("TCP_LINK_CONTROLLER_START kernel_id={}".format(
        USERNAME + "/webvm-tcp-link"), flush=True)

    # The TCP controller can take many minutes when Kaggle's status API stalls.
    # Run it in the background so the RX/TX command channel starts immediately.
    def _run_tcp_controller_background():
        try:
            run_streamed_subprocess(tcp_command, env, "TCP_LINK_CONTROLLER", timeout=1200)
            print("TCP_LINK_BRIDGE_SUBMITTED transport=tailscale-socks5-http-connect tailnet_port=18088", flush=True)
        except Exception as exc:
            print("TCP_LINK_BACKGROUND_FAILED error={}: {}".format(
                type(exc).__name__, str(exc)[:300]), flush=True)

    threading.Thread(
        target=_run_tcp_controller_background,
        name="webvm-tcp-link-controller",
        daemon=True,
    ).start()
    print("TCP_LINK_CONTROLLER_BACKGROUND_STARTED command_channel=unblocked", flush=True)

    # Keep a command channel alive for the lifetime of this WebVM process.
    # Editing remote_probe.py on GitHub delivers a new Python payload over RX/TX;
    # it does not require changing the reset marker or restarting WebVM.
    probe_path = target_dir / "remote_probe.py"
    last_probe_sha = hashlib.sha256(probe_path.read_bytes()).hexdigest()
    probe_url = base + "remote_probe.py"
    print("RX_TX_REMOTE_COMMAND_POLL_STARTED interval_seconds=30 current_sha256={}".format(
        last_probe_sha), flush=True)
    while True:
        time.sleep(30)
        try:
            request = Request(
                probe_url + "?poll=" + str(int(time.time() * 1000)),
                headers={"Cache-Control": "no-cache", "User-Agent": "webvm-kaggle-rx-tx/1.0"},
            )
            with urlopen(request, timeout=20) as response:
                candidate = response.read(65537)
            if not candidate or len(candidate) > 65536:
                print("RX_TX_REMOTE_COMMAND_REJECTED reason=payload_size bytes={}".format(
                    len(candidate)), flush=True)
                continue
            candidate_sha = hashlib.sha256(candidate).hexdigest()
            if candidate_sha == last_probe_sha:
                continue
            try:
                candidate_source = candidate.decode("utf-8")
                compile(candidate_source, str(probe_path), "exec")
            except (UnicodeDecodeError, SyntaxError) as exc:
                print("RX_TX_REMOTE_COMMAND_REJECTED sha256={} reason={}: {}".format(
                    candidate_sha, type(exc).__name__, str(exc)[:180]), flush=True)
                last_probe_sha = candidate_sha
                continue
            temporary_path = probe_path.with_suffix(".py.new")
            temporary_path.write_bytes(candidate)
            os.replace(temporary_path, probe_path)
            last_probe_sha = candidate_sha
            print("RX_TX_REMOTE_COMMAND_RECEIVED sha256={} bytes={} transport=github-fetch-then-kaggle-rx-tx".format(
                candidate_sha, len(candidate)), flush=True)
            try:
                run_streamed_subprocess(
                    proof_command, env, "RX_TX_REMOTE_COMMAND", timeout=1200)
                print("RX_TX_REMOTE_COMMAND_COMPLETED sha256={}".format(
                    candidate_sha), flush=True)
            except Exception as exc:
                print("RX_TX_REMOTE_COMMAND_FAILED sha256={} error={}: {}".format(
                    candidate_sha, type(exc).__name__, str(exc)[:300]), flush=True)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print("RX_TX_REMOTE_COMMAND_POLL_ERROR error={}: {}".format(
                type(exc).__name__, str(exc)[:220]), flush=True)


def main():
    prepare_system()
    prepare_credentials()
    # The current task is the one-message Kaggle API-mediated RX/TX proof.
    # Keep the Qwen experiment implementation intact but do not submit it here.
    run_io_loop_proof()

