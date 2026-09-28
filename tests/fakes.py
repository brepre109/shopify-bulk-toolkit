"""Test doubles for the HTTP transport.

* ``ReplayTransport`` replays recorded (synthetic) Admin API responses from
  ``tests/fixtures/responses`` in order, and records every request.
* ``FakeShop`` is a small in-memory store that understands exactly the
  operations this toolkit sends. It serves bulk exports built from its state
  and applies mutations to it, so tests can run export -> plan -> apply and
  then inspect the resulting catalog.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from shopify_bulk.transport import Response

FIXTURES = Path(__file__).parent / "fixtures"


def load_response(name: str) -> dict:
    return json.loads((FIXTURES / "responses" / f"{name}.json").read_text())


def op_name(query: str) -> str:
    m = re.match(r"\s*(?:query|mutation)\s+(\w+)", query)
    return m.group(1) if m else "anonymous"


def cost_ext(requested: float = 10, available: float = 1990) -> dict:
    return {"cost": {"requestedQueryCost": requested, "actualQueryCost": requested,
                     "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": available,
                                        "restoreRate": 100.0}}}


class ReplayTransport:
    def __init__(self, script: list, downloads: dict[str, str] | None = None):
        """``script`` items: fixture name, (status, headers, fixture name or None), or an Exception."""
        self.script = list(script)
        self.downloads = downloads or {}
        self.requests: list[dict] = []

    def post_json(self, url, payload, headers, timeout):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        if not self.script:
            raise AssertionError(f"unexpected request {op_name(payload['query'])}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return Response(200, {}, load_response(item))
        status, hdrs, name = item
        return Response(status, hdrs, load_response(name) if name else None, text="error page")

    def get_text(self, url, timeout):
        return self.downloads[url]


class FakeShop:
    def __init__(self):
        from shopify_bulk.bulk import reassemble

        lines = (FIXTURES / "bulk_products.jsonl").read_text().splitlines()
        self.products = {p["id"]: p for p in reassemble(lines)}
        self.redirects = {
            r["path"]: r for r in map(json.loads, (FIXTURES / "bulk_redirects.jsonl").read_text().splitlines())
        }
        self.calls: list[str] = []
        self.mutations: list[tuple[str, dict]] = []
        self.fail: dict[str, set[str]] = {}  # op name -> resource ids whose mutation returns a userError
        self._bulk: dict[str, str] = {}
        self._next_id = 9000

    # -- helpers for tests -------------------------------------------------
    def product(self, handle: str) -> dict:
        return next(p for p in self.products.values() if p["handle"] == handle)

    def metafield(self, handle: str, ns_key: str):
        ns, key = ns_key.split(".")
        for mf in self.product(handle).get("metafields", []):
            if mf["namespace"] == ns and mf["key"] == key:
                return mf
        return None

    # -- transport ---------------------------------------------------------
    def get_text(self, url, timeout):
        return self._bulk[url]

    def post_json(self, url, payload, headers, timeout):
        assert headers["X-Shopify-Access-Token"], "token header missing"
        name = op_name(payload["query"])
        self.calls.append(name)
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            raise AssertionError(f"FakeShop does not implement {name}")
        return Response(200, {}, {"data": handler(payload["query"], payload.get("variables") or {}),
                                  "extensions": cost_ext()})

    def _user_error(self, op, resource):
        return resource in self.fail.get(op, set())

    # -- bulk --------------------------------------------------------------
    def _op_RunBulkQuery(self, query, variables):
        self._next_id += 1
        op_id = f"gid://shopify/BulkOperation/{self._next_id}"
        url = f"https://storage.example.com/bulk/{self._next_id}.jsonl"
        self._bulk[url] = self._bulk_jsonl(variables["query"])
        self._bulk_state = {"id": op_id, "url": url, "polls": 0}
        return {"bulkOperationRunQuery": {"bulkOperation": {"id": op_id, "status": "CREATED"}, "userErrors": []}}

    def _op_PollBulkOperation(self, query, variables):
        st = self._bulk_state
        st["polls"] += 1
        done = st["polls"] >= 2  # RUNNING once, then COMPLETED
        return {"bulkOperation": {"id": st["id"], "status": "COMPLETED" if done else "RUNNING", "errorCode": None,
                                  "objectCount": "1", "fileSize": "1", "url": st["url"] if done else None,
                                  "partialDataUrl": None}}

    def _bulk_jsonl(self, bulk_query: str) -> str:
        if "urlRedirects" in bulk_query:
            return "".join(json.dumps(r) + "\n" for r in self.redirects.values())
        search = re.search(r"products\(query: (\"(?:[^\"\\]|\\.)*\")\)", bulk_query)
        search = json.loads(search.group(1)) if search else ""
        ns = re.search(r"metafields\(namespace: (\"[^\"]*\")\)", bulk_query)
        ns = json.loads(ns.group(1)) if ns else None
        want_mf = "metafields" in bulk_query
        out = []
        for p in self.products.values():
            if search.startswith("status:") and p["status"].lower() != search.split(":", 1)[1].lower():
                continue
            base = {k: v for k, v in p.items() if k not in ("variants", "metafields")}
            out.append(json.dumps(base))
            for v in p.get("variants", []):
                out.append(json.dumps({**v, "__parentId": p["id"]}))
            if want_mf:
                for m in p.get("metafields", []):
                    if ns is None or m["namespace"] == ns:
                        out.append(json.dumps({**m, "__parentId": p["id"]}))
        return "\n".join(out) + "\n"

    # -- reads ---------------------------------------------------------------
    def _op_ProductsState(self, query, variables):
        nodes = []
        for gid in variables["ids"]:
            p = self.products.get(gid)
            nodes.append(None if p is None else {"id": gid, "tags": list(p["tags"]),
                                                  "category": copy.deepcopy(p["category"])})
        return {"nodes": nodes}

    def _op_MetafieldState(self, query, variables):
        data = {}
        for alias, gid, ns, key in re.findall(
            r'(c\d+): product\(id: "([^"]+)"\) \{ id metafield\(namespace: "([^"]+)", key: "([^"]+)"\)', query
        ):
            p = self.products.get(gid)
            if p is None:
                data[alias] = None
                continue
            mf = next((m for m in p.get("metafields", []) if m["namespace"] == ns and m["key"] == key), None)
            data[alias] = {"id": gid, "metafield": {"type": mf["type"], "value": mf["value"]} if mf else None}
        return data

    def _op_RedirectsByPath(self, query, variables):
        paths = re.findall(r'path:"((?:[^"\\]|\\.)*)"', variables["query"])
        edges = [{"node": dict(self.redirects[p])} for p in paths if p in self.redirects]
        return {"urlRedirects": {"edges": edges}}

    # -- writes --------------------------------------------------------------
    def _op_TagsAdd(self, query, variables):
        self.mutations.append(("tagsAdd", variables))
        if self._user_error("TagsAdd", variables["id"]):
            return {"tagsAdd": {"node": None, "userErrors": [{"field": ["id"], "message": "Simulated failure"}]}}
        p = self.products[variables["id"]]
        p["tags"] = sorted(set(p["tags"]) | set(variables["tags"]))
        return {"tagsAdd": {"node": {"id": p["id"]}, "userErrors": []}}

    def _op_TagsRemove(self, query, variables):
        self.mutations.append(("tagsRemove", variables))
        p = self.products[variables["id"]]
        p["tags"] = sorted(set(p["tags"]) - set(variables["tags"]))
        return {"tagsRemove": {"node": {"id": p["id"]}, "userErrors": []}}

    def _op_MetafieldsSet(self, query, variables):
        self.mutations.append(("metafieldsSet", variables))
        bad = [m for m in variables["metafields"] if self._user_error("MetafieldsSet", m["ownerId"])]
        if bad:  # all-or-nothing, like the real mutation
            return {"metafieldsSet": {"metafields": [], "userErrors": [
                {"field": ["metafields", "0", "value"], "message": "Simulated invalid value", "code": "INVALID_VALUE"}]}}
        for m in variables["metafields"]:
            p = self.products[m["ownerId"]]
            mfs = p.setdefault("metafields", [])
            existing = next((x for x in mfs if x["namespace"] == m["namespace"] and x["key"] == m["key"]), None)
            if existing:
                existing.update(type=m["type"], value=m["value"])
            else:
                mfs.append({"id": f"gid://shopify/Metafield/{len(mfs) + 4000}", **{k: m[k] for k in
                            ("namespace", "key", "type", "value")}})
        return {"metafieldsSet": {"metafields": [], "userErrors": []}}

    def _op_ProductSetCategory(self, query, variables):
        self.mutations.append(("productUpdate", variables))
        pid = variables["product"]["id"]
        if self._user_error("ProductSetCategory", pid):
            return {"productUpdate": {"product": None, "userErrors": [{"field": ["category"], "message": "Simulated"}]}}
        cat = variables["product"]["category"]
        self.products[pid]["category"] = {"id": cat, "name": cat.rsplit("/", 1)[-1], "fullName": cat.rsplit("/", 1)[-1]}
        return {"productUpdate": {"product": {"id": pid, "category": {"id": cat, "fullName": ""}}, "userErrors": []}}

    def _op_RedirectCreate(self, query, variables):
        self.mutations.append(("urlRedirectCreate", variables))
        r = variables["urlRedirect"]
        self._next_id += 1
        self.redirects[r["path"]] = {"id": f"gid://shopify/UrlRedirect/{self._next_id}", **r}
        return {"urlRedirectCreate": {"urlRedirect": self.redirects[r["path"]], "userErrors": []}}

    def _op_RedirectUpdate(self, query, variables):
        self.mutations.append(("urlRedirectUpdate", variables))
        r = variables["urlRedirect"]
        current = next(x for x in self.redirects.values() if x["id"] == variables["id"])
        current.update(r)
        return {"urlRedirectUpdate": {"urlRedirect": current, "userErrors": []}}
