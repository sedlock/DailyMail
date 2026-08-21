"""TLS context construction.

`apps.rowan.edu` presents its leaf certificate but omits the
`InCommon RSA Server CA 2` intermediate, so a stock client cannot build a chain
to the (perfectly normal, publicly trusted) USERTrust root. Phase 0 §2.5.

We fix that by adding the missing intermediate to an otherwise standard trust
store. Verification stays fully enabled — there is no code path in DailyMail
that disables certificate or hostname checking.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi

INTERMEDIATE_PEM = Path(__file__).parent / "certs" / "incommon-rsa-server-ca-2.pem"


def build_ssl_context() -> ssl.SSLContext:
    """Standard roots (certifi) plus the intermediate Rowan fails to send."""
    context = ssl.create_default_context(cafile=certifi.where())

    # Belt and braces: create_default_context already sets both of these, but
    # they are the two settings that must never regress, so assert them locally.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    pem = INTERMEDIATE_PEM.read_text(encoding="ascii")
    context.load_verify_locations(cadata=pem)
    return context
