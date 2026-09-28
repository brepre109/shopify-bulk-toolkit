from datetime import datetime, timedelta, timezone

import pytest

from conftest import SHOP
from shopify_bulk.apply import apply_plan, journal_path
from shopify_bulk.matching import load_rules
from shopify_bulk.ops.categories import plan_categories
from shopify_bulk.ops.metafields import plan_metafields
from shopify_bulk.ops.redirects import plan_redirects
from shopify_bulk.ops.tags import plan_tags
from shopify_bulk.plan import PlanError


def run(plan, client, tmp_path, **kw):
    path = tmp_path / "plan.json"
    plan.save(path)
    kw.setdefault("log", lambda m: None)
    return apply_plan(plan, client, path, **kw)


def test_preflight_never_writes(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["spring-2026"])
    s = run(plan, make_client(shop), tmp_path, execute=False)
    assert s.would_apply == 6 and s.applied == 0
    assert shop.mutations == []
    assert not journal_path(tmp_path / "plan.json").exists()


def test_apply_then_rerun_is_a_noop(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["spring-2026"], remove=["kraft"])
    client = make_client(shop)
    s = run(plan, client, tmp_path, execute=True)
    assert s.applied == 6 and s.failed == 0
    assert shop.product("kraft-gift-wrap-roll")["tags"] == ["gift-wrap", "spring-2026"]
    # only the delta is sent, never a full tag list
    assert ("tagsRemove", {"id": "gid://shopify/Product/1001", "tags": ["kraft"]}) in shop.mutations

    n = len(shop.mutations)
    s2 = run(plan, client, tmp_path, execute=True)
    assert s2.skipped_done == 6 and s2.applied == 0
    assert len(shop.mutations) == n


def test_already_applied_changes_are_detected_live(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["x"])
    shop.product("washi-tape-set-pastel")["tags"] = ["x"]  # someone did it by hand
    s = run(plan, make_client(shop), tmp_path, execute=True)
    assert s.already_applied == 1 and s.applied == 5


def test_failures_are_journaled_and_retried_on_resume(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["x"])
    shop.fail["TagsAdd"] = {"gid://shopify/Product/1003"}
    client = make_client(shop)
    s = run(plan, client, tmp_path, execute=True)
    assert s.failed == 1 and s.applied == 5
    assert "FAILED satin-ribbon-blush: id: Simulated failure" in s.problems

    shop.fail.clear()
    s2 = run(plan, client, tmp_path, execute=True)
    assert s2.skipped_done == 5 and s2.applied == 1
    assert "x" in shop.product("satin-ribbon-blush")["tags"]


def test_limit_gives_a_canary_run(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["x"])
    s = run(plan, make_client(shop), tmp_path, execute=True, limit=2)
    assert s.applied == 2
    s2 = run(plan, make_client(shop), tmp_path, execute=True)
    assert s2.skipped_done == 2 and s2.applied == 4


def test_category_drift_is_skipped_unless_forced(shop, make_client, products, tmp_path):
    rules = load_rules([{"match": {"productType": "Ribbon"}, "category": "ae-2-1-2-14"}])
    plan = plan_categories(products, SHOP, rules=rules)
    assert [c.label.split()[0] for c in plan.changes] == ["velvet-ribbon-navy"]
    # a merchant sets a category by hand after the dry run
    shop.product("velvet-ribbon-navy")["category"] = {"id": "gid://shopify/TaxonomyCategory/ae-2-1-2-3"}
    client = make_client(shop)
    s = run(plan, client, tmp_path, execute=True)
    assert s.drifted == 1 and s.applied == 0
    assert shop.mutations == []
    s2 = run(plan, client, tmp_path, execute=True, force=True)
    assert s2.applied == 1
    assert shop.product("velvet-ribbon-navy")["category"]["id"].endswith("/ae-2-1-2-14")


def test_uncategorized_node_and_null_are_interchangeable_for_drift(shop, make_client, products, tmp_path):
    rules = load_rules([{"match": {"productType": "Ribbon"}, "category": "ae-2-1-2-14"}])
    plan = plan_categories(products, SHOP, rules=rules)
    shop.product("velvet-ribbon-navy")["category"] = None  # "na" -> null is not a real edit
    assert run(plan, make_client(shop), tmp_path, execute=True).applied == 1


def test_metafields_batch_and_isolate_bad_rows(shop, make_client, products, tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text("product,namespace,key,type,value\n"
                   "kraft-gift-wrap-roll,custom,width_mm,number_integer,760\n"
                   "washi-tape-set-pastel,custom,width_mm,number_integer,15\n"
                   "velvet-ribbon-navy,custom,width_mm,number_integer,25\n")
    plan = plan_metafields(products, SHOP, csv)
    shop.fail["MetafieldsSet"] = {"gid://shopify/Product/1004"}
    s = run(plan, make_client(shop), tmp_path, execute=True)
    assert (s.applied, s.failed) == (2, 1)
    calls = [len(v["metafields"]) for name, v in shop.mutations if name == "metafieldsSet"]
    assert calls == [3, 1, 1, 1]  # one batch, rejected as a whole, then row by row
    assert shop.metafield("kraft-gift-wrap-roll", "custom.width_mm")["value"] == "760"
    assert shop.metafield("washi-tape-set-pastel", "custom.width_mm") is None


def test_metafield_drift(shop, make_client, products, tmp_path):
    csv = tmp_path / "m.csv"
    csv.write_text("product,namespace,key,type,value\nsatin-ribbon-blush,custom,care,single_line_text_field,Cool iron\n")
    plan = plan_metafields(products, SHOP, csv)
    shop.metafield("satin-ribbon-blush", "custom.care")["value"] = "Dry clean only"
    s = run(plan, make_client(shop), tmp_path, execute=True)
    assert s.drifted == 1
    assert "value changed since the dry run" in s.problems[0]


def test_redirects_create_and_update(shop, make_client, tmp_path):
    csv = tmp_path / "r.csv"
    csv.write_text("path,target\n/collections/ribbons-old,/collections/ribbons\n/pages/about-us,/pages/about\n")
    plan = plan_redirects(list(shop.redirects.values()), SHOP, csv)
    s = run(plan, make_client(shop), tmp_path, execute=True)
    assert s.applied == 2
    assert shop.redirects["/collections/ribbons-old"]["target"] == "/collections/ribbons"
    assert shop.redirects["/pages/about-us"]["target"] == "/pages/about"
    assert [m[0] for m in shop.mutations] == ["urlRedirectUpdate", "urlRedirectCreate"]


def test_plan_for_another_shop_is_refused(shop, make_client, products, tmp_path):
    plan = plan_tags(products, "other-shop.myshopify.com", add=["x"])
    with pytest.raises(PlanError, match="made for other-shop"):
        run(plan, make_client(shop), tmp_path, execute=True)


def test_stale_plan_is_refused(shop, make_client, products, tmp_path):
    plan = plan_tags(products, SHOP, add=["x"])
    plan.created_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    with pytest.raises(PlanError, match="30.0h old"):
        run(plan, make_client(shop), tmp_path, execute=True)
    assert run(plan, make_client(shop), tmp_path, execute=True, max_age_hours=None).applied == 6


def test_journal_from_a_different_plan_is_refused(shop, make_client, products, tmp_path):
    run(plan_tags(products, SHOP, add=["x"]), make_client(shop), tmp_path, execute=True, limit=1)
    with pytest.raises(PlanError, match="belongs to a different plan"):
        run(plan_tags(products, SHOP, add=["y"]), make_client(shop), tmp_path, execute=True)
