"""Tag add/remove.

Writes use ``tagsAdd`` / ``tagsRemove`` with only the delta, never a full tag
list overwrite, so a concurrent edit to other tags is never lost. That also
makes re-applying a plan idempotent.
"""
from __future__ import annotations

from pathlib import Path

from ..client import GraphQLClient
from ..matching import Rule
from ..plan import Change, Plan
from ..queries import TAGS_ADD, TAGS_REMOVE
from . import ALREADY_APPLIED, APPLY, DRIFTED, Decision
from ._products import ProductIndex, product_state, read_csv, split_list


def _change(product: dict, add: set[str], remove: set[str]) -> Change | None:
    current = set(product["tags"])
    # Shopify tags are case-insensitive: "Sale" and "sale" are the same tag.
    lower = {t.casefold(): t for t in current}
    add = {t for t in add if t.casefold() not in lower}
    remove = {lower[t.casefold()] for t in remove if t.casefold() in lower}
    if not add and not remove:
        return None
    after = sorted((current - remove) | add)
    return Change(
        id=f"tags:{product['id']}", action="update", resource=product["id"], label=product["handle"],
        before={"tags": sorted(current)}, after={"tags": after},
    )


def plan_tags(
    products: list[dict], shop: str, *, add: list[str] = (), remove: list[str] = (),
    rules: list[Rule] | None = None, csv_path: Path | None = None, filters: list[Rule] | None = None,
) -> Plan:
    """Build a tag plan from global --add/--remove, rules, or a per-product CSV.

    ``filters`` (optional) restrict which products global add/remove touches.
    """
    warnings: list[str] = []
    per_product: dict[str, tuple[set[str], set[str]]] = {}

    def want(pid: str, a=(), r=()):
        cur_a, cur_r = per_product.setdefault(pid, (set(), set()))
        cur_a.update(a)
        cur_r.update(r)

    if add or remove:
        for p in products:
            if filters and not all(f.matches(p) for f in filters):
                continue
            want(p["id"], add, remove)

    for rule in rules or []:
        tags = rule.payload.get("add") or []
        untags = rule.payload.get("remove") or []
        hits = [p for p in products if rule.matches(p)]
        if not hits:
            warnings.append(f"{rule.describe()} matched no products")
        for p in hits:
            want(p["id"], tags, untags)

    if csv_path:
        index = ProductIndex(products)
        for n, row in enumerate(read_csv(csv_path, {"product"}), start=2):
            p = index.resolve(row["product"])
            if not p:
                warnings.append(f"{csv_path.name}:{n}: product '{row['product']}' not in snapshot, skipped")
                continue
            want(p["id"], split_list(row.get("add", "")), split_list(row.get("remove", "")))

    by_id = {p["id"]: p for p in products}
    changes = []
    for pid, (a, r) in per_product.items():
        overlap = {t for t in a if t.casefold() in {x.casefold() for x in r}}
        if overlap:
            warnings.append(f"{by_id[pid]['handle']}: tag(s) both added and removed, add wins: {sorted(overlap)}")
            r = {t for t in r if t.casefold() not in {x.casefold() for x in overlap}}
        c = _change(by_id[pid], a, r)
        if c:
            changes.append(c)
    changes.sort(key=lambda c: c.label)
    return Plan(kind="tags", shop=shop, changes=changes, warnings=warnings,
                params={"add": list(add), "remove": list(remove), "rules": len(rules or []),
                        "csv": str(csv_path) if csv_path else None})


class TagsHandler:
    batch_size = 1

    def fetch_current(self, client: GraphQLClient, changes: list[Change]) -> dict[str, object]:
        state = product_state(client, [c.resource for c in changes])
        return {c.id: ({"tags": sorted(state[c.resource]["tags"])} if c.resource in state else None) for c in changes}

    def decide(self, change: Change, current) -> Decision:
        if current is None:
            return Decision(DRIFTED, "product no longer exists", forceable=False)
        before, after = set(change.before["tags"]), set(change.after["tags"])
        live = {t.casefold(): t for t in current["tags"]}
        add = sorted(t for t in after - before if t.casefold() not in live)
        remove = sorted(live[t.casefold()] for t in before - after if t.casefold() in live)
        if not add and not remove:
            return Decision(ALREADY_APPLIED)
        return Decision(APPLY, work={"add": add, "remove": remove})

    def execute(self, client: GraphQLClient, items) -> list[str | None]:
        errors: list[str | None] = []
        for change, decision in items:
            try:
                if decision.work["add"]:
                    client.mutate(TAGS_ADD, {"id": change.resource, "tags": decision.work["add"]}, "tagsAdd")
                if decision.work["remove"]:
                    client.mutate(TAGS_REMOVE, {"id": change.resource, "tags": decision.work["remove"]}, "tagsRemove")
                errors.append(None)
            except Exception as exc:  # recorded in the journal, run continues
                errors.append(str(exc))
        return errors
