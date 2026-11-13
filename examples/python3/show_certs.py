#!/usr/bin/env python3
"""Report the subject, issuer and validity of the published TLS files."""
import urllib.request
from cryptography import x509

BASE = ("https://raw.githubusercontent.com/scatterp2/webvm/main/"
        "examples/python3/web_adjuster/tls/")

for name in ["dev-proxy-ca.crt", "dev-proxy-server.crt", "proxy-leaf.crt",
             "webvm-proxy.crt"]:
    req = urllib.request.Request(BASE + name, headers={
        "User-Agent": "probe", "Cache-Control": "no-cache"})
    data = urllib.request.urlopen(req, timeout=30).read()
    try:
        cert = x509.load_pem_x509_certificate(data)
        san = []
        try:
            ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            san = [str(n) for n in ext.value]
        except Exception:
            pass
        print("== " + name)
        print("   subject : " + cert.subject.rfc4514_string())
        print("   issuer  : " + cert.issuer.rfc4514_string())
        print("   valid   : %s .. %s" % (cert.not_valid_before_utc.date(),
                                          cert.not_valid_after_utc.date()))
        print("   SAN     : " + ", ".join(san))
    except Exception as exc:
        print("== " + name + "  (not a single PEM cert: %s)" % exc)
