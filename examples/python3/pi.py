#!/usr/bin/env python3

# Keep these three assignments exactly as they currently
# appear in examples/python3/pi.py.
USERNAME = "simonday"
LEGACY_KEY = "fc837b9fcbf01dd51978287f9437068c"
API_TOKEN = "KGAT_2e52efc50d91a13e1b7a3eabce8ac33e"

# The base image contains this launcher, but the implementation is loaded
# from main at runtime so fixes in pi_impl.py can be activated by a browser reset
# without rebuilding the large WebVM disk image.
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import subprocess
import os
import sys

PI_IMPL_URL = "https://raw.githubusercontent.com/scatterp2/webvm/main/examples/python3/pi_impl.py"


def cache_buster():
    """A value that changes on every call, even when the clock does not.

    The cache-buster has to be unpredictable rather than clock-based: the
    VM's clock can repeat between boots, and then "?t=<time>" is the same
    URL every time -- so the CDN serves the copy it cached for the first
    boot and the VM silently runs the code from an older commit.  That is
    exactly what happened: a reset ran the previous commit's launcher.py
    and the fix that was pushed just before it never took effect.
    """
    try:
        return os.urandom(8).hex()
    except Exception:
        return "{}".format(time.time_ns())

def start_proxy():
    """Start the HTTP proxy.

    The proxy now runs **inside** the pi_impl.py process, as daemon threads
    on ports 28082 (wrapper + iframe page) and 28081/28083 (forward proxy).

    It used to be started here as a separate `launcher.py` subprocess.  That
    approach was abandoned because:

      * `http.server`/`socketserver` misbehave under CheerpX — the response
        header write failed with `OSError: [Errno 107] Transport endpoint is
        not connected` and the client never received a reply.
      * The `adjuster` pip package had to be installed at boot and then run
        as its own long-lived process, which CheerpX starves.

    launcher.py is now a pure-Python proxy built on raw sockets and runs
    in-process, so there is nothing to spawn here.
    """
    print("PROXY_STARTING mode=in-process (handled by pi_impl.main)", flush=True)

def load_pi_impl():
    last_error = None
    for attempt in range(1, 4):
        try:
            request = Request(
                PI_IMPL_URL + "?t=" + cache_buster(),
                headers={
                    "Cache-Control": "no-cache",
                    "User-Agent": "webvm-kaggle-submitter/1.0",
                },
            )
            with urlopen(request, timeout=20) as response:
                source = response.read().decode("utf-8")
            compile(source, PI_IMPL_URL, "exec")
            print("PI_IMPL_FETCHED source=main characters={}".format(len(source)), flush=True)
            exec(compile(source, PI_IMPL_URL, "exec"), globals(), globals())
            return
        except (HTTPError, URLError, TimeoutError, OSError, SyntaxError) as exc:
            last_error = exc
            print(
                "PI_IMPL_FETCH_RETRY attempt={} error={}: {}".format(
                    attempt, type(exc).__name__, exc
                ),
                flush=True,
            )
            if attempt < 3:
                time.sleep(attempt * 2)

    # Preserve offline operation if the GitHub source cannot be reached.
    local_impl = Path(__file__).resolve().with_name("pi_impl.py")
    if local_impl.is_file():
        print(
            "PI_IMPL_FALLBACK source=local reason={}: {}".format(
                type(last_error).__name__ if last_error else "unknown", last_error
            ),
            flush=True,
        )
        import pi_impl
        globals()["main"] = pi_impl.main
        return
    raise RuntimeError("Could not load pi_impl.py from main or local fallback: {}".format(last_error))


if __name__ == "__main__":
    start_proxy()
    load_pi_impl()
    main()
