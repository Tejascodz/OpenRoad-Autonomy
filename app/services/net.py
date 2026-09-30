"""Which CA bundle HTTPS requests verify against (shown by `python -m app.cli check-network`).

TLS is always verified: an explicit SSL_CERT_FILE / REQUESTS_CA_BUNDLE wins (e.g. behind a
corporate proxy), otherwise certifi's CA bundle.
"""
from __future__ import annotations

import os


def ca_file() -> str | None:
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            return path
    try:
        import certifi

        return certifi.where()
    except ImportError:  # pragma: no cover
        return None

