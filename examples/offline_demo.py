"""Run the whole export -> dry run -> preflight -> apply flow against the
in-memory fake store used by the tests. No Shopify store or token needed.

    python examples/offline_demo.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from fakes import FakeShop  # noqa: E402
from shopify_bulk.cli import main  # noqa: E402

os.environ.update(SHOPIFY_SHOP="example-paper.myshopify.com", SHOPIFY_ACCESS_TOKEN="demo")
import shopify_bulk.client  # noqa: E402

shopify_bulk.client.time.sleep = lambda s: None  # don't really wait between bulk polls

shop = FakeShop()
examples = ROOT / "examples"
work = Path(tempfile.mkdtemp(prefix="shopify-bulk-demo-"))
os.chdir(work)


def run(*argv):
    print(f"\n$ shopify-bulk {' '.join(argv)}")
    code = main(list(argv), transport=shop)
    if code:
        raise SystemExit(code)


run("export", "products", "--format", "csv", "--out", "products.csv")
run("categories", "--rules", str(examples / "category-rules.json"), "--dry-run", "--plan", "plans/categories.json")
run("apply", "plans/categories.json")
run("apply", "plans/categories.json", "--apply")
run("apply", "plans/categories.json", "--apply")  # resume: nothing left to do
run("redirects", "--csv", str(examples / "redirects.csv"), "--dry-run", "--plan", "plans/redirects.json")
print(f"\nDemo files are in {work}")
