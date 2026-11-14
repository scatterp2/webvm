#!/usr/bin/env python3
"""Tests for the cookie jar, the cookie bridge and the fetch metadata.

These are the pieces that decide whether a proxied SPA can talk to its
own back end:

  * the page reads a cookie (Kaggle reads XSRF-TOKEN) to build a request
    header, and a credentialless iframe has no cookies;
  * the page's own API calls must reach the proxy, not the internet;
  * the request must look like a request the browser would make
    directly, because the target's bot detection is looking for
    anything else.
"""
import sys

sys.path.insert(0, ".")
from web_adjuster import launcher  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
    print("  %-5s %s" % ("ok" if condition else "FAIL", message))


print("[1] the cookie jar stores what upstream sends")
launcher._COOKIE_JAR.clear()
jar = launcher.cookie_jar_store("https://www.kaggle.com", [
    "XSRF-TOKEN=abc123; Path=/; Secure; HttpOnly",
    "GCLB=xyz; Path=/",
])
print("    jar:", jar)
check(jar.get("XSRF-TOKEN") == "abc123", "XSRF-TOKEN is stored")
check(jar.get("GCLB") == "xyz", "GCLB is stored")

print("[2] an expired cookie is dropped")
launcher.cookie_jar_store("https://www.kaggle.com", ["GCLB=; Max-Age=0"])
jar = launcher.cookie_jar_get("https://www.kaggle.com")
print("    jar:", jar)
check("GCLB" not in jar, "the expired cookie is gone")
check(jar.get("XSRF-TOKEN") == "abc123", "the other cookie survived")

print("[3] the jar becomes a Cookie request header")
header = launcher.cookie_jar_header("https://www.kaggle.com")
print("    header:", header)
check(header == "XSRF-TOKEN=abc123", "the header holds the token")

print("[4] the cookie bridge is injected with the nonce and the jar")
script = launcher.build_runtime_scripts(
    "NONCE", "https://www.kaggle.com", "https://proxy.example:28443",
    launcher.cookie_jar_string("https://www.kaggle.com"))
print("    script tags:", script.count("<script"))
check(script.count("<script") == 2, "two scripts (routing patch + cookie bridge)")
check(script.count('nonce="NONCE"') == 2, "both scripts carry the CSP nonce")
check("XSRF-TOKEN=abc123" in script, "the bridge hands the page the token")
check("/proxy-cookie?target=" in script, "the bridge has somewhere to write")
check("XMLHttpRequest.prototype.open" in script, "the routing patch is present")
check("window.fetch=function" in script, "the routing patch patches fetch")

print("[4b] the endpoint is a plain URL, not a JSON-quoted one")
check("'https://proxy.example:28443/proxy-cookie?target=" in script,
      "the endpoint is a bare URL inside the script")
check('"https://proxy.example' not in script,
      "the endpoint does not carry JSON quotes")

print("[5] the proof marker still carries the nonce")
proof = launcher.build_injection_script("NONCE")
check('nonce="NONCE"' in proof, "the proof marker carries the nonce")

print("[6] a document request keeps the navigation shape")
out = launcher.build_upstream_headers(
    "https://www.kaggle.com/", "GET",
    {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
     "Sec-Fetch-Site": "cross-site", "Sec-Fetch-User": "?1"})
print("    sec-fetch:", {k: v for k, v in out.items()
                        if k.lower().startswith("sec-fetch")})
check(out.get("sec-fetch-mode") == "navigate", "the mode is navigate")
check(out.get("sec-fetch-site") == "none", "the site is none, not cross-site")

print("[7] an API call keeps the browser's shape")
out = launcher.build_upstream_headers(
    "https://www.kaggle.com/api/i/users.HomePageService/GetLoggedOutHomepageData",
    "POST",
    {"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
     "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"})
print("    sec-fetch:", {k: v for k, v in out.items()
                        if k.lower().startswith("sec-fetch")})
check(out.get("sec-fetch-mode") == "cors", "the mode is cors, not navigate")
check(out.get("sec-fetch-dest") == "empty", "the destination is empty")
check(out.get("sec-fetch-site") == "same-origin", "the site is same-origin")

print("[8] client hints are forwarded, not dropped")
out = launcher.build_upstream_headers(
    "https://www.kaggle.com/", "GET",
    {"Sec-CH-UA": '"Chromium";v="124"', "Sec-CH-UA-Mobile": "?0",
     "Sec-CH-UA-Platform": '"Linux"'})
print("    hints:", {k: v for k, v in out.items() if k.lower().startswith("sec-ch")})
check(any(k.lower() == "sec-ch-ua" for k in out), "sec-ch-ua is forwarded")
check(any(k.lower() == "sec-ch-ua-platform" for k in out),
      "sec-ch-ua-platform is forwarded")

print("[9] the proxy's own URL never leaks upstream")
out = launcher.build_upstream_headers(
    "https://www.kaggle.com/", "POST",
    {"Origin": "https://webvm-proxy.tailf987e.ts.net:28443",
     "Referer": "https://webvm-proxy.tailf987e.ts.net:28443/proxy/x"})
print("    origin:", out.get("Origin"), " referer:", out.get("Referer"))
check(out.get("Origin") == "https://www.kaggle.com",
      "Origin is the target origin")
check("webvm-proxy" not in out.get("Referer", ""),
      "Referer does not leak the proxy URL")

print("[10] the jar is replayed upstream")
out = launcher.build_upstream_headers(
    "https://www.kaggle.com/api/i/x", "POST", {})
print("    cookie:", out.get("Cookie"))
check(out.get("Cookie") == "XSRF-TOKEN=abc123",
      "the upstream request carries the session cookie")

print()
if failures:
    print("FAILURES:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("ALL COOKIE AND HEADER TESTS PASSED")
