# shopify-bulk-toolkit

A Python CLI for bulk Shopify catalog changes through the Admin GraphQL API. It exports the catalog with a bulk
query, shows a reviewed dry-run diff, and then applies it with a throttle that respects GraphQL query costs, a check
that the live data hasn't changed since the plan was made, and a journal you can resume from.

It covers the edits that tend to go wrong when done in bulk by hand: adding and removing tags, setting metafields,
backfilling product categories, and importing redirects.

```text
$ shopify-bulk categories --rules examples/category-rules.json --dry-run --plan plans/categories.json
Plan: categories on your-store.myshopify.com  (4 change(s), created 2026-09-28T00:30:49+00:00)
Fingerprint: 28134d9d36265402
Actions: update=4
WARNING: 1 uncategorized product(s) matched no rule

UPDATE  kraft-gift-wrap-roll  [rule #2 (title~/\bgift wrap\b/)]
  - category: (none)
  + category: gid://shopify/TaxonomyCategory/ae-2-1-2-8
UPDATE  velvet-ribbon-navy  [rule #1 (productType=ribbon)]
  - category: gid://shopify/TaxonomyCategory/na
  + category: gid://shopify/TaxonomyCategory/ae-2-1-2-14
...
DRY RUN: nothing was written. Plan saved to plans/categories.json
Review it, then:  shopify-bulk apply plans/categories.json --apply
```

This output comes from `python examples/offline_demo.py`, which runs against an in-memory demo store. The
store name has been changed and the list shortened.

## Why

Bulk catalog edits usually get done with one-off scripts. They loop over products and call `productUpdate`, have
no preview, stop halfway on a rate limit, and overwrite whatever a merchandiser changed in the meantime. This
toolkit is what I wish those scripts had been:

| Problem | What the toolkit does |
| --- | --- |
| "What exactly will this change?" | Every write starts as a **plan** from `--dry-run`, with a before/after diff per product. |
| Plans edited or reused by accident | Plans carry a SHA-256 **fingerprint**. They are refused if edited, if they are older than 24 h, or if they were made for another shop. |
| Someone edited the product after the dry run | `apply` re-reads the live values. A change is written only if the live value still equals the plan's `before`. Otherwise it is reported as **drifted** (use `--force` to override). |
| Rate limits | A local model of the **GraphQL cost bucket** waits *before* a request would be throttled. `THROTTLED`, HTTP 429 and 5xx responses are retried with backoff. |
| The run died at item 3,000 of 5,000 | A **journal** (`<plan>.journal.jsonl`) records each outcome, and re-running resumes. Failures are retried; finished items are skipped. |
| Tag overwrites losing concurrent edits | Tags are written with `tagsAdd`/`tagsRemove` deltas, never a full tag list. |
| Re-running a finished job | Every operation is idempotent. Changes that are already live are counted as `already-applied` and not written. |

## Features

- **Export**: bulk query (`bulkOperationRunQuery` plus `bulkOperation(id:)` polling, API 2026-01+) to JSONL or CSV
  (one row per variant). Supports Shopify search syntax (`--search "status:active vendor:Acme"`) and metafields by
  namespace. Nested JSONL (`__parentId`) is reassembled into product → variants/metafields.
- **Tags**: global `--add/--remove` (optionally scoped with `--where` rules), rule files (regex on title, handle or
  SKU; exact match on type, vendor, tag or status), or a per-product CSV. Existing tags are matched
  case-insensitively, the same way Shopify matches them.
- **Metafields**: CSV of `product, namespace, key, type, value`. Values are checked against their type (json, lists,
  integers, decimals, booleans, single-line text) before any API call. Unchanged values are skipped, and a type
  mismatch with an existing metafield is flagged. Writes use `metafieldsSet` in batches of 25. If a batch is
  rejected, it is retried row by row so one bad value doesn't block the other 24.
- **Categories**: backfill Standard Product Taxonomy categories from ordered rules (first match wins) or a CSV
  (CSV beats rules). Two things count as "empty": `category: null` and the real taxonomy node
  `gid://shopify/TaxonomyCategory/na`, whose name is literally "Uncategorized". The admin shows them the same way,
  so an audit that only checks for null misses some products.
- **Redirects**: CSV import that normalizes full URLs to store paths, creates new redirects, updates changed ones,
  skips identical ones, and warns about loops, self-redirects, duplicate rows and redirect **chains**.
- **Canary runs**: `apply --limit 20` writes the first 20 changes. Check them in the admin, then run the same command
  without `--limit` and it resumes.
