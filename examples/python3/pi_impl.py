
#!/usr/bin/env python3

import os
import sys
import json
import time
import shutil
import subprocess
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
    for attempt in range(3):
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
            print("RUNTIME_SOURCE_FETCHED source=main characters={}".format(len(source)), flush=True)
            return source
        except (HTTPError, URLError, TimeoutError, OSError, SyntaxError) as exc:
            last_error = exc
            print("RUNTIME_SOURCE_FETCH_RETRY attempt={} error={}: {}".format(
                attempt + 1, type(exc).__name__, exc
            ), flush=True)
            if attempt < 2:
                time.sleep(2 ** attempt)

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
    """Embed live runtime code and prompts so Kaggle does not fetch source files during execution."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    runtime_source = fetch_runtime_source()
    prompts = {}
    for game in GAMES:
        prompt_url = "{}/{}/prompt.txt".format(PROMPT_ROOT, game)
        last_error = None
        for attempt in range(3):
            try:
                request = Request(
                    prompt_url,
                    headers={
                        "Cache-Control": "no-cache",
                        "User-Agent": "webvm-qwen-experiment-submitter/1.0",
                    },
                )
                with urlopen(request, timeout=60) as response:
                    prompts[game] = response.read().decode("utf-8")
                print("PROMPT_EMBEDDED game={} characters={}".format(game, len(prompts[game])), flush=True)
                break
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                print("PROMPT_FETCH_RETRY game={} attempt={} error={}: {}".format(
                    game, attempt + 1, type(exc).__name__, exc
                ), flush=True)
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if game not in prompts:
            raise RuntimeError("Could not fetch source prompt for {} after 3 attempts: {}".format(game, last_error))

    root_line = 'PROMPT_ROOT = "' + PROMPT_ROOT + '"'
    if root_line not in runtime_source:
        raise RuntimeError("Could not locate PROMPT_ROOT in kaggle_runtime.py; refusing to submit a partial experiment.")
    runtime_source = runtime_source.replace(
        root_line,
        "PROMPTS = " + json.dumps(prompts, ensure_ascii=False, separators=(",", ":")),
        1,
    )

    old_prompt_fetch = '''            prompt_url = f"{PROMPT_ROOT}/{game}/prompt.txt"
            log(f"PROMPT_FETCH_START game={game} url={prompt_url}")
            publish("PROMPT_FETCH_START", f"game={game}; url={prompt_url}")
            import requests
            response = requests.get(prompt_url, timeout=120)
            response.raise_for_status()
            prompt = response.text'''
    new_prompt_fetch = '''            prompt = PROMPTS[game]
            log(f"PROMPT_SOURCE_EMBEDDED game={game} characters={len(prompt)}")
            publish("PROMPT_SOURCE_EMBEDDED", f"game={game}; characters={len(prompt)}; source=embedded_at_submission")'''
    if old_prompt_fetch not in runtime_source:
        raise RuntimeError("Could not locate prompt-fetch block in kaggle_runtime.py; refusing to submit a partial experiment.")
    runtime_source = runtime_source.replace(old_prompt_fetch, new_prompt_fetch, 1)
    compile(runtime_source, str(RUNTIME_FILE) + "[embedded]", "exec")
    print("RUNTIME_EMBEDDED source={} characters={} prompts={} syntax=ok".format(
        "main-or-local-fallback", len(runtime_source), ",".join(GAMES)
    ), flush=True)
    return runtime_source


def write_notebook():
    import base64
    import gzip
    import textwrap

    print("booting....", flush=True)
    runtime_source = build_embedded_runtime()
    compressed_runtime = gzip.compress(runtime_source.encode("utf-8"), compresslevel=9)
    encoded_runtime = base64.b64encode(compressed_runtime).decode("ascii")
    payload_lines = "\n".join(
        "    " + json.dumps(chunk)
        for chunk in textwrap.wrap(encoded_runtime, 4000)
    )
    notebook_source = (
        "import base64 as _base64, gzip as _gzip\n"
        "_runtime_payload = (\n" + payload_lines + "\n)\n"
        "_runtime_source = _gzip.decompress(_base64.b64decode(_runtime_payload)).decode('utf-8')\n"
        "exec(compile(_runtime_source, 'kaggle_runtime.py', 'exec'), globals(), globals())\n"
    )
    print(
        "RUNTIME_COMPRESSED original_chars={} gzip_bytes={} base64_chars={}".format(
            len(runtime_source), len(compressed_runtime), len(encoded_runtime)
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
                    "Runs the four source prompts against Qwen/Qwen3.8-27B. Runtime code and prompts are embedded at submission time to avoid raw-GitHub DNS dependencies in Kaggle."
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
                "source": notebook_source.splitlines(keepends=True),
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
    """Ensure the installed Kaggle CLI supports an explicit accelerator request."""
    run([str(KAGGLE_BIN), "--version"], check=False, env=env)
    help_result = run([
        str(KAGGLE_BIN), "kernels", "push", "--help"
    ], check=False, env=env)
    if "--accelerator" in (help_result.stdout or ""):
        print("KAGGLE_CLI_ACCELERATOR_FLAG_AVAILABLE", flush=True)
        return

    print("KAGGLE_CLI_ACCELERATOR_FLAG_MISSING; upgrading Kaggle CLI before submission.", flush=True)
    run([
        str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "kaggle"
    ], check=True, env=env)
    help_result = run([
        str(KAGGLE_BIN), "kernels", "push", "--help"
    ], check=False, env=env)
    if "--accelerator" not in (help_result.stdout or ""):
        raise RuntimeError("Installed Kaggle CLI still does not support --accelerator after upgrade; refusing a CPU-ambiguous submission.")
    print("KAGGLE_CLI_ACCELERATOR_FLAG_AVAILABLE_AFTER_UPGRADE", flush=True)


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
    transient_tls_failure = False
    push_command = [
        str(KAGGLE_BIN), "kernels", "push",
        "-p", str(NOTEBOOK_DIR), "--accelerator", "NvidiaTeslaT4"
    ]
    for push_attempt in range(1, 4):
        push = run(push_command, check=False, env=env)
        if push.returncode == 0:
            break

        push_output = (push.stdout or "")
        transient_tls_failure = (
            "SSLV3_ALERT_BAD_RECORD_MAC" in push_output
            or "ssl alert bad record mac" in push_output.lower()
            or "TLSV1_ALERT_BAD_RECORD_MAC" in push_output
        )
        if not transient_tls_failure:
            raise RuntimeError(
                "Kaggle notebook submission failed with a non-transient error."
            )
        if push_attempt < 3:
            delay = 5 * push_attempt
            print(
                "KAGGLE_PUSH_TRANSIENT_TLS_FAILURE attempt={} retry_in={}s".format(
                    push_attempt, delay
                ),
                flush=True,
            )
            time.sleep(delay)

    if push is not None and push.returncode != 0 and transient_tls_failure:
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
        print(
            "KAGGLE_PUSH_TLS12_FALLBACK config={} action=retry".format(tls_config),
            flush=True,
        )
        push = run(push_command, check=False, env=tls12_env)

    if push is not None and push.returncode != 0 and transient_tls_failure:
        # The WebVM network path consistently corrupts TLS to api.kaggle.com.
        # Test the same public API through Kaggle's www host before giving up.
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
        alt_status_output = (alt_status.stdout or "")
        print(
            "KAGGLE_ALT_HOST_STATUS_CHECK returncode={} patch_applied={} output={}".format(
                alt_status.returncode,
                "WEBVM_KAGGLE_ALT_HOST_PATCH_APPLIED" in alt_status_output,
                " ".join(alt_status_output.split())[:240],
            ),
            flush=True,
        )
        if (
            alt_status.returncode == 0
            and "WEBVM_KAGGLE_ALT_HOST_PATCH_APPLIED" in alt_status_output
        ):
            print(
                "KAGGLE_PUSH_ALT_HOST_FALLBACK host=www.kaggle.com/api action=retry",
                flush=True,
            )
            push = run(push_command, check=False, env=alt_env)

    if push is None or push.returncode != 0:
        raise RuntimeError("Kaggle notebook submission failed after TLS retries and alternate-host fallback.")

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

def main():
    prepare_system()
    prepare_credentials()
    # prepare_python()  # Environment and Kaggle CLI are installed in the Docker image.
    write_notebook()
    deploy()

