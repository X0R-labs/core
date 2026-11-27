"""Send guard decisions to a Core workspace."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .sdk import DecisionRecord


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def core_sink(
    url: str, api_key: str, *, timeout: float = 5.0, retries: int = 2,
) -> Callable[[DecisionRecord], None]:
    """Return an on_decision hook that posts each record and retries transient errors."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.query or parsed.fragment):
        raise ValueError("Core URL must be an HTTP or HTTPS base URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Core URL must use HTTPS except on localhost")
    if retries < 0:
        raise ValueError("retries must be non-negative")

    endpoint = f"{url.rstrip('/')}/v1/policy/decisions"
    opener = build_opener(_NoRedirect())

    def send(record: DecisionRecord) -> None:
        body = json.dumps({"decisions": [asdict(record)]}).encode("utf-8")
        request = Request(endpoint, data=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        for attempt in range(retries + 1):
            try:
                with opener.open(request, timeout=timeout):
                    return
            except HTTPError as exc:
                exc.close()
                if not 500 <= exc.code < 600 or attempt == retries:
                    raise
            except (OSError, HTTPException):
                if attempt == retries:
                    raise

    return send
