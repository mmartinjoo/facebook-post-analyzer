"""One-off probe: run the Apify Facebook posts scraper on the Kontigo page and save the items as a fixture."""
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from apify_client import ApifyClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "apify_posts_sample.json"
PAGE_URL = "https://www.facebook.com/kontigokontenerpark"


def run(client: ApifyClient, days: int):
    newer = (date.today() - timedelta(days=days)).isoformat()
    return client.actor("apify/facebook-posts-scraper").call(
        run_input={"startUrls": [{"url": PAGE_URL}], "resultsLimit": 5, "onlyPostsNewerThan": newer},
        max_total_charge_usd=Decimal("0.50"),
    )


def main() -> None:
    client = ApifyClient(get_settings().apify_api_token)
    run_info = run(client, 90)
    items = list(client.dataset(run_info.default_dataset_id).iterate_items())
    if not items:
        run_info = run(client, 365)
        items = list(client.dataset(run_info.default_dataset_id).iterate_items())
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(items, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("run id:", run_info.id, "status:", run_info.status)
    print("items:", len(items))
    if items:
        print("top-level keys:", sorted(items[0].keys()))
        media = items[0].get("media") or []
        print("media[0] keys:", sorted(media[0].keys()) if media else None)
    print("usage_total_usd:", run_info.usage_total_usd)
    print("charged_event_counts:", run_info.charged_event_counts)


if __name__ == "__main__":
    main()
