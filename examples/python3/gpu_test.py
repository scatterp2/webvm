#!/usr/bin/env python3
"""Stable Kaggle notebook bootstrap: fetch the current experiment from raw GitHub on every run."""
import hashlib
from urllib.request import Request, urlopen

RUNTIME_URL = "https://raw.githubusercontent.com/scatterp2/webvm/runtime-feedback-monitor/examples/python3/kaggle_runtime.py"
request = Request(RUNTIME_URL + "?run=" + __import__("urllib.parse").parse.quote(__import__("datetime").datetime.now().isoformat()), headers={"Cache-Control": "no-cache", "User-Agent": "webvm-kaggle-runtime-bootstrap/1.0"})
with urlopen(request, timeout=60) as response:
    runtime_source = response.read().decode("utf-8")
runtime_hash = hashlib.sha256(runtime_source.encode("utf-8")).hexdigest()
print("KAGGLE_RUNTIME_FETCHED url=" + RUNTIME_URL + " sha256=" + runtime_hash, flush=True)
exec(compile(runtime_source, RUNTIME_URL, "exec"), {"__name__": "__main__", "__file__": RUNTIME_URL})
