#!/usr/bin/env python3
"""Stable Kaggle notebook bootstrap: fetch the current experiment from raw GitHub on every run."""
import hashlib
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

RUNTIME_URL = "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/kaggle_runtime.py"
request_url = RUNTIME_URL + "?run=" + quote(datetime.now().isoformat())
request = Request(
    request_url,
    headers={
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "webvm-kaggle-runtime-bootstrap/1.0",
    },
)
with urlopen(request, timeout=60) as response:
    runtime_source = response.read().decode("utf-8")
runtime_hash = hashlib.sha256(runtime_source.encode("utf-8")).hexdigest()
print("KAGGLE_RUNTIME_FETCHED url=" + RUNTIME_URL + " sha256=" + runtime_hash, flush=True)
exec(compile(runtime_source, RUNTIME_URL, "exec"), {"__name__": "__main__", "__file__": RUNTIME_URL})
