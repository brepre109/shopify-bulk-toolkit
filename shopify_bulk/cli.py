"""Command-line interface.

Every write is two steps:

    shopify-bulk <op> ... --dry-run --plan plans/x.json   # read-only: snapshot, diff, plan file
    shopify-bulk apply plans/x.json                       # preflight against live data, still read-only
    shopify-bulk apply plans/x.json --apply               # the only command that writes

There is no way to write without a plan produced by a dry run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import DEFAULT_API_VERSION, __version__
from .apply import apply_plan, journal_path
from .client import GraphQLClient, ShopifyError
from .matching import RuleError, load_rules
from .ops.categories import plan_categories
from .ops.metafields import plan_metafields
from .ops.redirects import plan_redirects
from .ops.tags import plan_tags
from .plan import Plan, PlanError, render
from .snapshot import export_products, export_redirects, read_jsonl, write_jsonl, write_products_csv
from .transport import Transport


def load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader so a .env works without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client(args, transport: Transport | None) -> GraphQLClient:
    shop = args.shop or os.environ.get("SHOPIFY_SHOP")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not shop or not token:
        raise SystemExit("error: set SHOPIFY_SHOP and SHOPIFY_ACCESS_TOKEN (see .env.example)")
    kwargs = {"shop": shop, "token": token,
              "api_version": args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION}
    if transport is not None:
        kwargs["transport"] = transport
    return GraphQLClient(**kwargs)


def progress(op: dict) -> None:
    print(f"  bulk operation {op['status'].lower()}: {op.get('objectCount') or 0} objects", file=sys.stderr)


def load_products(args, client: GraphQLClient, metafields: bool | str = False) -> list[dict]:
    if args.snapshot:
        products = read_jsonl(Path(args.snapshot))
        print(f"Loaded {len(products)} products from {args.snapshot}", file=sys.stderr)
        return products
    print("Exporting products with a bulk query ...", file=sys.stderr)
    products = list(export_products(client, search=args.search, metafields=metafields, on_progress=progress))
    snap = Path(args.plan).with_suffix(".snapshot.jsonl")
    snap.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(products, snap)
    print(f"Snapshot of {len(products)} products saved to {snap}", file=sys.stderr)
    return products


def finish_plan(plan: Plan, args) -> int:
    plan.api_version = args.api_version or os.environ.get("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION
    path = Path(args.plan)
    plan.save(path)
    print(render(plan, limit=None if args.show_all else 50))
    print(f"\nDRY RUN: nothing was written. Plan saved to {path}")
    if plan.changes:
        print(f"Review it, then:  shopify-bulk apply {path} --apply")
    return 0


def read_rules(path: str | None):
    if not path:
        return None
    return load_rules(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_export(args, transport) -> int:
    client = make_client(args, transport)
    out = Path(args.out)
    if args.resource == "products":
        metafields: bool | str = args.metafields if args.metafields else bool(args.all_metafields)
        records = export_products(client, search=args.search, metafields=metafields, on_progress=progress)
        if args.format == "csv":
            n = write_products_csv(records, out)
            print(f"Wrote {n} variant rows to {out}")
        else:
            n = write_jsonl(records, out)
            print(f"Wrote {n} products to {out}")
    else:
        records = list(export_redirects(client, on_progress=progress))
        if args.format == "csv":
            import csv

            with open(out, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["id", "path", "target"])
                w.writeheader()
                w.writerows(records)
        else:
            write_jsonl(records, out)
        print(f"Wrote {len(records)} redirects to {out}")
    print(f"API cost used: {client.stats.cost:.0f} points, {client.stats.requests} requests", file=sys.stderr)
    return 0


def cmd_tags(args, transport) -> int:
    if not (args.add or args.remove or args.rules or args.csv):
        raise SystemExit("error: give --add/--remove, --rules or --csv")
    filters = read_rules(args.where) if args.where else None
    client = make_client(args, transport)
    products = load_products(args, client)
    plan = plan_tags(products, client.shop, add=args.add or [], remove=args.remove or [],
                     rules=read_rules(args.rules), csv_path=Path(args.csv) if args.csv else None,
                     filters=filters)
    return finish_plan(plan, args)


def cmd_metafields(args, transport) -> int:
    client = make_client(args, transport)
    import csv

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        namespaces = {row.get("namespace", "").strip() for row in csv.DictReader(fh)} - {""}
    metafields: bool | str = next(iter(namespaces)) if len(namespaces) == 1 else True
    products = load_products(args, client, metafields=metafields)
    plan = plan_metafields(products, client.shop, Path(args.csv))
    return finish_plan(plan, args)


def cmd_categories(args, transport) -> int:
    if not (args.rules or args.csv):
        raise SystemExit("error: give --rules and/or --csv")
    client = make_client(args, transport)
    products = load_products(args, client)
    plan = plan_categories(products, client.shop, rules=read_rules(args.rules),
                           csv_path=Path(args.csv) if args.csv else None, overwrite=args.overwrite)
    return finish_plan(plan, args)


def cmd_redirects(args, transport) -> int:
    client = make_client(args, transport)
    if args.snapshot:
        existing = read_jsonl(Path(args.snapshot))
    else:
        print("Exporting existing redirects with a bulk query ...", file=sys.stderr)
        existing = list(export_redirects(client, on_progress=progress))
        snap = Path(args.plan).with_suffix(".snapshot.jsonl")
        snap.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(existing, snap)
    plan = plan_redirects(existing, client.shop, Path(args.csv))
    return finish_plan(plan, args)


def cmd_show(args, transport) -> int:
    plan = Plan.load(Path(args.plan_file))
    print(render(plan, limit=None if args.show_all else 50))
    return 0


def cmd_apply(args, transport) -> int:
    path = Path(args.plan_file)
    plan = Plan.load(path)
    client = make_client(args, transport)
    mode = "APPLY" if args.apply else "PREFLIGHT (read-only; add --apply to write)"
    print(f"{mode}: {plan.kind} plan with {len(plan.changes)} change(s) for {plan.shop}")
    summary = apply_plan(plan, client, path, execute=args.apply, force=args.force, limit=args.limit,
                         max_age_hours=None if args.max_age_hours <= 0 else args.max_age_hours,
                         log=lambda m: print(m, file=sys.stderr))
    for problem in summary.problems[:50]:
        print(problem)
    if len(summary.problems) > 50:
        print(f"... {len(summary.problems) - 50} more, see {journal_path(path)}")
    print(f"\n{'Done' if args.apply else 'Preflight'}: {summary.line()}")
    print(f"API: {client.stats.requests} requests, {client.stats.cost:.0f} cost points, "
          f"{client.stats.throttled} throttled, {client.throttle.total_waited:.1f}s waiting for bucket")
    if args.apply:
        print(f"Journal: {journal_path(path)} (re-run the same command to resume or retry failures)")
    return 1 if summary.failed else 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--shop", help="your-store.myshopify.com (default: $SHOPIFY_SHOP)")
    common.add_argument("--api-version", help=f"Admin API version (default: $SHOPIFY_API_VERSION or {DEFAULT_API_VERSION})")
    common.add_argument("--env-file", default=".env", help="load KEY=VALUE pairs from this file (default: .env)")

    planning = argparse.ArgumentParser(add_help=False)
    planning.add_argument("--dry-run", action="store_true", required=True,
                          help="required: planning commands never write; they print a diff and save a plan")
    planning.add_argument("--plan", required=True, help="where to save the plan (JSON)")
    planning.add_argument("--snapshot", help="use a product/redirect JSONL export instead of a live bulk query")
    planning.add_argument("--search", help="Shopify product search syntax to limit the live export, e.g. 'status:active'")
    planning.add_argument("--show-all", action="store_true", help="print every change, not just the first 50")
    planning.add_argument("--apply", dest="apply_misuse", action="store_true", help=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="shopify-bulk", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", parents=[common], help="bulk-export products or redirects to JSONL/CSV")
    e.add_argument("resource", choices=["products", "redirects"])
    e.add_argument("--out", required=True)
    e.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    e.add_argument("--search", help="Shopify search syntax, e.g. 'vendor:Acme status:active'")
    mf = e.add_mutually_exclusive_group()
    mf.add_argument("--metafields", metavar="NAMESPACE", help="include metafields from this namespace")
    mf.add_argument("--all-metafields", action="store_true", help="include all metafields")
    e.set_defaults(func=cmd_export)

    t = sub.add_parser("tags", parents=[common, planning], help="plan tag additions/removals")
    t.add_argument("--add", action="append", metavar="TAG", help="tag to add (repeatable)")
    t.add_argument("--remove", action="append", metavar="TAG", help="tag to remove (repeatable)")
    t.add_argument("--where", metavar="RULES.json", help="only products matching these rules get --add/--remove")
    t.add_argument("--rules", metavar="RULES.json", help='rule list: [{"match": {...}, "add": [...], "remove": [...]}]')
    t.add_argument("--csv", help="per-product CSV with columns product, add, remove")
    t.set_defaults(func=cmd_tags)

    m = sub.add_parser("metafields", parents=[common, planning], help="plan product metafield values from a CSV")
    m.add_argument("--csv", required=True, help="columns: product, namespace, key, type, value")
    m.set_defaults(func=cmd_metafields)

    c = sub.add_parser("categories", parents=[common, planning], help="plan product category backfill")
    c.add_argument("--rules", metavar="RULES.json", help='[{"match": {...}, "category": "aa-1-13"}], first match wins')
    c.add_argument("--csv", help="columns: product, category")
    c.add_argument("--overwrite", action="store_true", help="also change products that already have a category")
    c.set_defaults(func=cmd_categories)

    r = sub.add_parser("redirects", parents=[common, planning], help="plan URL redirect import from a CSV")
    r.add_argument("--csv", required=True, help="columns: path, target")
    r.set_defaults(func=cmd_redirects)

    s = sub.add_parser("show", help="print a saved plan's diff")
    s.add_argument("plan_file")
    s.add_argument("--show-all", action="store_true")
    s.set_defaults(func=cmd_show)

    a = sub.add_parser("apply", parents=[common], help="preflight a plan, or execute it with --apply")
    a.add_argument("plan_file")
    a.add_argument("--apply", action="store_true", help="actually write (without it: read-only preflight)")
    a.add_argument("--force", action="store_true", help="also write changes whose live value drifted since the dry run")
    a.add_argument("--limit", type=int, help="only process the first N pending changes (canary run)")
    a.add_argument("--max-age-hours", type=float, default=24.0, help="refuse older plans (0 = no limit)")
    a.set_defaults(func=cmd_apply)
    return p


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "apply_misuse", False):
        print("error: planning commands never write. Run with --dry-run, review the diff, "
              "then: shopify-bulk apply <plan> --apply", file=sys.stderr)
        return 2
    if getattr(args, "env_file", None):
        load_env_file(Path(args.env_file))
    try:
        return args.func(args, transport)
    except (PlanError, ShopifyError, RuleError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
