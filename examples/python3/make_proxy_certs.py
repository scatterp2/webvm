#!/usr/bin/env python3
"""Generate a throwaway development CA and a wildcard server certificate.

The WebVM page is served over HTTPS, and Chrome blocks an ``http://``
iframe on an HTTPS page as mixed content.  The proxy therefore has to be
reachable over HTTPS.

The WebVM's Tailscale node keeps its name across reboots only in the
``js-N`` part (``js-2``, ``js-14``, ...), and the MagicDNS suffix is
stable, so a wildcard certificate for ``*.<tailnet-suffix>`` covers every
reboot.  The certificate is issued for the MagicDNS name, not the IP,
because the IP changes on every boot.

Usage:
    python make_proxy_certs.py [--suffix tailf987e.ts.net] [--out tls]
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "WebVM Dev Proxy CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WebVM dev (throwaway)"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_server(ca_key, ca_cert, suffix):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName([
        x509.DNSName("*." + suffix),
        x509.DNSName(suffix),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "*." + suffix),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # OpenSSL 3 refuses to build a chain without the authority key
        # identifier, so both it and the subject key identifier are needed.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def write(path, data):
    path.write_bytes(data)
    print("wrote {} ({} bytes)".format(path, len(data)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suffix", default="tailf987e.ts.net")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent
                                             / "web_adjuster" / "tls"))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ca_key, ca_cert = make_ca()
    server_key, server_cert = make_server(ca_key, ca_cert, args.suffix)

    write(out / "dev-proxy-ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))
    write(out / "dev-proxy-server.crt",
          server_cert.public_bytes(serialization.Encoding.PEM))
    write(out / "dev-proxy-server.key",
          server_key.private_bytes(
              encoding=serialization.Encoding.PEM,
              format=serialization.PrivateFormat.TraditionalOpenSSL,
              encryption_algorithm=serialization.NoEncryption(),
          ))

    print()
    print("Trust this CA in the browser that loads the WebVM page:")
    print("  certutil -user -addstore Root {}".format(out / "dev-proxy-ca.crt"))
    print("The WebVM page will use https://<node>.{}/proxy/<url>".format(args.suffix))


if __name__ == "__main__":
    main()
