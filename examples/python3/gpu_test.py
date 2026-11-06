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


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message):
    line = f"[{timestamp()}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def publish(stage, details=""):
    """Publish a public diagnostic paste. Never include credentials or env dumps."""
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
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        paste_url = result.get("url") or result.get("paste_url")
        paste_id = result.get("id") or result.get("key")
        log(f"FEEDBACK_UPLOAD_OK stage={stage} url={paste_url or paste_id or 'created'}")
    except Exception as exc:
        log(f"FEEDBACK_UPLOAD_FAILED stage={stage} error={type(exc).__name__}: {exc}")


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
        import torch
        log(f"TORCH version={torch.__version__} cuda_available={torch.cuda.is_available()} gpu_count={torch.cuda.device_count()}")
        for index in range(torch.cuda.device_count()):
            log(f"GPU index={index} name={torch.cuda.get_device_name(index)}")
        publish("GPU_CHECK", f"torch={torch.__version__}; cuda={torch.cuda.is_available()}; gpu_count={torch.cuda.device_count()}")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; Kaggle GPU acceleration is required.")

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
