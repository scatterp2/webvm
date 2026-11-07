#!/usr/bin/env python3
"""Replicate the Qwen weight-space interpretation example on Kaggle.

The notebook uses the four self-contained prompts from RikiyaT/lm_understand-logs,
runs a quantized Qwen3.8-27B model without tools, and publishes progress/results
to pste.us so the WebVM run can be inspected remotely.
"""
import os
import sys
import time
import json
import queue
import threading
import traceback
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

FEEDBACK_TAG = "WVMQWENWS_6D91C42F"
FEEDBACK_URL = "https://pste.us/api/v1/pastes"
PROMPT_ROOT = "https://raw.githubusercontent.com/RikiyaT/lm_understand-logs/main/exp1_which_game/runs"
MODEL_ID = "Qwen/Qwen3.8-27B"
GAMES = ["pacman", "pong", "breakout", "freeway"]
MAX_CONTEXT_TOKENS = 65536
MAX_NEW_TOKENS = 49152
LOG_PATH = Path("/kaggle/working/weight_space_experiment.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
FEEDBACK_AVAILABLE = True


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message):
    line = f"[{timestamp()}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def publish(stage, details=""):
    """Publish diagnostics with up to ten logged attempts; never include credentials."""
    global FEEDBACK_AVAILABLE
    if not FEEDBACK_AVAILABLE:
        return
    from urllib.error import HTTPError, URLError
    content = (
        f"{FEEDBACK_TAG}\n"
        f"source=KAGGLE\n"
        f"time={timestamp()}\n"
        f"stage={stage}\n"
        f"{details}\n"
    )
    payload = json.dumps({
        "content": content[:90000],
        "language": "text",
        "expires_in": "1m",
    }).encode("utf-8")
    last_error = None
    for attempt in range(1, 11):
        try:
            request = Request(
                FEEDBACK_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "webvm-qwen-weight-space-feedback/1.0",
                },
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
            paste_url = result.get("url") or result.get("paste_url")
            paste_id = result.get("id") or result.get("key")
            log(f"FEEDBACK_UPLOAD_ATTEMPT stage={stage} attempt={attempt}/10 result=ok")
            log(f"FEEDBACK_UPLOAD_OK stage={stage} url={paste_url or paste_id or 'created'}")
            return
        except Exception as exc:
            last_error = exc
            log(f"FEEDBACK_UPLOAD_ATTEMPT stage={stage} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
            if attempt < 10:
                time.sleep(min(attempt, 5))
    log(f"FEEDBACK_UPLOAD_EXHAUSTED stage={stage} attempts=10 last_error={type(last_error).__name__ if last_error else 'unknown'}")
    FEEDBACK_AVAILABLE = False
    log("FEEDBACK_DISABLED reason=10 consecutive upload failures; continuing with Kaggle log/output collection")



def run_diagnostic_command(command, timeout=10):
    """Run a harmless GPU diagnostic and capture output in the Kaggle log."""
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False,
        )
        output = (result.stdout or "").strip()
        log(f"DIAGNOSTIC_COMMAND command={command!r} exit={result.returncode}")
        if output:
            for line in output.splitlines()[-80:]:
                log("DIAGNOSTIC_OUTPUT " + line)
        return result.returncode, output
    except FileNotFoundError:
        log(f"DIAGNOSTIC_COMMAND_NOT_FOUND command={command!r}")
        return 127, ""
    except Exception as exc:
        log(f"DIAGNOSTIC_COMMAND_ERROR command={command!r} error={type(exc).__name__}: {exc}")
        return 1, ""


