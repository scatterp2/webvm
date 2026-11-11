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

def start_proxy():
    """Start the wrapper/adjuster proxy so port 28080 is listening."""
    launcher = Path(__file__).resolve().parent / "web_adjuster" / "launcher.py"
    if not launcher.is_file():
        print("PROXY_LAUNCHER_MISSING path={}".format(launcher), flush=True)
        return
    print("PROXY_STARTING launcher={}".format(launcher), flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log_file = open("/tmp/web-adjuster.log", "ab", buffering=0)
    process = subprocess.Popen(
        [sys.executable, str(launcher)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    log_file.close()
    for _ in range(60):
        if process.poll() is not None:
            print("PROXY_LAUNCHER_EXITED code={}".format(process.returncode), flush=True)
            return
        try:
            import socket
            s = socket.create_connection(("127.0.0.1", 28080), timeout=0.5)
            s.close()
            print("PROXY_READY port=28080 pid={}".format(process.pid), flush=True)
            return
        except OSError:
            time.sleep(1)
    print("PROXY_START_TIMEOUT pid={}".format(process.pid), flush=True)

def load_pi_impl():
    last_error = None
    for attempt in range(1, 4):
        try:
            request = Request(
                PI_IMPL_URL + "?t=" + str(time.time_ns()),
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
    for port in (28080, 28081):
        try:
            subprocess.run(["fuser", "-k", "{}/tcp".format(port)],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    start_proxy()
    load_pi_impl()
    main()
