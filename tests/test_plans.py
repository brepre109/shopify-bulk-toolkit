import json

import pytest

from conftest import SHOP
from shopify_bulk.matching import RuleError, load_rules
from shopify_bulk.ops.categories import category_gid, plan_categories
from shopify_bulk.ops.metafields import plan_metafields
from shopify_bulk.ops.redirects import normalize_path, plan_redirects
from shopify_bulk.ops.tags import plan_tags
from shopify_bulk.snapshot import has_category


def by_label(plan):
    return {c.label.split("  [")[0]: c for c in plan.changes}


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


# --- snapshot -----------------------------------------------------------------

def test_both_kinds_of_uncategorized_count_as_empty(products):
    cats = {p["handle"]: has_category(p) for p in products}
    assert cats["kraft-gift-wrap-roll"] is False  # category: null
    assert cats["velvet-ribbon-navy"] is False  # the literal "Uncategorized" taxonomy node
    assert cats["satin-ribbon-blush"] is True


# --- tags -----------------------------------------------------------------------

def test_global_add_with_filter_only_touches_matching_products(products):
    where = load_rules([{"match": {"productType": "ribbon"}}])
    plan = plan_tags(products, SHOP, add=["craft-supplies"], filters=where)
    assert sorted(by_label(plan)) == ["satin-ribbon-blush", "velvet-ribbon-navy"]
    c = by_label(plan)["velvet-ribbon-navy"]
    assert c.before == {"tags": ["ribbon"]} and c.after == {"tags": ["craft-supplies", "ribbon"]}


def test_existing_tags_are_matched_case_insensitively(products):
    plan = plan_tags(products, SHOP, add=["sale"])
    assert "satin-ribbon-blush" not in by_label(plan)  # already has "Sale"
    plan = plan_tags(products, SHOP, remove=["sale"])
    assert by_label(plan)["satin-ribbon-blush"].after == {"tags": ["ribbon"]}
    assert len(plan.changes) == 1


def test_rules_use_word_boundaries_the_author_writes(products):
    naive = load_rules([{"match": {"title": "rave"}, "add": ["party"]}])
    assert "unraveling-yarn-guide" in by_label(plan_tags(products, SHOP, rules=naive))
    strict = load_rules([{"match": {"title": r"\brave\b"}, "add": ["party"]}])
    plan = plan_tags(products, SHOP, rules=strict)
    assert plan.changes == []
    assert any("matched no products" in w for w in plan.warnings)


def test_csv_per_product_and_unknown_products_warn(products, tmp_path):
    csv = write(tmp_path, "t.csv", "product,add,remove\nwashi-tape-set-pastel,pastel; tape,\n1001,,kraft\nnope,x,\n")
    plan = plan_tags(products, SHOP, csv_path=csv)
    labels = by_label(plan)
    assert labels["washi-tape-set-pastel"].after == {"tags": ["pastel", "tape"]}
    assert labels["kraft-gift-wrap-roll"].after == {"tags": ["gift-wrap"]}
    assert any("'nope' not in snapshot" in w for w in plan.warnings)


def test_add_and_remove_same_tag_warns_and_add_wins(products):
    plan = plan_tags(products, SHOP, add=["ribbon"], remove=["ribbon"],
                     filters=load_rules([{"match": {"handle": "^velvet"}}]))
    assert plan.changes == []  # velvet already has "ribbon"; removal cancelled
    assert any("both added and removed" in w for w in plan.warnings)


def test_bad_rules_fail_loudly():
    with pytest.raises(RuleError, match="unknown match field"):
        load_rules([{"match": {"colour": "red"}}])
    with pytest.raises(RuleError, match="bad regex"):
        load_rules([{"match": {"title": "("}}])


# --- metafields -----------------------------------------------------------------

def test_metafields_plan_create_update_skip_and_validation(products, tmp_path):
    csv = write(tmp_path, "m.csv", "\n".join([
        "product,namespace,key,type,value",
        "kraft-gift-wrap-roll,custom,care,single_line_text_field,Store dry",  # unchanged -> skipped
        "satin-ribbon-blush,custom,care,single_line_text_field,Cool iron",  # update
        "washi-tape-set-pastel,custom,width_mm,number_integer,15",  # create
        "washi-tape-set-pastel,custom,rolls,number_integer,six",  # invalid
        'velvet-ribbon-navy,tiered_pricing,tiers,json,"{""retail"": [{""minQuantity"": 10, ""percentage"": 10}]}"',
        "glitter-cardstock-gold,custom,paper_weight_gsm,single_line_text_field,300",  # type mismatch
    ]) + "\n")
    plan = plan_metafields(products, SHOP, csv)
    labels = by_label(plan)
    assert sorted(labels) == ["satin-ribbon-blush custom.care", "velvet-ribbon-navy tiered_pricing.tiers",
                              "washi-tape-set-pastel custom.width_mm"]
    assert labels["satin-ribbon-blush custom.care"].action == "update"
    assert labels["satin-ribbon-blush custom.care"].before == {"value": "Hand wash"}
    assert labels["washi-tape-set-pastel custom.width_mm"].action == "create"
    assert any("invalid number_integer" in w for w in plan.warnings)
    assert any("is number_integer, CSV says single_line_text_field" in w for w in plan.warnings)


