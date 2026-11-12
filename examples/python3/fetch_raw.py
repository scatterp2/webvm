#!/usr/bin/env python3
"""Fetch a path from the WebVM wrapper and dump the raw response.

Usage: python fetch_raw.py <host> <port> <path>
"""
import socket
import sys

HOST = sys.argv[1]
PORT = int(sys.argv[2])
PATH = sys.argv[3] if len(sys.argv) > 3 else "/"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(30)
s.connect((HOST, PORT))
s.sendall(
    "GET {} HTTP/1.0\r\nHost: {}:{}\r\nAccept-Encoding: gzip, deflate, br\r\n"
    "Connection: close\r\n\r\n".format(PATH, HOST, PORT).encode()
)
data = b""
while True:
    try:
        chunk = s.recv(16384)
    except socket.timeout:
        break
    if not chunk:
        break
    data += chunk
s.close()

head, _, body = data.partition(b"\r\n\r\n")
print("--- headers ---")
print(head.decode("utf-8", "replace"))
print("--- body ({} bytes) ---".format(len(body)))
print(body[:1500].decode("utf-8", "replace"))
