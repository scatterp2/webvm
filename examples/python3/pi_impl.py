
#!/usr/bin/env python3

import os
import sys
import json
import time
import shutil
import subprocess
import selectors
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

def run_io_loop_proof():
    """Fetch and run the isolated API-mediated RX/TX proof instead of the Qwen experiment."""
    from urllib.error import HTTPError, URLError

    base = "https://raw.githubusercontent.com/scatterp2/webvm/d37c394128367d8fd3a3ed96e0da69725ac25e80/examples/python3/kaggle_io_loop/"
    target_dir = Path("/home/user/kaggle-io-loop")
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("controller.py", "agent.py"):
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
    command = [
        sys.executable, str(target_dir / "controller.py"),
        "--dataset-id", USERNAME + "/webvm-io-loop-rx",
        "--kernel-id", USERNAME + "/webvm-io-loop-proof",
        "--work-dir", "/home/user/kaggle-io-loop-run",
        "--timeout-seconds", "900",
        "--poll-seconds", "10",
    ]
    print("IO_LOOP_CONTROLLER_START dataset_id={} kernel_id={}".format(
        USERNAME + "/webvm-io-loop-rx", USERNAME + "/webvm-io-loop-proof"), flush=True)
    # Stream controller diagnostics as they happen. Capturing with subprocess.run()
    # hides API_REQUEST_START / timeout events until the whole 20-minute process exits.
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=env)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            if time.monotonic() - started > 1200:
                process.kill()
                raise subprocess.TimeoutExpired(command, 1200)
            events = selector.select(timeout=1)
            for key, _ in events:
                line = key.fileobj.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    selector.unregister(key.fileobj)
            returncode = process.poll()
            if returncode is not None and not selector.get_map():
                break
        print("IO_LOOP_CONTROLLER_EXIT returncode={}".format(returncode), flush=True)
        if returncode != 0:
            raise RuntimeError("Kaggle API-mediated IO loop proof failed with exit code {}".format(returncode))
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()


def main():
    prepare_system()
    prepare_credentials()
    # The current task is the one-message Kaggle API-mediated RX/TX proof.
    # Keep the Qwen experiment implementation intact but do not submit it here.
    run_io_loop_proof()