# --- categories -----------------------------------------------------------------

RULES = [
    {"match": {"productType": "Ribbon"}, "category": "ae-2-1-2-14"},
    {"match": {"title": r"\bgift wrap\b"}, "category": "gid://shopify/TaxonomyCategory/ae-2-1-2-8"},
    {"match": {"vendor": "Northwind Paper"}, "category": "ae-2-1-2"},
]


def test_categories_fill_null_and_uncategorized_first_rule_wins(products):
    plan = plan_categories(products, SHOP, rules=load_rules(RULES))
    got = {k: c.after["category"].rsplit("/", 1)[-1] for k, c in by_label(plan).items()}
    assert got == {
        "velvet-ribbon-navy": "ae-2-1-2-14",  # was the literal Uncategorized node
        "kraft-gift-wrap-roll": "ae-2-1-2-8",  # title rule outranks the vendor rule below it
        "washi-tape-set-pastel": "ae-2-1-2",
        "glitter-cardstock-gold": "ae-2-1-2",
    }
    assert "satin-ribbon-blush" not in got  # already categorized
    assert any("1 uncategorized product(s) matched no rule" in w for w in plan.warnings)  # the book


def test_categories_overwrite_and_csv_precedence(products, tmp_path):
    csv = write(tmp_path, "c.csv", "product,category\nunraveling-yarn-guide,me-1-3\nsatin-ribbon-blush,ae-2-1-2-99\n")
    plan = plan_categories(products, SHOP, rules=load_rules(RULES), csv_path=csv)
    assert "satin-ribbon-blush" not in by_label(plan)  # has a category, no --overwrite
    assert by_label(plan)["unraveling-yarn-guide"].after["category"].endswith("/me-1-3")
    plan = plan_categories(products, SHOP, rules=load_rules(RULES), csv_path=csv, overwrite=True)
    assert by_label(plan)["satin-ribbon-blush"].after["category"].endswith("/ae-2-1-2-99")


def test_category_ids_are_validated():
    assert category_gid("aa-1-13") == "gid://shopify/TaxonomyCategory/aa-1-13"
    with pytest.raises(ValueError):
        category_gid("Ribbons")


# --- redirects ------------------------------------------------------------------

EXISTING = [json.loads(line) for line in
            open(__file__.replace("test_plans.py", "fixtures/bulk_redirects.jsonl")).read().splitlines()]


def test_redirect_path_normalization():
    assert normalize_path("https://shop.example.com/pages/old/?a=1") == "/pages/old?a=1"
    assert normalize_path("pages/old/") == "/pages/old"
    assert normalize_path("/") == "/"


def test_redirect_plan_create_update_skip_and_warnings(tmp_path):
    csv = write(tmp_path, "r.csv", "\n".join([
        "path,target",
        "/products/old-kraft-wrap,/products/kraft-gift-wrap-roll",  # identical -> skip
        "/collections/ribbons-old,/collections/ribbons",  # update
        "https://shop.example.com/pages/about-us/,/pages/about",  # create, URL normalized
        "/pages/a,/pages/b",
        "/pages/b,/pages/a",  # loop
        "/blogs/news/old,/collections/ribbons-old",  # chain through an existing redirect
        "/pages/self,/pages/self",
    ]) + "\n")
    plan = plan_redirects(EXISTING, SHOP, csv)
    labels = by_label(plan)
    assert "/products/old-kraft-wrap" not in labels
    assert labels["/collections/ribbons-old"].action == "update"
    assert labels["/collections/ribbons-old"].before["redirect_id"] == "gid://shopify/UrlRedirect/502"
    assert labels["/pages/about-us"].action == "create"
    assert any(w.startswith("loop: /pages/a") for w in plan.warnings)
    assert any("chain: /blogs/news/old -> /collections/ribbons-old -> /collections/ribbons" in w for w in plan.warnings)
    assert any("redirects to itself" in w for w in plan.warnings)
