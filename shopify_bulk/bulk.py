"""Bulk query operations: start, poll, download, and reassemble JSONL.

Bulk queries do not consume the GraphQL cost bucket, so they are the right tool
for reading a whole catalog. Results arrive as JSONL where nested connection
nodes are separate lines pointing at their parent through ``__parentId``;
:func:`reassemble` turns that back into nested objects.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Iterable, Iterator

from .client import GraphQLClient, ShopifyError, UserErrors

RUN_QUERY = """
mutation RunBulkQuery($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""

# API 2026-01+: poll a specific operation (currentBulkOperation is deprecated).
POLL = """
query PollBulkOperation($id: ID!) {
  bulkOperation(id: $id) {
    id status errorCode objectCount fileSize url partialDataUrl
  }
}
"""

TERMINAL = {"COMPLETED", "FAILED", "CANCELED", "EXPIRED"}

# Child node type (from its gid) -> key it is nested under on the parent.
CHILD_KEYS = {
    "ProductVariant": "variants",
    "Metafield": "metafields",
    "MediaImage": "media",
    "Collection": "collections",
}


def gid_type(gid: str) -> str:
    # gid://shopify/ProductVariant/123 -> ProductVariant
    parts = gid.split("/")
    return parts[3] if len(parts) > 4 else ""


def reassemble(lines: Iterable[str]) -> Iterator[dict]:
    """Yield top-level objects with their children nested under list keys.

    Shopify writes children after their parent, so a parent is complete when
    the next top-level object starts; memory stays bounded by one object tree.
    The list key comes from the child's gid type (see ``CHILD_KEYS``).
    """
    current: dict | None = None
    index: dict[str, dict] = {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        parent_id = obj.pop("__parentId", None)
        if parent_id is None:
            if current is not None:
                yield current
            current = obj
            index = {obj["id"]: obj} if "id" in obj else {}
            continue
        parent = index.get(parent_id)
        if parent is None:
            raise ShopifyError(f"bulk JSONL child references unknown parent {parent_id}")
        key = CHILD_KEYS.get(gid_type(obj.get("id", "")), "children")
        parent.setdefault(key, []).append(obj)
        if "id" in obj:
            index[obj["id"]] = obj
    if current is not None:
        yield current


def run_bulk_query(
    client: GraphQLClient,
    query: str,
    *,
    poll_interval: float = 2.0,
    max_poll_interval: float = 15.0,
    timeout: float = 3600.0,
    on_progress: Callable[[dict], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[dict]:
    """Run ``query`` as a bulk operation and yield reassembled top-level objects."""
    try:
        started = client.mutate(RUN_QUERY, {"query": query}, "bulkOperationRunQuery")
    except UserErrors as exc:
        raise ShopifyError(f"bulk query rejected: {exc}") from exc
    op_id = started["bulkOperation"]["id"]

    deadline = clock() + timeout
    interval = poll_interval
    while True:
        op = client.execute(POLL, {"id": op_id}, estimated_cost=1)["bulkOperation"]
        if on_progress:
            on_progress(op)
        if op["status"] in TERMINAL:
            break
        if clock() > deadline:
            raise ShopifyError(f"bulk operation {op_id} still {op['status']} after {timeout:.0f}s")
        client.sleep(interval)
        interval = min(max_poll_interval, interval * 1.5)

    if op["status"] != "COMPLETED":
        partial = f" (partial data: {op['partialDataUrl']})" if op.get("partialDataUrl") else ""
        raise ShopifyError(f"bulk operation {op_id} {op['status']}: {op.get('errorCode')}{partial}")
    if not op.get("url"):
        return  # no matching objects
    yield from reassemble(client.download(op["url"]).splitlines())
