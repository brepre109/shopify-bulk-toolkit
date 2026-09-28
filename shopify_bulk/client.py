"""Admin GraphQL client with cost-aware throttling and bounded retries."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from . import DEFAULT_API_VERSION
from .throttle import CostThrottle
from .transport import RequestsTransport, Transport


class ShopifyError(RuntimeError):
    """A request failed permanently (after retries) or returned GraphQL errors."""


class UserErrors(ShopifyError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{'.'.join(map(str, e.get('field') or []))}: {e.get('message')}" for e in errors))


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    throttled: int = 0
    cost: float = 0.0


@dataclass
class GraphQLClient:
    shop: str
    token: str
    api_version: str = DEFAULT_API_VERSION
    transport: Transport = field(default_factory=RequestsTransport)
    throttle: CostThrottle = field(default_factory=CostThrottle)
    max_retries: int = 6
    timeout: float = 60.0
    sleep: Callable[[float], None] = field(default=lambda seconds: time.sleep(seconds))
    jitter: Callable[[], float] = random.random
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        shop = self.shop.strip().removeprefix("https://").rstrip("/")
        if "." not in shop:
            shop = f"{shop}.myshopify.com"
        self.shop = shop
        # Share the injected sleep with the throttle so tests never really wait.
        self.throttle.sleep = self.sleep

    @property
    def url(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    def _backoff(self, attempt: int) -> float:
        return min(30.0, (2**attempt) * 0.5) + self.jitter() * 0.25

    def execute(self, query: str, variables: dict | None = None, *, estimated_cost: float = 10.0) -> dict:
        """Run one GraphQL operation and return ``data``.

        * waits for bucket capacity before sending (``estimated_cost``),
        * retries THROTTLED responses, HTTP 429 and 5xx with backoff,
        * raises :class:`ShopifyError` for GraphQL errors that are not throttling.
        """
        payload = {"query": query, "variables": variables or {}}
        headers = {"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"}
        last_problem = ""
        for attempt in range(self.max_retries + 1):
            self.throttle.acquire(estimated_cost)
            self.stats.requests += 1
            try:
                resp = self.transport.post_json(self.url, payload, headers, self.timeout)
            except Exception as exc:  # network errors: retry
                last_problem = f"network error: {exc}"
                self.stats.retries += 1
                self.sleep(self._backoff(attempt))
                continue

            if resp.status == 429:
                self.stats.throttled += 1
                self.stats.retries += 1
                retry_after = float((resp.headers or {}).get("Retry-After", 0) or 0)
                self.sleep(retry_after or self._backoff(attempt))
                last_problem = "HTTP 429"
                continue
            if resp.status >= 500:
                self.stats.retries += 1
                self.sleep(self._backoff(attempt))
                last_problem = f"HTTP {resp.status}"
                continue
            if resp.status >= 400 or resp.body is None:
                raise ShopifyError(f"HTTP {resp.status}: {resp.text[:300]}")

            cost = (resp.body.get("extensions") or {}).get("cost") or {}
            self.throttle.update(cost.get("throttleStatus"))
            errors = resp.body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                self.stats.throttled += 1
                self.stats.retries += 1
                requested = float(cost.get("requestedQueryCost") or estimated_cost)
                wait = self.throttle.wait_time(requested)
                self.sleep(max(wait, 1.0))
                last_problem = "THROTTLED"
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            self.stats.cost += float(cost.get("actualQueryCost") or 0)
            return resp.body.get("data") or {}
        raise ShopifyError(f"giving up after {self.max_retries} retries ({last_problem})")

    def mutate(self, query: str, variables: dict, payload_key: str, *, estimated_cost: float = 10.0) -> dict:
        """Run a mutation and raise :class:`UserErrors` if its payload reports any."""
        data = self.execute(query, variables, estimated_cost=estimated_cost)
        payload = data.get(payload_key) or {}
        if payload.get("userErrors"):
            raise UserErrors(payload["userErrors"])
        return payload

    def download(self, url: str) -> str:
        return self.transport.get_text(url, self.timeout)
