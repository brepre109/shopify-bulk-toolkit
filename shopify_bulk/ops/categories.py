"""Product category (Shopify Standard Product Taxonomy) backfill."""
from __future__ import annotations

import re
from pathlib import Path

from ..client import GraphQLClient
from ..matching import Rule
from ..plan import Change, Plan
from ..queries import PRODUCT_SET_CATEGORY
from ..snapshot import UNCATEGORIZED_IDS, has_category
from . import ALREADY_APPLIED, APPLY, DRIFTED, Decision
from ._products import ProductIndex, product_state, read_csv

TAXONOMY_ID = re.compile(r"^[a-z]{2}(-\d+)*$")


def category_gid(value: str) -> str:
    """Accept a full gid or a bare taxonomy id such as ``aa-1-13``."""
    value = value.strip()
    if value.startswith("gid://shopify/TaxonomyCategory/"):
        return value
    if TAXONOMY_ID.match(value):
        return f"gid://shopify/TaxonomyCategory/{value}"
    raise ValueError(f"not a taxonomy category id: '{value}'")


def _current_id(product: dict) -> str | None:
    return product["category"]["id"] if has_category(product) else None


def plan_categories(
    products: list[dict], shop: str, *, rules: list[Rule] | None = None, csv_path: Path | None = None,
    overwrite: bool = False,
) -> Plan:
    """Rules are tried in order and the first match wins. CSV rows beat rules.

    Only products with no category (null *or* the literal "Uncategorized" node)
    are touched unless ``overwrite`` is set.
    """
    warnings: list[str] = []
    explicit: dict[str, tuple[str, str]] = {}
    if csv_path:
        index = ProductIndex(products)
        for n, row in enumerate(read_csv(csv_path, {"product", "category"}), start=2):
            p = index.resolve(row["product"])
            if not p:
                warnings.append(f"{csv_path.name}:{n}: product '{row['product']}' not in snapshot, skipped")
                continue
            try:
                explicit[p["id"]] = (category_gid(row["category"]), f"csv line {n}")
            except ValueError as exc:
                warnings.append(f"{csv_path.name}:{n}: {exc}")

    parsed_rules = []
    for rule in rules or []:
        try:
            parsed_rules.append((rule, category_gid(str(rule.payload.get("category", "")))))
        except ValueError as exc:
            raise ValueError(f"{rule.describe()}: {exc}") from exc

    changes, unmatched = [], 0
    for p in products:
        current = _current_id(p)
        if current and not overwrite:
            continue
        target, source = explicit.get(p["id"], (None, None))
        if target is None:
            for rule, gid in parsed_rules:
                if rule.matches(p):
                    target, source = gid, rule.describe()
                    break
        if target is None:
            if not current:
                unmatched += 1
            continue
        if target == current:
            continue
        changes.append(Change(
            id=f"category:{p['id']}", action="update", resource=p["id"], label=f"{p['handle']}  [{source}]",
            before={"category": (p["category"] or {}).get("id")}, after={"category": target},
        ))
    if unmatched:
        warnings.append(f"{unmatched} uncategorized product(s) matched no rule")
    changes.sort(key=lambda c: c.label)
    return Plan(kind="categories", shop=shop, changes=changes, warnings=warnings,
                params={"rules": len(rules or []), "csv": str(csv_path) if csv_path else None,
                        "overwrite": overwrite})


def _empty(cat_id: str | None) -> bool:
    return not cat_id or cat_id in UNCATEGORIZED_IDS


class CategoriesHandler:
    batch_size = 1

    def fetch_current(self, client: GraphQLClient, changes: list[Change]) -> dict[str, object]:
        state = product_state(client, [c.resource for c in changes])
        out: dict[str, object] = {}
        for c in changes:
            node = state.get(c.resource)
            out[c.id] = None if node is None else {"category": (node.get("category") or {}).get("id")}
        return out

    def decide(self, change: Change, current) -> Decision:
        if current is None:
            return Decision(DRIFTED, "product no longer exists", forceable=False)
        live, before = current["category"], change.before["category"]
        if live == change.after["category"]:
            return Decision(ALREADY_APPLIED)
        if live != before and not (_empty(live) and _empty(before)):
            return Decision(DRIFTED, f"category is now {live}")
        return Decision(APPLY)

    def execute(self, client: GraphQLClient, items) -> list[str | None]:
        errors: list[str | None] = []
        for change, _ in items:
            try:
                client.mutate(PRODUCT_SET_CATEGORY,
                              {"product": {"id": change.resource, "category": change.after["category"]}},
                              "productUpdate")
                errors.append(None)
            except Exception as exc:
                errors.append(str(exc))
        return errors
