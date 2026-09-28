"""HTTP transport, kept behind a tiny interface so tests can replay fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests


@dataclass
class Response:
    status: int
    headers: dict
    body: dict | None
    text: str = ""


class Transport(Protocol):
    def post_json(self, url: str, payload: dict, headers: dict, timeout: float) -> Response: ...

    def get_text(self, url: str, timeout: float) -> str: ...


class RequestsTransport:
    def __init__(self) -> None:
        self.session = requests.Session()

    def post_json(self, url: str, payload: dict, headers: dict, timeout: float) -> Response:
        r = self.session.post(url, json=payload, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = None
        return Response(status=r.status_code, headers=dict(r.headers), body=body, text=r.text[:2000])

    def get_text(self, url: str, timeout: float) -> str:
        # Bulk result URLs are pre-signed storage links: no Shopify auth header.
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
