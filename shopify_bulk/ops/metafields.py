"""Product metafield set from a CSV (product, namespace, key, type, value)."""
from __future__ import annotations

import json
from pathlib import Path

from ..client import GraphQLClient, UserErrors
from ..plan import Change, Plan
from ..queries import METAFIELDS_SET, json_string
from . import ALREADY_APPLIED, APPLY, DRIFTED, Decision, chunks
from ._products import ProductIndex, read_csv

JSON_TYPES = {"json", "rich_text_field", "rating", "dimension", "volume", "weight", "money", "link"}


def validate_value(mtype: str, value: str) -> str | None:
    """Return an error message if ``value`` cannot be valid for ``mtype``."""
    try:
        if mtype in JSON_TYPES or mtype.startswith("list."):
            parsed = json.loads(value)
            if mtype.startswith("list.") and not isinstance(parsed, list):
                return f"{mtype} needs a JSON array"
        elif mtype == "number_integer":
            int(value)
        elif mtype == "number_decimal":
            float(value)
        elif mtype == "boolean":
            if value not in {"true", "false"}:
                return "boolean must be 'true' or 'false'"
        elif mtype == "single_line_text_field":
            if "\n" in value:
                return "single_line_text_field cannot contain newlines"
    except ValueError as exc:
        return f"invalid {mtype}: {exc}"
    return None


def same_value(mtype: str, a: str | None, b: str | None) -> bool:
    """Compare values the way Shopify stores them (JSON is compared parsed)."""
    if a is None or b is None:
        return a == b
    if mtype in JSON_TYPES or mtype.startswith("list."):
        try:
            return json.loads(a) == json.loads(b)
        except ValueError:
            return a == b
    return a == b


def plan_metafields(products: list[dict], shop: str, csv_path: Path) -> Plan:
    index = ProductIndex(products)
    warnings: list[str] = []
    changes: dict[str, Change] = {}
    for n, row in enumerate(read_csv(csv_path, {"product", "namespace", "key", "type", "value"}), start=2):
        where = f"{csv_path.name}:{n}"
        p = index.resolve(row["product"])
        if not p:
            warnings.append(f"{where}: product '{row['product']}' not in snapshot, skipped")
            continue
        ns, key, mtype, value = row["namespace"], row["key"], row["type"], row["value"]
        if not ns or not key or not mtype:
            warnings.append(f"{where}: namespace, key and type are required, skipped")
            continue
        problem = validate_value(mtype, value)
        if problem:
            warnings.append(f"{where}: {problem}, skipped")
            continue
        existing = p["metafields"].get(f"{ns}.{key}")
        if existing and existing.get("type") and existing["type"] != mtype:
            warnings.append(
                f"{where}: {p['handle']} {ns}.{key} is {existing['type']}, CSV says {mtype}; skipped "
                "(change the metafield definition first)"
            )
            continue
        old = existing.get("value") if existing else None
        if same_value(mtype, old, value):
            continue
        cid = f"mf:{p['id']}:{ns}.{key}"
        if cid in changes:
            warnings.append(f"{where}: duplicate row for {p['handle']} {ns}.{key}, last one wins")
        changes[cid] = Change(
            id=cid, action="update" if existing else "create", resource=p["id"],
            label=f"{p['handle']} {ns}.{key}",
            before={"value": old}, after={"value": value, "type": mtype, "namespace": ns, "key": key},
        )
    return Plan(kind="metafields", shop=shop, changes=sorted(changes.values(), key=lambda c: c.label),
                warnings=warnings, params={"csv": str(csv_path)})


class MetafieldsHandler:
    batch_size = 25  # metafieldsSet accepts up to 25 metafields per call

    def fetch_current(self, client: GraphQLClient, changes: list[Change]) -> dict[str, object]:
        out: dict[str, object] = {}
        for batch in chunks(changes, 25):
            fields = []
            for i, c in enumerate(batch):
                fields.append(
                    f"c{i}: product(id: {json_string(c.resource)}) {{ id "
                    f"metafield(namespace: {json_string(c.after['namespace'])}, key: {json_string(c.after['key'])})"
                    " { type value } }"
                )
            data = client.execute("query MetafieldState {\n  " + "\n  ".join(fields) + "\n}",
                                  estimated_cost=2 * len(batch) + 1)
            for i, c in enumerate(batch):
                node = data.get(f"c{i}")
                if node is None:
                    out[c.id] = None
                else:
                    mf = node.get("metafield")
                    out[c.id] = {"value": mf["value"] if mf else None}
        return out

    def decide(self, change: Change, current) -> Decision:
        if current is None:
            return Decision(DRIFTED, "product no longer exists", forceable=False)
        mtype = change.after["type"]
        if same_value(mtype, current["value"], change.after["value"]):
            return Decision(ALREADY_APPLIED)
        if not same_value(mtype, current["value"], change.before["value"]):
            return Decision(DRIFTED, "value changed since the dry run")
        return Decision(APPLY)

    @staticmethod
    def _input(change: Change) -> dict:
        a = change.after
        return {"ownerId": change.resource, "namespace": a["namespace"], "key": a["key"],
                "type": a["type"], "value": a["value"]}

    def execute(self, client: GraphQLClient, items) -> list[str | None]:
        inputs = [self._input(c) for c, _ in items]
        try:
            client.mutate(METAFIELDS_SET, {"metafields": inputs}, "metafieldsSet", estimated_cost=10)
            return [None] * len(items)
        except UserErrors:
            if len(items) == 1:
                raise
        # One bad value rejects the whole call: retry one by one to isolate it.
        errors: list[str | None] = []
        for single in inputs:
            try:
                client.mutate(METAFIELDS_SET, {"metafields": [single]}, "metafieldsSet", estimated_cost=10)
                errors.append(None)
            except Exception as exc:
                errors.append(str(exc))
        return errors
