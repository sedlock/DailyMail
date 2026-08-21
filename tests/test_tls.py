"""TLS configuration. Verification must never be disabled anywhere."""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi

from dailymail.tls import INTERMEDIATE_PEM, build_ssl_context

SRC = Path(__file__).resolve().parents[1] / "src" / "dailymail"


def test_context_verifies_certificates_and_hostnames():
    context = build_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_intermediate_certificate_is_vendored_and_parseable():
    pem = INTERMEDIATE_PEM.read_text(encoding="ascii")
    assert "BEGIN CERTIFICATE" in pem
    # Loading into a fresh context proves it is a usable CA certificate.
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=pem)


def test_intermediate_is_the_expected_incommon_ca():
    context = build_ssl_context()
    subjects = [
        dict(item for pair in cert["subject"] for item in pair)
        for cert in context.get_ca_certs()
    ]
    common_names = {subject.get("commonName") for subject in subjects}
    assert "InCommon RSA Server CA 2" in common_names
    # Trust still terminates at a standard public root.
    assert "USERTrust RSA Certification Authority" in common_names


def test_context_adds_to_rather_than_replaces_the_public_roots():
    baseline = ssl.create_default_context(cafile=certifi.where())
    assert len(build_ssl_context().get_ca_certs()) > len(baseline.get_ca_certs()) - 1


def test_no_verification_bypass_anywhere_in_the_package():
    """A regression guard: these are the phrases that would silently disable TLS."""
    banned = (
        "verify=False",
        "verify=None",
        "ignoreHTTPSErrors",
        "CERT_NONE",
        "check_hostname = False",
        "check_hostname=False",
        "_create_unverified_context",
    )
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            if phrase in text:
                offenders.append(f"{path.name}: {phrase}")
    assert offenders == [], offenders
