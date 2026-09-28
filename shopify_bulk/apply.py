"""Execute a reviewed plan: preflight, drift check, resumable journal."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .client import GraphQLClient
from .ops import ALREADY_APPLIED, DRIFTED, handler_for
from .plan import Plan, PlanError

APPLIED = "applied"
FAILED = "failed"
DONE_STATUSES = {APPLIED, ALREADY_APPLIED}


class Journal:
    """Append-only JSONL log of per-change outcomes, flushed after every line.

    Re-running ``apply`` with the same plan skips changes already recorded as
    applied, so an interrupted run (Ctrl-C, network loss, token expiry) resumes
    where it stopped. Failed and drifted changes are retried on the next run.
    """

    def __init__(self, path: Path, fingerprint: str):
        self.path = path
        self.fingerprint = fingerprint
        self.done: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("plan") != fingerprint:
                    raise PlanError(f"{path} belongs to a different plan; move it away or use a new plan file")
                if entry["status"] in DONE_STATUSES:
                    self.done.add(entry["change"])
                else:
                    self.done.discard(entry["change"])

    def record(self, change_id: str, status: str, detail: str = "") -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "plan": self.fingerprint,
                 "change": change_id, "status": status}
        if detail:
            entry["detail"] = detail
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if status in DONE_STATUSES:
            self.done.add(change_id)


@dataclass
class Summary:
    total: int = 0
    skipped_done: int = 0
    applied: int = 0
    already_applied: int = 0
    drifted: int = 0
    failed: int = 0
    would_apply: int = 0
    problems: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"total={self.total}"]
        for name in ("skipped_done", "would_apply", "applied", "already_applied", "drifted", "failed"):
            value = getattr(self, name)
            if value:
                parts.append(f"{name}={value}")
        return " ".join(parts)


def journal_path(plan_path: Path) -> Path:
    return plan_path.with_name(plan_path.name + ".journal.jsonl")


def check_plan(plan: Plan, client: GraphQLClient, max_age_hours: float | None) -> None:
    if plan.shop != client.shop:
        raise PlanError(f"plan was made for {plan.shop}, but the client is connected to {client.shop}")
    if max_age_hours is not None and plan.age_hours() > max_age_hours:
        raise PlanError(
            f"plan is {plan.age_hours():.1f}h old (limit {max_age_hours}h). Re-run --dry-run so the diff "
            "reflects the current catalog, or pass --max-age-hours to override."
        )


def apply_plan(
    plan: Plan,
    client: GraphQLClient,
    plan_path: Path,
    *,
    execute: bool,
    force: bool = False,
    limit: int | None = None,
    max_age_hours: float | None = 24.0,
    log: Callable[[str], None] = print,
    fetch_chunk: int = 200,
) -> Summary:
    """Preflight (``execute=False``) or apply (``execute=True``) a plan.

    Both modes re-read live state and classify every pending change as
    apply / already-applied / drifted. Only ``execute=True`` writes, and then
    only changes classified as apply (or drifted ones when ``force`` is set).
    """
    check_plan(plan, client, max_age_hours)
    handler = handler_for(plan.kind)
    journal = Journal(journal_path(plan_path), plan.fingerprint()) if execute else None
    summary = Summary(total=len(plan.changes))

    pending = [c for c in plan.changes if not (journal and c.id in journal.done)]
    summary.skipped_done = len(plan.changes) - len(pending)
    if summary.skipped_done:
        log(f"Resuming: {summary.skipped_done} change(s) already done according to {journal.path.name}")
    if limit is not None:
        pending = pending[:limit]

    for start in range(0, len(pending), fetch_chunk):
        window = pending[start : start + fetch_chunk]
        current = handler.fetch_current(client, window)
        to_write = []
        for change in window:
            decision = handler.decide(change, current.get(change.id))
            if decision.status == ALREADY_APPLIED:
                summary.already_applied += 1
                if journal:
                    journal.record(change.id, ALREADY_APPLIED)
                continue
            if decision.status == DRIFTED and not (force and decision.forceable):
                summary.drifted += 1
                summary.problems.append(f"DRIFTED {change.label}: {decision.detail}")
                if journal:
                    journal.record(change.id, DRIFTED, decision.detail)
                continue
            to_write.append((change, decision))

        if not execute:
            summary.would_apply += len(to_write)
            continue

        size = handler.batch_size
        for i in range(0, len(to_write), size):
            batch = to_write[i : i + size]
            try:
                errors = handler.execute(client, batch)
            except Exception as exc:
                errors = [str(exc)] * len(batch)
            for (change, _), error in zip(batch, errors):
                if error:
                    summary.failed += 1
                    summary.problems.append(f"FAILED {change.label}: {error}")
                    journal.record(change.id, FAILED, error)
                else:
                    summary.applied += 1
                    journal.record(change.id, APPLIED)
        log(f"  progress: {start + len(window)}/{len(pending)}  {summary.line()}")
    return summary