def diagnose_network():
    """Retry DNS, TCP connect, and HTTPS checks up to ten times, logging every result."""
    import socket
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    attempts = 10
    hosts = ("pypi.org", "huggingface.co", "pste.us", "raw.githubusercontent.com")
    log("NETWORK_PREFLIGHT_BEGIN attempts=10")

    for host in hosts:
        resolved = []
        for attempt in range(1, attempts + 1):
            try:
                resolved = sorted({item[4][0] for item in socket.getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM
                )})
                log(f"NETWORK_DNS_ATTEMPT host={host} attempt={attempt}/10 result=ok addresses={','.join(resolved[:6])}")
                break
            except Exception as exc:
                log(f"NETWORK_DNS_ATTEMPT host={host} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                if attempt < attempts:
                    time.sleep(min(attempt, 5))
        if not resolved:
            log(f"NETWORK_DNS_EXHAUSTED host={host} attempts=10")

        tcp_ok = False
        for attempt in range(1, attempts + 1):
            try:
                with socket.create_connection((host, 443), timeout=5):
                    tcp_ok = True
                log(f"NETWORK_TCP_ATTEMPT host={host} port=443 attempt={attempt}/10 result=ok")
                break
            except Exception as exc:
                log(f"NETWORK_TCP_ATTEMPT host={host} port=443 attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                if attempt < attempts:
                    time.sleep(min(attempt, 5))
        if not tcp_ok:
            log(f"NETWORK_TCP_EXHAUSTED host={host} port=443 attempts=10")

    urls = (
        "https://pypi.org/simple/transformers/",
        "https://huggingface.co/api/models/Qwen/Qwen3.8-27B",
        "https://pste.us/",
        "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/kaggle_runtime.py",
    )
    for url in urls:
        host = url.split("/")[2]
        success = False
        for attempt in range(1, attempts + 1):
            try:
                request = Request(url, headers={"User-Agent": "webvm-kaggle-network-preflight/1.0"}, method="HEAD")
                with urlopen(request, timeout=8) as response:
                    log(f"NETWORK_HTTPS_ATTEMPT host={host} attempt={attempt}/10 result=ok status={response.status}")
                success = True
                break
            except HTTPError as exc:
                # An HTTP response, including 4xx/5xx, proves the endpoint was reached.
                log(f"NETWORK_HTTPS_ATTEMPT host={host} attempt={attempt}/10 result=http_response status={exc.code}")
                success = True
                break
            except Exception as exc:
                log(f"NETWORK_HTTPS_ATTEMPT host={host} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                if attempt < attempts:
                    time.sleep(min(attempt, 5))
        if not success:
            log(f"NETWORK_HTTPS_EXHAUSTED host={host} attempts=10")
    log("NETWORK_PREFLIGHT_END")



def diagnose_gpu():
    """Capture enough evidence to separate missing GPU allocation from CPU-only torch."""
    log("GPU_PREFLIGHT_BEGIN")
    for name in (
        "KAGGLE_KERNEL_RUN_TYPE", "KAGGLE_KERNEL_INTEGRATION",
        "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
        "CUDA_HOME", "LD_LIBRARY_PATH",
    ):
        log(f"GPU_ENV {name}={os.environ.get(name, '<unset>')}")
    log(f"GPU_DEVICE_NODES /dev/nvidia*={','.join(str(p) for p in sorted(Path('/dev').glob('nvidia*'))) or '<none>'}")
    log(f"GPU_PROC_DRIVER_EXISTS={Path('/proc/driver/nvidia').exists()}")
    smi_code, smi_output = run_diagnostic_command(["nvidia-smi", "-q"], timeout=15)
    run_diagnostic_command(["sh", "-lc", "ls -l /dev/nvidia* 2>&1; cat /proc/driver/nvidia/version 2>&1"], timeout=10)
    try:
        import torch
        log(f"TORCH_DIAGNOSTIC version={torch.__version__}")
        log(f"TORCH_COMPILED_CUDA={getattr(torch.version, 'cuda', None)}")
        log(f"TORCH_CUDA_AVAILABLE={torch.cuda.is_available()}")
        log(f"TORCH_CUDA_DEVICE_COUNT={torch.cuda.device_count()}")
        log("TORCH_BUILD_CONFIG_BEGIN")
        for line in torch.__config__.show().splitlines()[-40:]:
            log("TORCH_BUILD_CONFIG " + line)
        log("TORCH_BUILD_CONFIG_END")
        for index in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(index)
            log(f"GPU_DEVICE index={index} name={props.name} total_memory={props.total_memory}")
    except Exception as exc:
        log(f"TORCH_DIAGNOSTIC_FAILED {type(exc).__name__}: {exc}")
        raise
    if not torch.cuda.is_available():
        if smi_code == 0 and smi_output:
            reason = (
                "NVIDIA GPU is visible to nvidia-smi, but PyTorch reports CUDA unavailable. "
                "This strongly indicates a CPU-only or CUDA-incompatible PyTorch install."
            )
        elif Path("/dev/nvidia0").exists() or Path("/proc/driver/nvidia").exists():
            reason = (
                "NVIDIA device/driver paths exist, but nvidia-smi or PyTorch cannot use CUDA. "
                "Check driver/runtime compatibility and the installed PyTorch build."
            )
        else:
            reason = (
                "No usable NVIDIA device/driver is visible inside the Kaggle kernel. "
                "Check whether Kaggle actually allocated the requested GPU; kernel metadata alone is not proof."
            )
        log("GPU_PREFLIGHT_CLASSIFICATION " + reason)
        raise RuntimeError("CUDA preflight failed: " + reason)
    log("GPU_PREFLIGHT_PASSED")
    return torch


def install_dependencies():
    log("DEPENDENCIES_INSTALLING transformers accelerate bitsandbytes attempts=10")
    publish("DEPENDENCIES_INSTALLING", "Installing transformers, accelerate, bitsandbytes; max_attempts=10")
    command = [
        sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
        "--retries", "10", "--timeout", "30",
        "transformers", "accelerate", "bitsandbytes", "safetensors",
    ]
    last_output = ""
    error = "unknown"
    for attempt in range(1, 11):
        try:
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=900, check=False,
            )
            last_output = result.stdout or ""
            log(f"DEPENDENCIES_INSTALL_ATTEMPT attempt={attempt}/10 exit={result.returncode}")
            if last_output:
                log(last_output[-12000:])
            if result.returncode == 0:
                publish("DEPENDENCIES_INSTALLED", f"attempt={attempt}; " + (last_output[-4000:] if last_output else "pip succeeded"))
                return
            error = f"pip exited {result.returncode}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log(f"DEPENDENCIES_INSTALL_ATTEMPT attempt={attempt}/10 error={error}")
        publish("DEPENDENCIES_INSTALL_RETRY", f"attempt={attempt}/10; error={error}; output={last_output[-3000:]}")
        if attempt < 10:
            time.sleep(min(attempt, 5))
    publish("DEPENDENCIES_FAILED", f"attempts=10; last_error={error}; output={last_output[-12000:]}")
    raise RuntimeError(f"pip dependency installation failed after 10 attempts: {error}")


def fetch_prompt(game):
    """Try each GitHub prompt endpoint repeatedly, logging every network attempt."""
    import base64
    import requests

    api_url = (
        "https://api.github.com/repos/RikiyaT/lm_understand-logs/contents/"
        f"exp1_which_game/runs/{game}/prompt.txt?ref=main"
    )
    raw_url = f"{PROMPT_ROOT}/{game}/prompt.txt"
    last_error = None
    log(f"PROMPT_FETCH_START game={game} attempts_per_endpoint=10")
    publish("PROMPT_FETCH_START", f"game={game}; attempts_per_endpoint=10")

    # Give both endpoints ten independent attempts: API first, then raw fallback.
    for source, url in (("github_contents_api", api_url), ("raw_github_fallback", raw_url)):
        for attempt in range(1, 11):
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "webvm-qwen-runtime/1.0",
                    },
                )
                response.raise_for_status()
                if source == "github_contents_api":
                    payload = response.json()
                    if payload.get("encoding") != "base64" or not payload.get("content"):
                        raise ValueError("GitHub Contents API did not return base64 file content")
                    prompt = base64.b64decode(payload["content"]).decode("utf-8")
                else:
                    prompt = response.text
                log(f"PROMPT_FETCH_ATTEMPT game={game} source={source} attempt={attempt}/10 result=ok characters={len(prompt)}")
                publish("PROMPT_FETCH_OK", f"game={game}; source={source}; attempt={attempt}/10; characters={len(prompt)}")
                return prompt
            except (requests.RequestException, ValueError, KeyError, UnicodeDecodeError, AttributeError) as exc:
                last_error = exc
                log(f"PROMPT_FETCH_ATTEMPT game={game} source={source} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                publish("PROMPT_FETCH_RETRY", f"game={game}; source={source}; attempt={attempt}/10; error={type(exc).__name__}: {exc}")
                if attempt < 10:
                    time.sleep(min(attempt, 5))
        log(f"PROMPT_FETCH_EXHAUSTED game={game} source={source} attempts=10")
        publish("PROMPT_FETCH_EXHAUSTED", f"game={game}; source={source}; attempts=10; last_error={last_error}")

    raise RuntimeError(f"Could not fetch prompt for {game} after 10 GitHub API attempts and 10 raw endpoint attempts: {last_error}")


def main():
    run_started = timestamp()
    log(f"RUN_STARTED tag={FEEDBACK_TAG} python={platform.python_version()} platform={platform.platform()}")
    publish("RUN_STARTED", f"model={MODEL_ID}; games={','.join(GAMES)}; python={platform.python_version()}")
    try:
        diagnose_network()
        torch = diagnose_gpu()
        publish("GPU_CHECK", f"torch={torch.__version__}; torch_cuda_build={getattr(torch.version, 'cuda', None)}; cuda={torch.cuda.is_available()}; gpu_count={torch.cuda.device_count()}")
        install_dependencies()
        from transformers import (
            AutoModelForImageTextToText,
            AutoTokenizer,
            BitsAndBytesConfig,
            TextIteratorStreamer,
        )

        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )
        gpu_count = torch.cuda.device_count()
        max_memory = {index: "14GiB" for index in range(gpu_count)}
        max_memory["cpu"] = "40GiB"

        log(f"MODEL_LOADING model={MODEL_ID} quantization=4bit context_target={MAX_CONTEXT_TOKENS}")
        publish("MODEL_LOADING", f"model={MODEL_ID}; quantization=4bit NF4; max_context={MAX_CONTEXT_TOKENS}; max_memory={max_memory}")
        tokenizer = None
        last_download_error = None
        for attempt in range(1, 11):
            try:
                log(f"TOKENIZER_DOWNLOAD_ATTEMPT model={MODEL_ID} attempt={attempt}/10")
                tokenizer = AutoTokenizer.from_pretrained(
                    MODEL_ID, trust_remote_code=True, force_download=(attempt > 1)
                )
                log(f"TOKENIZER_DOWNLOAD_OK model={MODEL_ID} attempt={attempt}/10")
                break
            except Exception as exc:
                last_download_error = exc
                log(f"TOKENIZER_DOWNLOAD_ATTEMPT model={MODEL_ID} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                publish("TOKENIZER_DOWNLOAD_RETRY", f"model={MODEL_ID}; attempt={attempt}/10; error={type(exc).__name__}: {exc}")
                if attempt < 10:
                    time.sleep(min(attempt, 5))
        if tokenizer is None:
            raise RuntimeError(f"Tokenizer download failed after 10 attempts: {last_download_error}")

        model = None
        last_download_error = None
        for attempt in range(1, 11):
            try:
                log(f"MODEL_DOWNLOAD_ATTEMPT model={MODEL_ID} attempt={attempt}/10")
                model = AutoModelForImageTextToText.from_pretrained(
                    MODEL_ID,
                    trust_remote_code=True,
                    device_map="auto",
                    max_memory=max_memory,
                    quantization_config=quantization,
                    torch_dtype=torch.float16,
                    low_cpu_mem_usage=True,
                    force_download=(attempt > 1),
                )
                log(f"MODEL_DOWNLOAD_OK model={MODEL_ID} attempt={attempt}/10")
                break
            except Exception as exc:
                last_download_error = exc
                log(f"MODEL_DOWNLOAD_ATTEMPT model={MODEL_ID} attempt={attempt}/10 result=failed error={type(exc).__name__}: {exc}")
                publish("MODEL_DOWNLOAD_RETRY", f"model={MODEL_ID}; attempt={attempt}/10; error={type(exc).__name__}: {exc}")
                if attempt < 10:
                    time.sleep(min(attempt, 5))
        if model is None:
            raise RuntimeError(f"Model download/load failed after 10 attempts: {last_download_error}")
        model.eval()
        model_limit = getattr(model.config, "max_position_embeddings", MAX_CONTEXT_TOKENS)
        context_limit = min(MAX_CONTEXT_TOKENS, int(model_limit))
        log(f"MODEL_LOADED model_limit={model_limit} context_limit={context_limit} device_map={model.hf_device_map}")
        publish("MODEL_LOADED", f"model={MODEL_ID}; model_limit={model_limit}; context_limit={context_limit}; device_map={model.hf_device_map}")

        results = []
        for game in GAMES:
            prompt = fetch_prompt(game)
            messages = [{"role": "user", "content": prompt}]
            encoded = tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_tensors="pt",
                return_dict=True,
            )
            input_tokens = int(encoded["input_ids"].shape[-1])
            log(f"PROMPT_READY game={game} characters={len(prompt)} input_tokens={input_tokens}")
            publish("PROMPT_READY", f"game={game}; characters={len(prompt)}; input_tokens={input_tokens}")
            if input_tokens + 256 >= context_limit:
                detail = f"game={game}; input_tokens={input_tokens}; context_limit={context_limit}; prompt not truncated"
                log(f"PROMPT_TOO_LONG {detail}")
                publish("PROMPT_TOO_LONG", detail)
                results.append({"game": game, "status": "prompt_too_long", "input_tokens": input_tokens})
                continue

            inputs = {key: value.to(model.device) for key, value in encoded.items()}
            output_budget = min(MAX_NEW_TOKENS, context_limit - input_tokens)
            streamer = TextIteratorStreamer(
                tokenizer, skip_prompt=True, skip_special_tokens=False, timeout=10
            )
            generation_kwargs = {
                **inputs,
                "streamer": streamer,
                "max_new_tokens": output_budget,
                "do_sample": False,
                "use_cache": True,
            }
            generation_error = []

            def generate_worker():
                try:
                    model.generate(**generation_kwargs)
                except Exception as exc:
                    generation_error.append(f"{type(exc).__name__}: {exc}")
                    try:
                        streamer.end()
                    except Exception:
                        pass

            log(f"INFERENCE_STARTED game={game} max_new_tokens={output_budget}")
            publish("INFERENCE_STARTED", f"game={game}; input_tokens={input_tokens}; max_new_tokens={output_budget}")
            worker = threading.Thread(target=generate_worker, daemon=True)
            worker.start()
            pieces = []
            last_heartbeat = time.monotonic()
            while True:
                try:
                    piece = next(streamer)
                except queue.Empty:
                    if not worker.is_alive():
                        break
                    if time.monotonic() - last_heartbeat >= 120:
                        detail = f"game={game}; generated_characters={sum(map(len, pieces))}; inference still running"
                        log(f"INFERENCE_PROGRESS {detail}")
                        publish("INFERENCE_PROGRESS", detail)
                        last_heartbeat = time.monotonic()
                    continue
                except StopIteration:
                    break
                if piece is streamer.stop_signal:
                    break
                pieces.append(piece)
                if time.monotonic() - last_heartbeat >= 120:
                    detail = f"game={game}; generated_characters={sum(map(len, pieces))}; inference still running"
                    log(f"INFERENCE_PROGRESS {detail}")
                    publish("INFERENCE_PROGRESS", detail)
                    last_heartbeat = time.monotonic()
            worker.join()
            generated_text = "".join(pieces)
            if generation_error:
                detail = f"game={game}; error={generation_error[0]}; partial_characters={len(generated_text)}"
                log(f"INFERENCE_FAILED {detail}")
                publish("INFERENCE_FAILED", detail + "\npartial_output:\n" + generated_text[-12000:])
                results.append({"game": game, "status": "inference_failed", "error": generation_error[0]})
                continue

            status = "answer_complete" if generated_text.strip() else "empty_answer"
            log(f"INFERENCE_FINISHED game={game} generated_characters={len(generated_text)}")
            publish("ANSWER_RESULT", f"game={game}; status={status}; input_tokens={input_tokens}; generated_characters={len(generated_text)}\n\n{generated_text[-65000:]}")
            results.append({
                "game": game,
                "status": status,
                "input_tokens": input_tokens,
                "generated_characters": len(generated_text),
                "answer": generated_text,
            })
            del inputs, encoded
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        summary = {
            "run_started": run_started,
            "run_finished": timestamp(),
            "model": MODEL_ID,
            "quantization": "bitsandbytes NF4 4-bit",
            "context_limit": context_limit,
            "results": [
                {key: value for key, value in item.items() if key != "answer"}
                for item in results
            ],
        }
        summary_path = Path("/kaggle/working/weight_space_summary.json")
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        log("RUN_FINISHED " + json.dumps(summary, sort_keys=True))
        publish("RUN_FINISHED", json.dumps(summary, indent=2))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-12000:]}"
        log(f"RUN_FAILED {detail}")
        publish("RUN_FAILED", detail)
        raise


if __name__ == "__main__":
    main()
