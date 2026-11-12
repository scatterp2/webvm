#!/usr/bin/env python3
"""Install the development proxy CA into the current user's trust store.

Uses CertAddEncodedCertificateToStore directly, because the usual
`certutil -addstore` and `Import-Certificate` paths both want to show a
confirmation dialog, and a non-interactive session cannot answer it
("UI is not allowed in this operation").

The certificate itself lives on the WebVM (it is fetched from `main` at
boot and used by the proxy's TLS listener).  This only adds the matching
root to the trust store of the machine whose browser loads the WebVM page.

Usage:
    python install_dev_ca.py [--remove]
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

CERT_STORE_PROV_SYSTEM_W = "ROOT"
CERT_SYSTEM_STORE_CURRENT_USER_ID = 0x40000
CERT_STORE_OPEN_ALWAYS = 0x00004000
X509_ASN_ENCODING = 1
CERT_STORE_ADD_REPLACE_EXISTING = 3

wintypes.HCERTSTORE = wintypes.HANDLE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true")
    parser.add_argument(
        "--ca",
        default=str(Path(__file__).resolve().parent
                    / "web_adjuster" / "tls" / "dev-proxy-ca.crt"),
    )
    args = parser.parse_args()

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(Path(args.ca).read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # CertOpenSystemStore is the documented way to open "ROOT" for the
    # current user: dwFlags 0 means CERT_SYSTEM_STORE_CURRENT_USER.
    crypt32.CertOpenSystemStoreA.restype = wintypes.HCERTSTORE
    crypt32.CertOpenSystemStoreA.argtypes = [
        wintypes.DWORD, ctypes.c_char_p, wintypes.DWORD,
    ]
    crypt32.CertAddEncodedCertificateToStore.restype = wintypes.BOOL
    crypt32.CertAddEncodedCertificateToStore.argtypes = [
        wintypes.HCERTSTORE, wintypes.DWORD, ctypes.c_char_p, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]

    store = crypt32.CertOpenSystemStoreA(
        0, b"ROOT", CERT_STORE_OPEN_ALWAYS)
    if not store:
        print("CertOpenSystemStore failed: {}".format(ctypes.get_last_error()))
        return 1
    try:
        context = ctypes.c_void_p()
        ok = crypt32.CertAddEncodedCertificateToStore(
            store, X509_ASN_ENCODING, der, len(der),
            CERT_STORE_ADD_REPLACE_EXISTING, ctypes.byref(context),
        )
        if not ok:
            print("CertAddEncodedCertificateToStore failed: {}".format(
                ctypes.get_last_error()))
            return 1
        if context:
            kernel32.LocalFree(context)
    finally:
        crypt32.CertCloseStore(store, 0)

    print("Installed the development proxy CA for the current user.")
    print("Subject: {}".format(cert.subject.rfc4514_string()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
