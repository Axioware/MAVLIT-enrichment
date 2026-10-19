"""LLM cost report from the llm_usage table (written by pipeline/helpers/gpt_llm.py).

    .venv/bin/python scripts/llm_cost_report.py                 # last 7 days
    .venv/bin/python scripts/llm_cost_report.py --days 30
    .venv/bin/python scripts/llm_cost_report.py --since 2026-10-19
    .venv/bin/python scripts/llm_cost_report.py --creator-id 4  # everything one MAVLIT user triggered

Prints the total, then cost by day, by model, by feature (grouped from each
call's context label) and by MAVLIT user (calls triggered from the app: description,
profile signals, matches v2/v3, pitch, rate estimate, contract advice).
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from config import DATABASE_URL

# (substring of the context label, feature name) — first match wins.
_FEATURES = [
    ("llm brand ranking", "matching ranking (v2/v3)"),
    ("brand website audience", "brand audience check (website)"),
    ("brand gender check", "brand audience check (instagram)"),
    ("geo reach", "geo reach"),
    ("full-filter post", "instagram posts: full filter"),
    ("instagram post sponsorship score", "instagram posts: sponsorship score"),
    ("demographics @", "instagram users: demographics"),
    ("brand-post creator niche", "creator niche"),
    ("collaborator", "collaborator creators"),
    ("creator niche", "creator niche"),
    ("brand embedding", "brand embedding"),
    ("creator embedding", "creator embedding"),
    ("creator tags", "creator tags"),
    ("creator instagram", "creator description"),
    ("creator youtube", "creator description"),
    ("brand tier", "brand tier"),
    ("beach related check", "beach brand check"),
    ("brand niche tags", "brand niche tags"),
    ("apollo", "apollo contacts"),
    ("linkedin", "linkedin verify"),
    ("commenter gender", "youtube commenter gender"),
    ("link_classify", "brand instagram profile"),
    ("website_pick", "brand instagram profile"),
    ("brand_check", "content creator RE"),
    ("sponsorship score", "RE sponsorship score"),
    ("pitch for", "pitching"),
    ("rate estimate", "rate intelligence"),
    ("contract advice", "contract advice"),
]


def _feature(context: str | None) -> str:
    if not context:
        return "(no context)"
    lowered = context.lower()
    for needle, name in _FEATURES:
        if needle in lowered:
            return name
    # Unknown label: drop ids, handles and URLs so similar calls group together.
    return re.sub(r"(@\S+|\S+=\S+|\(.*?\)|\d+)", "", context).strip() or "(other)"


def _print_table(title: str, rows: dict, total_cost: float) -> None:
    print(f"\n{title}")
    print(f"  {'':42} {'calls':>7} {'input tok':>12} {'output tok':>12} {'cost $':>10} {'share':>6}")
    for key, r in sorted(rows.items(), key=lambda kv: -kv[1]["cost"]):
        share = (r["cost"] / total_cost * 100) if total_cost else 0
        print(f"  {str(key)[:42]:42} {r['calls']:>7,} {r['input']:>12,} {r['output']:>12,} {r['cost']:>10.4f} {share:>5.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise LLM cost from llm_usage.")
    parser.add_argument("--days", type=int, default=7, help="Look back this many days (default 7).")
    parser.add_argument("--since", help="Start date YYYY-MM-DD (UTC); overrides --days.")
    parser.add_argument("--creator-id", type=int, help="Only the calls triggered by this MAVLIT user (creator_profiles.id).")
    args = parser.parse_args()

    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.since else datetime.now(timezone.utc) - timedelta(days=args.days)
    )
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.llm_usage')")).scalar()
        if not exists:
            print("llm_usage table does not exist yet — it is created on the first logged LLM call.")
            return 0
        rows = conn.execute(text("""
            SELECT created_at, model, context, prompt_tokens, completion_tokens, reasoning_tokens, cost_usd,
                   creator_profile_id, feature
            FROM llm_usage WHERE created_at >= :since ORDER BY created_at
        """), {"since": since}).all()

    if args.creator_id is not None:
        rows = [r for r in rows if r.creator_profile_id == args.creator_id]

    empty = lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0}  # noqa: E731
    by_day, by_model, by_feature, by_creator = (defaultdict(empty) for _ in range(4))
    unpriced = 0
    total = empty()
    for r in rows:
        cost = float(r.cost_usd) if r.cost_usd is not None else 0.0
        unpriced += r.cost_usd is None
        feature = r.feature or _feature(r.context)
        groups = [total, by_day[r.created_at.date().isoformat()], by_model[r.model], by_feature[feature]]
        if r.creator_profile_id is not None:
            groups.append(by_creator[f"user {r.creator_profile_id} - {r.feature}"])
        for g in groups:
            g["calls"] += 1
            g["input"] += r.prompt_tokens
            g["output"] += r.completion_tokens
            g["cost"] += cost

    print(f"LLM cost since {since:%Y-%m-%d %H:%M} UTC: ${total['cost']:.4f} "
          f"over {total['calls']:,} calls ({total['input']:,} input / {total['output']:,} output tokens)")
    if unpriced:
        print(f"  note: {unpriced} call(s) used a model with no price in gpt_llm._PRICES and count as $0")
    if not rows:
        return 0
    _print_table("By day", by_day, total["cost"])
    _print_table("By model", by_model, total["cost"])
    _print_table("By feature", by_feature, total["cost"])
    if by_creator:
        _print_table("By MAVLIT user and feature", by_creator, total["cost"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
