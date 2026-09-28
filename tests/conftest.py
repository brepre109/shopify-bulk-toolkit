import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FIXTURES, FakeShop  # noqa: E402
from shopify_bulk.client import GraphQLClient  # noqa: E402
from shopify_bulk.snapshot import normalize_product  # noqa: E402
from shopify_bulk.bulk import reassemble  # noqa: E402

SHOP = "example-paper.myshopify.com"


class Sleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(round(seconds, 3))


@pytest.fixture
def sleeper():
    return Sleeper()


@pytest.fixture
def shop():
    return FakeShop()


@pytest.fixture
def make_client(sleeper):
    def _make(transport):
        return GraphQLClient(shop=SHOP, token="test-token", transport=transport, sleep=sleeper, jitter=lambda: 0.0)
    return _make


@pytest.fixture
def products():
    lines = (FIXTURES / "bulk_products.jsonl").read_text().splitlines()
    return [normalize_product(p) for p in reassemble(lines)]
