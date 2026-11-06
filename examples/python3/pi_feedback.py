#!/usr/bin/env python3
"""Run pi.py while publishing redacted WebVM progress to pste.us."""
import json
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

FEEDBACK_TAG = "WVMQWENWS_6D91C42F"
FEEDBACK_URL = "https://pste.us/api/v1/pastes"
SCRIPT = Path(__file__).resolve().parent / "pi.py"
LOG_FILE = Path("/tmp/webvm-kaggle-feedback.log")
MAX_LOG_CHARS = 30000


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(value):
    text = str(value)
    text = re.sub(r"(?i)KGAT_[A-Za-z0-9_-]+", "[REDACTED_KAGGLE_TOKEN]", text)
    text = re.sub(r"\\b[0-9a-fA-F]{32}\\b", "[REDACTED_HEX_TOKEN]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_ -]?token\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r'(?i)("key"\s*:\s*")[^"]+(")', r"\1[REDACTED]\2", text)
    text = re.sub(r"(?i)(legacy[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    return text


def append_line(line):
    safe = redact(line.rstrip("\r\n"))
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write("[{}] {}\n".format(timestamp(), safe))
    print(safe, flush=True)


def publish(stage):
    try:
        snapshot = LOG_FILE.read_text(encoding="utf-8")[-MAX_LOG_CHARS:]
        content = "{}\nsource=WEBVM\ntime={}\nstage={}\n{}".format(
            FEEDBACK_TAG, timestamp(), stage, snapshot
        )
        payload = json.dumps({
            "content": content,
            "language": "text",
            "expires_in": "1m"
        }).encode("utf-8")
        request = Request(
            FEEDBACK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "webvm-qwen-weight-space-feedback/1.0"
            },
            method="POST"
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
        paste_url = result.get("url") or result.get("paste_url") or result.get("id") or result.get("key")
        print("WEBVM_FEEDBACK_OK stage={} paste={}".format(stage, paste_url or "created"), flush=True)
    except Exception as exc:
        print("WEBVM_FEEDBACK_FAILED stage={} error={}: {}".format(
            stage, type(exc).__name__, redact(exc)
        ), flush=True)


def reader(pipe, messages):
    try:
        for line in iter(pipe.readline, ""):
            messages.put(line)
    finally:
        pipe.close()
        messages.put(None)


def main():
    if not SCRIPT.is_file():
        print("WEBVM_FEEDBACK_FATAL missing script: {}".format(SCRIPT), flush=True)
        return 2

    LOG_FILE.write_text("", encoding="utf-8")
    append_line("PI_WRAPPER_STARTED tag={} script={}".format(FEEDBACK_TAG, SCRIPT.name))
    publish("PI_WRAPPER_STARTED")

    process = subprocess.Popen(
        [sys.executable, "-u", str(SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    messages = queue.Queue()
    thread = threading.Thread(target=reader, args=(process.stdout, messages), daemon=True)
    thread.start()
    last_publish = time.monotonic()
    stream_finished = False

    while not stream_finished or process.poll() is None:
        try:
            line = messages.get(timeout=10)
            if line is None:
                stream_finished = True
            else:
                append_line(line)
                lower = line.lower()
                if any(marker in lower for marker in (
                    "notebook url", "kaggle notebook submission failed",
                    "fatal:", "kaggle reports", "kaggle reported",
                    "kaggle kernels push", "feedback upload unavailable"
                )):
                    publish("PI_PROGRESS")
        except queue.Empty:
            pass

        if time.monotonic() - last_publish >= 60:
            publish("PI_HEARTBEAT")
            last_publish = time.monotonic()

    return_code = process.wait()
    append_line("PI_WRAPPER_FINISHED returncode={}".format(return_code))
    publish("PI_WRAPPER_FINISHED")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
