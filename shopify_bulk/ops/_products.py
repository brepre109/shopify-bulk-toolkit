"""Shared helpers for product-based operations."""
from __future__ import annotations

import csv
from pathlib import Path

from ..client import GraphQLClient
from ..queries import PRODUCTS_STATE
from . import chunks


def product_state(client: GraphQLClient, ids: list[str]) -> dict[str, dict]:
    """Current tags and category for product gids (50 per request)."""
    out: dict[str, dict] = {}
    for batch in chunks(sorted(set(ids)), 50):
        data = client.execute(PRODUCTS_STATE, {"ids": batch}, estimated_cost=len(batch) + 2)
        for node in data.get("nodes") or []:
            if node:
                out[node["id"]] = node
    return out


class ProductIndex:
    """Resolve CSV product references (gid, numeric id or handle) against a snapshot."""

    def __init__(self, products: list[dict]):
        self.by_id = {p["id"]: p for p in products}
        self.by_handle = {p["handle"]: p for p in products}

    def resolve(self, ref: str) -> dict | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith("gid://"):
            return self.by_id.get(ref)
        if ref.isdigit():
            return self.by_id.get(f"gid://shopify/Product/{ref}")
        return self.by_handle.get(ref)


def read_csv(path: Path, required: set[str]) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing column(s): {', '.join(sorted(missing))}")
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]


def split_list(value: str) -> list[str]:
    """Split a tag list on commas or semicolons, dropping blanks."""
    return [t.strip() for t in value.replace(";", ",").split(",") if t.strip()]
