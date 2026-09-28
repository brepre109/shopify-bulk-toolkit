"""One module per operation: a planner (dry run) and a handler (apply)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..client import GraphQLClient
from ..plan import Change

APPLY = "apply"
ALREADY_APPLIED = "already-applied"
DRIFTED = "drifted"


@dataclass
class Decision:
    status: str  # APPLY | ALREADY_APPLIED | DRIFTED
    detail: str = ""
    work: dict | None = None  # what execute() should actually send (may be narrower than the plan)
    forceable: bool = True  # may --force apply it anyway? (never for deleted resources)


class Handler(Protocol):
    batch_size: int

    def fetch_current(self, client: GraphQLClient, changes: list[Change]) -> dict[str, object]:
        """Live state for each change id, in the same shape as ``change.before``."""

    def decide(self, change: Change, current: object) -> Decision: ...

    def execute(self, client: GraphQLClient, items: list[tuple[Change, Decision]]) -> list[str | None]:
        """Perform the writes; return an error message (or None) per item."""


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def handler_for(kind: str) -> Handler:
    from . import categories, metafields, redirects, tags

    handlers = {
        "tags": tags.TagsHandler,
        "metafields": metafields.MetafieldsHandler,
        "categories": categories.CategoriesHandler,
        "redirects": redirects.RedirectsHandler,
    }
    if kind not in handlers:
        raise ValueError(f"unknown plan kind '{kind}'")
    return handlers[kind]()
