"""The one way this system talks to outside services over HTTPS (Xero, Google).

A transport takes a prepared request and returns the response body. Code that calls an outside
service accepts a transport argument, so tests pass a fake one and never touch the network.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable

Transport = Callable[[urllib.request.Request], bytes]


def https_transport(request: urllib.request.Request) -> bytes:
    """Send the request over HTTPS (plain HTTP is refused) and return the body."""
    if not request.full_url.startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS URL: {request.full_url}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https enforced above
        body: bytes = response.read()
        return body