- Planning from a saved snapshot (`--snapshot export.jsonl`) makes **no API calls**, so a plan can be reviewed and
  code-reviewed before anyone has write access.

## Quick start

```bash
git clone https://github.com/brepre109/shopify-bulk-toolkit.git
cd shopify-bulk-toolkit
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest -q                          # 59 offline tests, no store needed
python examples/offline_demo.py    # full export -> dry run -> preflight -> apply flow against a fake store
```

To use it against a real store, create a custom app with `read_products` and `write_products` scopes. Redirects
also need `read_online_store_navigation` and `write_online_store_navigation`. Then:

```bash
cp .env.example .env               # SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION

shopify-bulk export products --search "status:active" --format csv --out exports/products.csv

shopify-bulk tags --rules examples/tag-rules.json --dry-run --plan plans/tags.json
shopify-bulk apply plans/tags.json                 # preflight: re-reads live data, writes nothing
shopify-bulk apply plans/tags.json --apply --limit 20
shopify-bulk apply plans/tags.json --apply         # resumes after the first 20

shopify-bulk metafields --csv examples/metafields.csv --dry-run --plan plans/mf.json
shopify-bulk categories --rules examples/category-rules.json --dry-run --plan plans/cat.json
shopify-bulk redirects  --csv examples/redirects.csv --dry-run --plan plans/redirects.json
shopify-bulk show plans/redirects.json --show-all
```

Planning commands require `--dry-run` and refuse `--apply`. Nothing writes to the store except
`apply <plan> --apply`.

## Architecture

```
shopify_bulk/
├── client.py      GraphQLClient: cost-aware throttle, THROTTLED/429/5xx retries, userErrors -> exceptions
├── throttle.py    leaky-bucket model synced from extensions.cost.throttleStatus
├── bulk.py        bulkOperationRunQuery + polling + JSONL reassembly
├── snapshot.py    normalized product records, JSONL/CSV export, "Uncategorized" handling
├── plan.py        Change/Plan, fingerprint, diff rendering
├── apply.py       preflight/apply engine, drift classification, resumable journal
├── matching.py    rule matching shared by tags and categories
└── ops/           one module per operation: planner (pure) + handler (fetch_current / decide / execute)
    ├── tags.py  metafields.py  categories.py  redirects.py
```

Planners are pure functions from a snapshot and inputs to a `Plan`, so they need no network access. Each handler
does three things:

- reads the live state for a batch of changes,
- classifies each change as `apply`, `already-applied` or `drifted`,
- performs the writes.

`apply.py` owns the loop, the journal and the summary. Adding a new operation means one new module in `ops/`.

### Tests

The tests use synthetic data only (a made-up paper and craft store). There are two kinds of test double:

- **Recorded response fixtures** (`tests/fixtures/responses/*.json`). These are Admin API response bodies in
  Shopify's real shape: throttling errors with cost extensions, bulk operation states, userErrors. They test the
  client and bulk logic exactly.
- **`FakeShop`** (`tests/fakes.py`), an in-memory store that implements exactly the operations the toolkit sends. It
  serves bulk exports from its state and applies mutations, so the tests run the complete export → plan → apply →
  resume flow and then assert on the resulting catalog. Drift, partial failures and "someone already did it by hand"
  are simulated by editing the fake store between steps.

## Tech stack

Python 3.10+, `requests`, `argparse`, `pytest`. Shopify Admin GraphQL API 2026-07 (bulk operations, `tagsAdd`,
`tagsRemove`, `metafieldsSet`, `productUpdate(product:)`, `urlRedirectCreate`/`urlRedirectUpdate`).

## Status / limitations

- Tested offline against recorded fixtures and the fake store. The GraphQL documents follow the 2026-07 Admin API
  docs. This public rewrite has not been run against a live store yet, so start with `--limit` on a development
  store.
- Only product resources are covered (plus redirects). Variant-level metafields and collections are not implemented.
- The drift check compares full values. Two runs editing *different* keys of the same JSON metafield will see each
  other as drift. That is safe but conservative.
- Redirect lookup at apply time uses the `urlRedirects(query: "path:...")` search and keeps exact matches only. Very
  large redirect imports (tens of thousands) would be better served by Shopify's `urlRedirectImportCreate` CSV flow.
- Snapshots and plans can contain product titles and handles, and `.gitignore` excludes `plans/` and `exports/`.
  Keep real catalog data out of version control.

## License

MIT © 2026 Brett Prejeant
