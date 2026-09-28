import pytest

from fakes import FIXTURES, ReplayTransport
from shopify_bulk.bulk import reassemble, run_bulk_query
from shopify_bulk.client import ShopifyError

URL = "https://storage.example.com/bulk/9001.jsonl"


def test_reassemble_nests_children_by_type():
    products = list(reassemble((FIXTURES / "bulk_products.jsonl").read_text().splitlines()))
    assert [p["handle"] for p in products][:2] == ["kraft-gift-wrap-roll", "velvet-ribbon-navy"]
    ribbon = products[1]
    assert [v["sku"] for v in ribbon["variants"]] == ["RBN-VEL-NV-10", "RBN-VEL-NV-25"]
    assert products[0]["metafields"][0]["key"] == "care"
    assert "__parentId" not in products[0]["variants"][0]


def test_reassemble_rejects_orphans():
    with pytest.raises(ShopifyError, match="unknown parent"):
        list(reassemble(['{"id": "gid://shopify/ProductVariant/1", "__parentId": "gid://shopify/Product/404"}']))


def test_run_bulk_query_polls_until_completed(make_client, sleeper):
    jsonl = (FIXTURES / "bulk_products.jsonl").read_text()
    t = ReplayTransport(["bulk_started", "bulk_running", "bulk_running", "bulk_completed"], {URL: jsonl})
    seen = []
    out = list(run_bulk_query(make_client(t), "{ products { edges { node { id } } } }", on_progress=seen.append))
    assert len(out) == 6
    assert [s["status"] for s in seen] == ["RUNNING", "RUNNING", "COMPLETED"]
    assert sleeper.calls == [2.0, 3.0]  # poll interval backs off
    assert t.requests[1]["payload"]["variables"] == {"id": "gid://shopify/BulkOperation/9001"}


def test_completed_without_url_means_no_results(make_client):
    t = ReplayTransport(["bulk_started", "bulk_completed_empty"])
    assert list(run_bulk_query(make_client(t), "{ products { edges { node { id } } } }")) == []


def test_failed_operation_reports_partial_data(make_client):
    t = ReplayTransport(["bulk_started", "bulk_failed"])
    with pytest.raises(ShopifyError, match="FAILED: TIMEOUT.*partial"):
        list(run_bulk_query(make_client(t), "{ products { edges { node { id } } } }"))


def test_rejected_query_surfaces_user_error(make_client):
    t = ReplayTransport(["bulk_rejected"])
    with pytest.raises(ShopifyError, match="more than 5 connections"):
        list(run_bulk_query(make_client(t), "{ products { edges { node { id } } } }"))
