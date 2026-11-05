
#!/usr/bin/env python3

import os
import sys
import json
import time
import subprocess
from pathlib import Path

# ============================================================
# CONFIGURATION — credentials hardcoded as requested
# ============================================================

USERNAME = "simonday"
LEGACY_KEY = "fc837b9fcbf01dd51978287f9437068c"
API_TOKEN = "KGAT_2e52efc50d91a13e1b7a3eabce8ac33e"

HOME = Path("/root")
KAGGLE_DIR = HOME / ".kaggle"
TOKEN_FILE = KAGGLE_DIR / "access_token"
JSON_FILE = KAGGLE_DIR / "kaggle.json"

NOTEBOOK_DIR = Path("/home/user/my-notebook")
NOTEBOOK_FILE = NOTEBOOK_DIR / "gpu-test.ipynb"
METADATA_FILE = NOTEBOOK_DIR / "kernel-metadata.json"

KERNEL_SLUG = "automated-gpu-test"
KERNEL_ID = USERNAME + "/" + KERNEL_SLUG
NOTEBOOK_URL = "https://www.kaggle.com/code/" + KERNEL_ID


def run(command, cwd=None, check=True):
    print("\n$", " ".join(str(x) for x in command), flush=True)

    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )

    if result.stdout:
        print(result.stdout, end="", flush=True)

    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed with exit code {}".format(result.returncode)
        )

    return result


def create_credentials():
    print("[1/8] Preparing directories and credentials", flush=True)

    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    NOTEBOOK_DIR.mkdir(parents=True, exist_ok=True)

    TOKEN_FILE.write_text(API_TOKEN + "\n")

    JSON_FILE.write_text(json.dumps({
        "username": USERNAME,
        "key": LEGACY_KEY
    }, indent=2) + "\n")

    os.chmod(str(TOKEN_FILE), 0o600)
    os.chmod(str(JSON_FILE), 0o600)

    os.environ["HOME"] = str(HOME)
    os.environ["KAGGLE_API_TOKEN"] = API_TOKEN

    print("Credentials written and permissions secured.")


def install_kaggle():
    print("[2/8] Installing Kaggle CLI", flush=True)

    run([
        sys.executable, "-m", "pip", "install", "--upgrade", "kaggle"
    ])

    # Locate the installed executable.
    result = run([
        sys.executable, "-c",
        "import shutil; print(shutil.which('kaggle') or '')"
    ])

    executable = result.stdout.strip().splitlines()
    if not executable or not executable[-1]:
        raise RuntimeError("Could not locate the Kaggle executable.")

    return executable[-1]


def write_notebook():
    print("[3/8] Creating notebook and metadata", flush=True)

    metadata = {
        "id": KERNEL_ID,
        "title": "Automated GPU Test",
        "code_file": NOTEBOOK_FILE.name,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
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
                    "# Automated GPU Test\n",
                    "Checks the GPU available to this Kaggle notebook."
                ]
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "import os\n",
                    "import sys\n",
                    "import platform\n",
                    "print('Python:', platform.python_version())\n",
                    "print('CUDA_VISIBLE_DEVICES:', os.environ.get('CUDA_VISIBLE_DEVICES'))\n",
                    "try:\n",
                    "    import torch\n",
                    "    print('PyTorch:', torch.__version__)\n",
                    "    print('CUDA available:', torch.cuda.is_available())\n",
                    "    if torch.cuda.is_available():\n",
                    "        print('GPU:', torch.cuda.get_device_name(0))\n",
                    "        print('GPU VRAM (GB):', round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 2))\n",
                    "        x = torch.randn((2048, 2048), device='cuda')\n",
                    "        y = x @ x\n",
                    "        torch.cuda.synchronize()\n",
                    "        print('GPU matrix test passed:', tuple(y.shape))\n",
                    "    else:\n",
                    "        print('No CUDA GPU detected.')\n",
                    "except Exception as e:\n",
                    "    print('GPU test error:', repr(e))\n"
                ]
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
    print("Notebook:", NOTEBOOK_FILE)
    print("Metadata:", METADATA_FILE)


def main():
    create_credentials()

    kaggle = install_kaggle()

    print("[4/8] Checking Kaggle CLI version", flush=True)
    run([kaggle, "--version"])

    write_notebook()

    print("[5/8] Testing Kaggle authentication", flush=True)
    auth = run([kaggle, "kernels", "list", "--mine"], check=False)

    if auth.returncode != 0:
        raise RuntimeError(
            "Kaggle authentication failed. The hardcoded credentials "
            "may be invalid, expired, or unsupported by this CLI version."
        )

    print("[6/8] Submitting notebook to Kaggle", flush=True)
    push = run([
        kaggle, "kernels", "push", "-p", str(NOTEBOOK_DIR)
    ], check=False)

    if push.returncode != 0:
        raise RuntimeError(
            "Notebook submission failed. See the Kaggle CLI output above."
        )

    print("\nNotebook URL:", NOTEBOOK_URL, flush=True)
    print("[7/8] Monitoring execution", flush=True)

    # Status support varies between Kaggle CLI/API versions.
    # Do not treat a 403 status response as proof that the push failed.
    for attempt in range(20):
        print(
            "\nStatus check {}/20".format(attempt + 1),
            flush=True
        )

        status = run([
            kaggle, "kernels", "status", KERNEL_ID
        ], check=False)

        output = (status.stdout or "").lower()

        if status.returncode == 0:
            print("Status response received.", flush=True)

            if "complete" in output or "completed" in output:
                print("[8/8] Notebook completed.", flush=True)
                print("Open:", NOTEBOOK_URL)
                return

            if "error" in output or "failed" in output:
                print("Kaggle reports a possible execution failure.")
                print("Inspect the notebook page:", NOTEBOOK_URL)
                return
        else:
            print(
                "Status endpoint unavailable; submission was accepted "
                "if the push command reported success.",
                flush=True
            )

        time.sleep(30)

    print("[8/8] Automated status monitoring finished.")
    print("Inspect the notebook and execution logs here:")
    print(NOTEBOOK_URL)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "\nFATAL: {}: {}".format(type(exc).__name__, exc),
            file=sys.stderr,
            flush=True
        )
        sys.exit(1)
