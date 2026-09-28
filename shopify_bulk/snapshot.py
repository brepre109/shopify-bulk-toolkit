"""Catalog snapshots: the "before" state every plan is computed against.

A snapshot is a JSONL file of normalized product records. It is produced by a
bulk query (``shopify-bulk export products``) or loaded from an earlier export,
so planning never has to hammer the API and can be reviewed offline.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Iterator

from .bulk import run_bulk_query
from .client import GraphQLClient
from .queries import BULK_REDIRECTS, bulk_products_query

# Shopify's taxonomy has a real node whose name is literally "Uncategorized".
# It renders exactly like "no category" in the admin, so audits treat both as empty.
UNCATEGORIZED_IDS = {"gid://shopify/TaxonomyCategory/na"}


def normalize_product(raw: dict) -> dict:
    category = raw.get("category") or None
    metafields = {}
    for mf in raw.get("metafields", []):
        metafields[f"{mf['namespace']}.{mf['key']}"] = {"type": mf.get("type"), "value": mf.get("value")}
    return {
        "id": raw["id"],
        "handle": raw.get("handle", ""),
        "title": raw.get("title", ""),
        "status": raw.get("status", ""),
        "vendor": raw.get("vendor", ""),
        "productType": raw.get("productType", ""),
        "tags": sorted(raw.get("tags") or []),
        "category": {"id": category["id"], "name": category.get("fullName") or category.get("name", "")}
        if category
        else None,
        "variants": [
            {"id": v["id"], "sku": v.get("sku") or "", "title": v.get("title", ""), "price": v.get("price")}
            for v in raw.get("variants", [])
        ],
        "metafields": metafields,
    }


def has_category(product: dict) -> bool:
    cat = product.get("category")
    return bool(cat and cat.get("id") and cat["id"] not in UNCATEGORIZED_IDS)


def export_products(client: GraphQLClient, *, search: str | None = None,
                    metafields: bool | str = False, on_progress=None) -> Iterator[dict]:
    query = bulk_products_query(search, metafields)
    for raw in run_bulk_query(client, query, on_progress=on_progress):
        yield normalize_product(raw)


def export_redirects(client: GraphQLClient, *, on_progress=None) -> Iterator[dict]:
    for raw in run_bulk_query(client, BULK_REDIRECTS, on_progress=on_progress):
        yield {"id": raw["id"], "path": raw["path"], "target": raw["target"]}


def write_jsonl(records: Iterable[dict], path: Path) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_products_csv(products: Iterable[dict], path: Path) -> int:
    """One row per variant (products without variants get one row)."""
    cols = ["product_id", "handle", "title", "status", "vendor", "product_type", "tags",
            "category_id", "category", "variant_id", "sku", "variant_title", "price"]
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for p in products:
            base = {
                "product_id": p["id"], "handle": p["handle"], "title": p["title"], "status": p["status"],
                "vendor": p["vendor"], "product_type": p["productType"], "tags": ", ".join(p["tags"]),
                "category_id": (p["category"] or {}).get("id", ""), "category": (p["category"] or {}).get("name", ""),
            }
            for v in p["variants"] or [{}]:
                w.writerow({**base, "variant_id": v.get("id", ""), "sku": v.get("sku", ""),
                            "variant_title": v.get("title", ""), "price": v.get("price", "")})
                n += 1
    return n
