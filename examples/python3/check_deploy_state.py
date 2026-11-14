#!/usr/bin/env python3
"""What has the Pages deploy published right now?"""
import re
import urllib.request

req = urllib.request.Request("https://scatterp2.github.io/webvm/?t=2",
                             headers={"Cache-Control": "no-cache",
                                      "User-Agent": "probe"})
text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
print("index bytes:", len(text))
for m in re.findall(r'(?:src|href)="([^"]+)"', text)[:16]:
    print("   ", m)

print()
for name in ("config_github_terminal.js", "alpine.html", "config.js"):
    url = "https://scatterp2.github.io/webvm/" + name
    try:
        req = urllib.request.Request(url + "?t=2",
                                     headers={"Cache-Control": "no-cache",
                                              "User-Agent": "probe"})
        body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        images = re.findall(r"[A-Za-z0-9_\-]+\.ext2", body)
        print("%-28s %d bytes  images=%s" % (name, len(body), images[:2]))
    except Exception as exc:
        print("%-28s ERR %s" % (name, exc))
