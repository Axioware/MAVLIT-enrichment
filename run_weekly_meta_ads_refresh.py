"""
run_weekly_meta_ads_refresh.py

Weekly cron job — refreshes meta_ads_recency_days/meta_ads_count (and the
other sponsorship-activity fields) on brand_match_profile WITHOUT
re-fetching any ads. Does not call enrich_meta_ads() / hit the Meta Ad
Library API at all — it only recomputes from whatever is already sitting
in the meta_ads table.

meta_ads_recency_days is "days since the most recent ad started" — that
number genuinely goes stale on its own as time passes even with zero new
ads fetched (an ad that started 7 days ago becomes "14 days ago" a week
later), so recomputing it weekly keeps sponsorship_activity_score/
match_text.py's "actively running paid campaigns" reason accurate for
matching, with no API cost.

For every brand with a brand_match_profile row, calls
compute_sponsorship_activity() (pipeline/enrichment/brand_signals.py),
which re-derives meta_ads_active/meta_ads_no_end_date/meta_ads_count/
meta_ads_recency_days/youtube_last_sponsorship and the composite
sponsorship_activity_score purely from existing meta_ads/youtube_sponsorships/
instagram_posts rows.

Run manually:
    python3 run_weekly_meta_ads_refresh.py

Suggested crontab (weekly, Sunday 3am server time) — adjust the venv path:
    0 3 * * 0 cd /home/axioware/Desktop/MAVLIT-enrichment && /path/to/venv/bin/python3 run_weekly_meta_ads_refresh.py >> /home/axioware/Desktop/MAVLIT-enrichment/logs/weekly_meta_ads_refresh.log 2>&1

Install with: crontab -e, then paste the line above (mkdir -p logs first).
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from pipeline.db import BrandProfile, SessionLocal
from pipeline.enrichment.brand_signals import compute_sponsorship_activity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)


def _recompute_profiles(db) -> int:
    """Recompute sponsorship_activity_score/meta_ads_* for every brand
    that already has a brand_match_profile row, purely from whatever is
    already in meta_ads/youtube_sponsorships/instagram_posts — no fetch."""
    brand_ids = [row[0] for row in db.query(BrandProfile.brand_raw_id).all()]
    for bid in brand_ids:
        compute_sponsorship_activity(db, bid)
    return len(brand_ids)


def main() -> None:
    db = SessionLocal()
    try:
        recomputed_count = _recompute_profiles(db)
        logger.info("Weekly meta ads refresh: recomputed sponsorship activity for %d brand profiles", recomputed_count)
    finally:
        db.close()


if __name__ == "__main__":
    main()
