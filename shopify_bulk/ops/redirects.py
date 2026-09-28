"""URL redirect import from a CSV (path, target)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..client import GraphQLClient
from ..plan import Change, Plan
from ..queries import REDIRECT_CREATE, REDIRECT_UPDATE, REDIRECTS_BY_PATH
from . import ALREADY_APPLIED, APPLY, DRIFTED, Decision, chunks
from ._products import read_csv


def normalize_path(value: str) -> str:
    """Redirect sources are store-relative paths. Accept full URLs and strip the host."""
    value = value.strip()
    if "://" in value:
        parts = urlsplit(value)
        path, query = parts.path, parts.query
    else:
        path, _, query = value.partition("?")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path + (f"?{query}" if query else "")


def normalize_target(value: str) -> str:
    value = value.strip()
    if "://" in value:
        return value  # external or absolute target: keep as given
    return normalize_path(value)


def plan_redirects(existing: list[dict], shop: str, csv_path: Path) -> Plan:
    warnings: list[str] = []
    live = {normalize_path(r["path"]): r for r in existing}
    wanted: dict[str, str] = {}
    for n, row in enumerate(read_csv(csv_path, {"path", "target"}), start=2):
        if not row["path"] or not row["target"]:
            warnings.append(f"{csv_path.name}:{n}: empty path or target, skipped")
            continue
        path, target = normalize_path(row["path"]), normalize_target(row["target"])
        if path == target:
            warnings.append(f"{csv_path.name}:{n}: {path} redirects to itself, skipped")
            continue
        if path in wanted and wanted[path] != target:
            warnings.append(f"{csv_path.name}:{n}: duplicate path {path}, last one wins")
        wanted[path] = target

    final = {**{p: r["target"] for p, r in live.items()}, **wanted}
    for path, target in wanted.items():
        if target in final:
            hop = final[target]
            if hop == path:
                warnings.append(f"loop: {path} -> {target} -> {path}")
            else:
                warnings.append(f"chain: {path} -> {target} -> {hop} (consider pointing straight at {hop})")

    changes = []
    for path, target in sorted(wanted.items()):
        current = live.get(path)
        if current and current["target"] == target:
            continue
        changes.append(Change(
            id=f"redirect:{path}", action="update" if current else "create", resource=path, label=path,
            before={"target": current["target"] if current else None, "redirect_id": current["id"] if current else None},
            after={"target": target},
        ))
    return Plan(kind="redirects", shop=shop, changes=changes, warnings=warnings, params={"csv": str(csv_path)})


class RedirectsHandler:
    batch_size = 1

    def fetch_current(self, client: GraphQLClient, changes: list[Change]) -> dict[str, object]:
        out: dict[str, object] = {c.id: {"target": None, "redirect_id": None} for c in changes}
        by_path = {c.resource: c for c in changes}
        for batch in chunks(changes, 20):
            search = " OR ".join('path:"{}"'.format(c.resource.replace('"', '\\"')) for c in batch)
            data = client.execute(REDIRECTS_BY_PATH, {"query": search}, estimated_cost=55)
            for edge in (data.get("urlRedirects") or {}).get("edges", []):
                node = edge["node"]
                c = by_path.get(normalize_path(node["path"]))
                if c:  # search may be fuzzy: keep exact path matches only
                    out[c.id] = {"target": node["target"], "redirect_id": node["id"]}
        return out

    def decide(self, change: Change, current) -> Decision:
        if current["target"] == change.after["target"]:
            return Decision(ALREADY_APPLIED)
        if current["target"] != change.before["target"]:
            return Decision(DRIFTED, f"now redirects to {current['target']}",
                            work={"redirect_id": current["redirect_id"]})
        return Decision(APPLY, work={"redirect_id": current["redirect_id"]})

    def execute(self, client: GraphQLClient, items) -> list[str | None]:
        errors: list[str | None] = []
        for change, decision in items:
            body = {"path": change.resource, "target": change.after["target"]}
            try:
                if decision.work["redirect_id"]:
                    client.mutate(REDIRECT_UPDATE, {"id": decision.work["redirect_id"], "urlRedirect": body},
                                  "urlRedirectUpdate")
                else:
                    client.mutate(REDIRECT_CREATE, {"urlRedirect": body}, "urlRedirectCreate")
                errors.append(None)
            except Exception as exc:
                errors.append(str(exc))
        return errors
