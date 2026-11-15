#!/usr/bin/env python3
"""Check the new send-path helpers without a WebVM.

Covers the two things that only show up under CheerpX in the field but can
be pinned down here:

  * /proxy-loopback measures the writer over loopback, so a short read
    there is the writer's fault and a short read from outside the guest is
    the network's.
  * _sleep() survives a clock that raises OverflowError on long pauses.
"""
import json
import socket
import sys
import time

sys.path.insert(0, ".")
from web_adjuster import launcher  # noqa: E402

PORT = 38102
failures = []


def check(name, condition, detail=""):
    print("{} {}".format("ok  " if condition else "FAIL", name))
    if detail:
        print("     " + detail)
    if not condition:
        failures.append(name)


server = launcher.start_wrapper(PORT, "wrapper")
time.sleep(0.5)


def get(path, timeout=60):
    sock = socket.socket()
    sock.settimeout(timeout)
    sock.connect(("127.0.0.1", PORT))
    sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  "Connection: close\r\n\r\n" % path).encode())
    data = b""
    while True:
        part = sock.recv(65536)
        if not part:
            break
        data += part
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


head, body = get("/proxy-loopback?bytes=101248")
payload = json.loads(body.decode())
check("loopback delivers a 101 KB body",
      payload.get("got") == 101248,
      "got={} error={}".format(payload.get("got"), payload.get("error")))
check("loopback never deadlocks the server",
      payload.get("status", "").startswith("HTTP/1.1 200"))

# A clock that rejects long pauses must not end the response half-written.
real_sleep = launcher.time.sleep
calls = []


def flaky_sleep(seconds):
    calls.append(seconds)
    if seconds > 0.06:
        raise OverflowError("timestamp too large to convert to C _PyTime_t")
    real_sleep(min(seconds, 0.002))


launcher.time.sleep = flaky_sleep
try:
    ok = launcher._sleep(0.3)
finally:
    launcher.time.sleep = real_sleep
check("_sleep splits long pauses into short ones",
      ok is None and len(calls) > 1,
      "calls={} first={}".format(len(calls), calls[0] if calls else None))

launcher.time.sleep = lambda s: (_ for _ in ()).throw(OverflowError("boom"))
try:
    launcher._sleep(0.05)
    survived = True
except Exception:
    survived = False
finally:
    launcher.time.sleep = real_sleep
check("_sleep never raises", survived)

server.shutdown()
print()
if failures:
    print("FAILED: " + ", ".join(failures))
    sys.exit(1)
print("ALL LOOPBACK TESTS PASSED")
