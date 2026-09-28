import json

import pytest

from conftest import SHOP
from shopify_bulk.ops.tags import plan_tags
from shopify_bulk.plan import Plan, PlanError, render


def test_roundtrip_and_tamper_detection(products, tmp_path):
    plan = plan_tags(products, SHOP, add=["new"])
    path = tmp_path / "p.json"
    plan.save(path)
    loaded = Plan.load(path)
    assert loaded.fingerprint() == plan.fingerprint()
    assert len(loaded.changes) == 6

    data = json.loads(path.read_text())
    data["changes"][0]["after"]["tags"].append("sneaky")
    path.write_text(json.dumps(data))
    with pytest.raises(PlanError, match="edited after the dry run"):
        Plan.load(path)


def test_render_shows_a_readable_diff(products):
    text = render(plan_tags(products, SHOP, add=["new"], remove=["kraft"]))
    assert "Plan: tags on example-paper.myshopify.com  (6 change(s)" in text
    assert "UPDATE  kraft-gift-wrap-roll\n  - tags: kraft\n  + tags: new" in text


def test_render_empty_plan(products):
    assert "Nothing to change." in render(plan_tags(products, SHOP, add=[]))
