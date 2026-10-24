"""One-off probe: run the Apify Facebook comments scraper on a fixture post, save items, estimate cost."""
import json
import sys
from decimal import Decimal
from pathlib import Path

from apify_client import ApifyClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "apify_comments_sample.json"
POSTS_FIXTURE = ROOT / "tests" / "fixtures" / "apify_posts_sample.json"
STORE_PRICE_PER_COMMENT = 1.40 / 1000
COMMENTS_PER_POST = 100


def run(client: ApifyClient, post_url: str):
    return client.actor("apify/facebook-comments-scraper").call(
        run_input={"startUrls": [{"url": post_url}], "resultsLimit": 20, "includeNestedComments": True},
        max_total_charge_usd=Decimal("0.50"),
    )


def post_count(dsn: str) -> tuple[int, str]:
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            return conn.execute("SELECT count(*) FROM posts").fetchone()[0], "database"
    except Exception as e:  # noqa: BLE001
        return 1500, f"assumed (db unreachable: {type(e).__name__})"


def main() -> None:
    settings = get_settings()
    client = ApifyClient(settings.apify_api_token)
    urls = [p["url"] for p in json.loads(POSTS_FIXTURE.read_text(encoding="utf-8"))]
    items, run_info = [], None
    for url in urls[:3]:
        run_info = run(client, url)
        items = list(client.dataset(run_info.default_dataset_id).iterate_items())
        print("post:", url, "-> items:", len(items), "(error items: %d)" % sum("error" in i for i in items))
        if any("error" not in i for i in items):
            items = [i for i in items if "error" not in i]
            break
    FIXTURE.write_text(json.dumps(items, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("items:", len(items))
    if items:
        print("top-level keys:", sorted(items[0].keys()))
    usage = float(run_info.usage_total_usd or 0.0)
    print("usage_total_usd:", usage)
    print("charged_event_counts:", run_info.charged_event_counts)
    print("pricing_info:", run_info.pricing_info.model_dump() if run_info.pricing_info else None)
    charged = (run_info.charged_event_counts or {}).get("comment", 0)
    per_comment = usage / max(len(items), 1)
    if charged == 0:
        events = (run_info.pricing_info.model_dump().get("pricing_per_event") or {}).get("actor_charge_events", {})
        per_comment = (events.get("comment") or {}).get("event_price_usd") or STORE_PRICE_PER_COMMENT
        print(f"no comments charged in this run; using pricing_info comment event price ${per_comment}/comment")
    n, src = post_count(settings.database_url)
    print(f"per-comment USD: {per_comment:.6f}")
    print(f"backfill estimate: {n} posts ({src}) x {COMMENTS_PER_POST} comments = ${n * COMMENTS_PER_POST * per_comment:.2f}")


if __name__ == "__main__":
    main()
