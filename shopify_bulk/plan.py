"""Plans: the reviewed, tamper-evident output of a dry run.

Every write goes through a plan. A plan records, for each change, the state
the dry run saw (``before``) and the state it wants (``after``). ``apply``
re-reads the live state and only writes when it still matches ``before`` (or
already equals ``after``), so a plan cannot silently overwrite edits made
after it was reviewed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PLAN_VERSION = 1


class PlanError(RuntimeError):
    pass


@dataclass
class Change:
    id: str  # stable id, used by the journal for resume
    action: str  # e.g. "update", "create"
    resource: str  # Product gid, redirect path, ...
    label: str  # human-readable (handle, path)
    before: dict
    after: dict


@dataclass
class Plan:
    kind: str
    shop: str
    changes: list[Change]
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    api_version: str = ""
    version: int = PLAN_VERSION

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"kind": self.kind, "shop": self.shop, "changes": [asdict(c) for c in self.changes]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint()
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Plan":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != PLAN_VERSION:
            raise PlanError(f"unsupported plan version {data.get('version')}")
        stored = data.pop("fingerprint", None)
        plan = cls(**{**data, "changes": [Change(**c) for c in data["changes"]]})
        if stored != plan.fingerprint():
            raise PlanError(
                "plan fingerprint mismatch: the plan was edited after the dry run. "
                "Re-run the command with --dry-run to produce a fresh plan."
            )
        return plan

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - datetime.fromisoformat(self.created_at)).total_seconds() / 3600


def _fmt(value) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value if value else '""'
    return json.dumps(value, ensure_ascii=False)


def diff_lines(change: Change) -> list[str]:
    """Unified-diff-style lines for one change."""
    out = [f"{change.action.upper():7} {change.label}"]
    # Only fields present on both sides are user-facing; extra keys on either side
    # (redirect ids, metafield type/namespace) are bookkeeping for apply.
    keys = [k for k in change.before if k in change.after]
    for key in keys:
        b, a = change.before.get(key), change.after.get(key)
        if b == a:
            continue
        if isinstance(b, list) or isinstance(a, list):
            b_set, a_set = set(b or []), set(a or [])
            for item in sorted(b_set - a_set):
                out.append(f"  - {key}: {item}")
            for item in sorted(a_set - b_set):
                out.append(f"  + {key}: {item}")
        else:
            out.append(f"  - {key}: {_fmt(b)}")
            out.append(f"  + {key}: {_fmt(a)}")
    return out


def render(plan: Plan, limit: int | None = 50) -> str:
    lines = [
        f"Plan: {plan.kind} on {plan.shop}  ({len(plan.changes)} change(s), created {plan.created_at})",
        f"Fingerprint: {plan.fingerprint()[:16]}",
    ]
    counts: dict[str, int] = {}
    for c in plan.changes:
        counts[c.action] = counts.get(c.action, 0) + 1
    if counts:
        lines.append("Actions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for w in plan.warnings:
        lines.append(f"WARNING: {w}")
    lines.append("")
    shown = plan.changes if limit is None else plan.changes[:limit]
    for c in shown:
        lines.extend(diff_lines(c))
    if limit is not None and len(plan.changes) > limit:
        lines.append(f"... {len(plan.changes) - limit} more (use --show-all or open the plan file)")
    if not plan.changes:
        lines.append("Nothing to change.")
    return "\n".join(lines)
