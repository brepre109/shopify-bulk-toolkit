import csv
import json

import pytest

from shopify_bulk.cli import main


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOPIFY_SHOP", "example-paper.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "test-token")
    monkeypatch.chdir(tmp_path)
    # the real client would sleep between bulk polls
    monkeypatch.setattr("shopify_bulk.client.time.sleep", lambda s: None)


def test_export_products_csv(shop, tmp_path, capsys):
    assert main(["export", "products", "--format", "csv", "--out", "p.csv"], transport=shop) == 0
    rows = list(csv.DictReader(open(tmp_path / "p.csv")))
    assert len(rows) == 7  # one row per variant
    assert rows[1]["sku"] == "RBN-VEL-NV-10" and rows[1]["category"] == "Uncategorized"
    assert "Wrote 7 variant rows" in capsys.readouterr().out


def test_export_with_search_and_metafields(shop, tmp_path):
    main(["export", "products", "--search", "status:draft", "--metafields", "custom", "--out", "d.jsonl"],
         transport=shop)
    lines = [json.loads(line) for line in open(tmp_path / "d.jsonl")]
    assert [p["handle"] for p in lines] == ["glitter-cardstock-gold"]
    assert lines[0]["metafields"] == {"custom.paper_weight_gsm": {"type": "number_integer", "value": "300"}}


def test_planning_requires_dry_run_flag(shop):
    with pytest.raises(SystemExit) as exc:
        main(["tags", "--add", "x", "--plan", "p.json"], transport=shop)
    assert exc.value.code == 2


def test_planning_commands_refuse_apply(shop, capsys):
    assert main(["tags", "--add", "x", "--dry-run", "--apply", "--plan", "p.json"], transport=shop) == 2
    assert "planning commands never write" in capsys.readouterr().err
    assert shop.calls == []


def test_full_flow_dry_run_preflight_apply(shop, tmp_path, capsys):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"match": {"productType": "Ribbon"}, "add": ["ribbon-shop"]}]))
    assert main(["tags", "--rules", str(rules), "--dry-run", "--plan", "plans/ribbon.json"], transport=shop) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: nothing was written" in out and "+ tags: ribbon-shop" in out
    assert (tmp_path / "plans/ribbon.snapshot.jsonl").exists()
    assert not any(c.startswith("Tags") for c in shop.calls)

    assert main(["apply", "plans/ribbon.json"], transport=shop) == 0
    assert "Preflight: total=2 would_apply=2" in capsys.readouterr().out
    assert shop.mutations == []

    assert main(["apply", "plans/ribbon.json", "--apply"], transport=shop) == 0
    assert "Done: total=2 applied=2" in capsys.readouterr().out
    assert "ribbon-shop" in shop.product("velvet-ribbon-navy")["tags"]

    assert main(["show", "plans/ribbon.json"], transport=shop) == 0


def test_categories_from_snapshot_without_live_export(shop, tmp_path, capsys):
    main(["export", "products", "--out", "snap.jsonl"], transport=shop)
    shop.calls.clear()
    rules = tmp_path / "c.json"
    rules.write_text(json.dumps([{"match": {"vendor": "Northwind Paper"}, "category": "ae-2-1-2"}]))
    assert main(["categories", "--rules", str(rules), "--snapshot", "snap.jsonl", "--dry-run",
                 "--plan", "c.json"], transport=shop) == 0
    assert shop.calls == []  # planning from a snapshot needs no API calls
    assert "Actions: update=3" in capsys.readouterr().out


def test_metafields_and_redirects_commands(shop, tmp_path, capsys):
    (tmp_path / "m.csv").write_text("product,namespace,key,type,value\nwashi-tape-set-pastel,custom,width_mm,number_integer,15\n")
    assert main(["metafields", "--csv", "m.csv", "--dry-run", "--plan", "m.json"], transport=shop) == 0
    assert main(["apply", "m.json", "--apply"], transport=shop) == 0
    assert shop.metafield("washi-tape-set-pastel", "custom.width_mm")["value"] == "15"

    (tmp_path / "r.csv").write_text("path,target\n/pages/about-us,/pages/about\n")
    assert main(["redirects", "--csv", "r.csv", "--dry-run", "--plan", "r.json"], transport=shop) == 0
    assert main(["apply", "r.json", "--apply"], transport=shop) == 0
    assert shop.redirects["/pages/about-us"]["target"] == "/pages/about"


def test_tampered_plan_is_rejected(shop, tmp_path, capsys):
    main(["tags", "--add", "x", "--dry-run", "--plan", "p.json"], transport=shop)
    data = json.loads((tmp_path / "p.json").read_text())
    data["changes"] = data["changes"][:1]
    (tmp_path / "p.json").write_text(json.dumps(data))
    assert main(["apply", "p.json", "--apply"], transport=shop) == 2
    assert "fingerprint mismatch" in capsys.readouterr().err


def test_missing_credentials(monkeypatch, shop):
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN")
    with pytest.raises(SystemExit, match="SHOPIFY_ACCESS_TOKEN"):
        main(["export", "products", "--out", "x.jsonl", "--env-file", "missing.env"], transport=shop)
