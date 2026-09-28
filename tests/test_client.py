import pytest
import requests

from fakes import ReplayTransport
from shopify_bulk.client import ShopifyError, UserErrors
from shopify_bulk.queries import TAGS_ADD

Q = "query ShopName { shop { name } }"


def test_success_returns_data_and_syncs_throttle(make_client):
    t = ReplayTransport(["shop_ok"])
    c = make_client(t)
    assert c.execute(Q) == {"shop": {"name": "Example Paper Co."}}
    assert c.throttle.available == 1999
    assert c.throttle.maximum == 2000
    req = t.requests[0]
    assert req["url"] == "https://example-paper.myshopify.com/admin/api/2026-07/graphql.json"
    assert req["headers"]["X-Shopify-Access-Token"] == "test-token"


def test_bare_shop_name_is_expanded(make_client):
    from shopify_bulk.client import GraphQLClient

    c = GraphQLClient(shop="https://example-paper.myshopify.com/", token="x")
    assert c.shop == "example-paper.myshopify.com"
    assert GraphQLClient(shop="example-paper", token="x").shop == "example-paper.myshopify.com"


def test_throttled_then_ok_waits_for_requested_cost(make_client, sleeper):
    t = ReplayTransport(["throttled", "shop_ok"])
    c = make_client(t)
    c.execute(Q)
    # throttled.json: requested 752, available 120, restore 100/s -> ~6.32 s
    assert sleeper.calls[0] == pytest.approx(6.32, abs=0.01)
    assert c.stats.throttled == 1 and c.stats.retries == 1


def test_http_429_honours_retry_after(make_client, sleeper):
    t = ReplayTransport([(429, {"Retry-After": "2.0"}, None), "shop_ok"])
    make_client(t).execute(Q)
    assert 2.0 in sleeper.calls


def test_5xx_and_network_errors_retry_with_backoff(make_client, sleeper):
    t = ReplayTransport([(502, {}, None), requests.ConnectionError("reset"), "shop_ok"])
    assert make_client(t).execute(Q)["shop"]["name"] == "Example Paper Co."
    assert sleeper.calls == [0.5, 1.0]


def test_gives_up_after_max_retries(make_client):
    t = ReplayTransport([(503, {}, None)] * 7)
    with pytest.raises(ShopifyError, match="giving up after 6 retries"):
        make_client(t).execute(Q)


def test_graphql_errors_are_not_retried(make_client):
    t = ReplayTransport(["graphql_error"])
    with pytest.raises(ShopifyError, match="doesn't exist"):
        make_client(t).execute(Q)
    assert len(t.requests) == 1


def test_4xx_fails_fast(make_client):
    t = ReplayTransport([(401, {}, None)])
    with pytest.raises(ShopifyError, match="HTTP 401"):
        make_client(t).execute(Q)


def test_user_errors_raise(make_client):
    t = ReplayTransport(["tags_add_user_error"])
    with pytest.raises(UserErrors, match="id: Product does not exist"):
        make_client(t).mutate(TAGS_ADD, {"id": "gid://shopify/Product/1", "tags": ["x"]}, "tagsAdd")
