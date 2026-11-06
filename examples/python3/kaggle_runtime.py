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
    """Publish a public diagnostic paste. Never include credentials or env dumps."""
    global FEEDBACK_AVAILABLE
    if not FEEDBACK_AVAILABLE:
        return
    content = (
        f"{FEEDBACK_TAG}\n"
        f"source=KAGGLE\n"
        f"time={timestamp()}\n"
        f"stage={stage}\n"
        f"{details}\n"
    )
    try:
        payload = json.dumps({
            "content": content[:90000],
            "language": "text",
            "expires_in": "1m",
        }).encode("utf-8")
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
        log(f"FEEDBACK_UPLOAD_OK stage={stage} url={paste_url or paste_id or 'created'}")
    except Exception as exc:
        log(f"FEEDBACK_UPLOAD_FAILED stage={stage} error={type(exc).__name__}: {exc}")
        message = str(exc).lower()
        if "temporary failure in name resolution" in message or "name or service not known" in message:
            FEEDBACK_AVAILABLE = False
            log("FEEDBACK_DISABLED reason=dns_failure; continuing with Kaggle log/output collection")


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
    """Log whether Kaggle can resolve and reach the endpoints the run depends on."""
    import socket
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    log("NETWORK_PREFLIGHT_BEGIN")
    for host in ("pypi.org", "huggingface.co", "pste.us", "raw.githubusercontent.com"):
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
            log(f"NETWORK_DNS_OK host={host} addresses={','.join(addresses[:6])}")
        except Exception as exc:
            log(f"NETWORK_DNS_FAILED host={host} error={type(exc).__name__}: {exc}")

    for url in (
        "https://pypi.org/simple/transformers/",
        "https://huggingface.co/api/models/Qwen/Qwen3.8-27B",
        "https://pste.us/",
        "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/kaggle_runtime.py",
    ):
        host = url.split("/")[2]
        try:
            request = Request(url, headers={"User-Agent": "webvm-kaggle-network-preflight/1.0"}, method="HEAD")
            with urlopen(request, timeout=8) as response:
                log(f"NETWORK_HTTP_OK host={host} status={response.status}")
        except HTTPError as exc:
            # A reachable endpoint returning an HTTP error still proves DNS/TCP/TLS worked.
            log(f"NETWORK_HTTP_RESPONSE host={host} status={exc.code}")
        except Exception as exc:
            log(f"NETWORK_HTTP_FAILED host={host} error={type(exc).__name__}: {exc}")
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
    log("DEPENDENCIES_INSTALLING transformers accelerate bitsandbytes")
    publish("DEPENDENCIES_INSTALLING", "Installing transformers, accelerate, bitsandbytes")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
         "transformers", "accelerate", "bitsandbytes", "safetensors"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    log(result.stdout[-12000:] if result.stdout else "(pip produced no output)")
    if result.returncode:
        publish("DEPENDENCIES_FAILED", result.stdout[-12000:])
        raise RuntimeError(f"pip dependency installation exited {result.returncode}")
    publish("DEPENDENCIES_INSTALLED", result.stdout[-4000:] if result.stdout else "pip succeeded")


def main():
    run_started = timestamp()
    log(f"RUN_STARTED tag={FEEDBACK_TAG} python={platform.python_version()} platform={platform.platform()}")
    publish("RUN_STARTED", f"model={MODEL_ID}; games={','.join(GAMES)}; python={platform.python_version()}")
    try:
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
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)
        model = AutoModelForImageTextToText.from_pretrained(
            MODEL_ID,
            trust_remote_code=True,
            device_map="auto",
            max_memory=max_memory,
            quantization_config=quantization,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
        )
        model.eval()
        model_limit = getattr(model.config, "max_position_embeddings", MAX_CONTEXT_TOKENS)
        context_limit = min(MAX_CONTEXT_TOKENS, int(model_limit))
        log(f"MODEL_LOADED model_limit={model_limit} context_limit={context_limit} device_map={model.hf_device_map}")
        publish("MODEL_LOADED", f"model={MODEL_ID}; model_limit={model_limit}; context_limit={context_limit}; device_map={model.hf_device_map}")

        results = []
        for game in GAMES:
            prompt_url = f"{PROMPT_ROOT}/{game}/prompt.txt"
            log(f"PROMPT_FETCH_START game={game} url={prompt_url}")
            publish("PROMPT_FETCH_START", f"game={game}; url={prompt_url}")
            import requests
            response = requests.get(prompt_url, timeout=120)
            response.raise_for_status()
            prompt = response.text
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
