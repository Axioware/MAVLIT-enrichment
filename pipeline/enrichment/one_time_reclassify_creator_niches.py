"""
pipeline/enrichment/one_time_reclassify_creator_niches.py

ONE-TIME TEST SCRIPT — delete after use.

Runs the current CREATOR_NICHE_DEFAULT_PROMPT (Music / Fitness / Health / Beauty
first) on every creator in instagram_users whose niche is NOT one of Music,
Beauty, Health or Fitness, to see where the new prompt would put them.

Step 1 (default — no database writes): classify each creator once with the LLM
(bio + up to 5 recent captions + hashtags from their instagram_users rows) and
write the proposals to two files next to this script:
    one_time_reclassify_creator_niches.csv   — creator, old niche, new niche, applies?
    one_time_reclassify_creator_niches.json  — the same, read by --apply
A proposal "applies" only when the new niche is Music, Beauty, Health or
Fitness; a creator the prompt still puts elsewhere (or answers "Unknown")
keeps the old niche.

Step 2 (--apply): reads the JSON from step 1 (no LLM calls), backs up the rows
to bak_instagram_users_niche_<date>_reclassify and updates the niche of ALL
non-commenter rows of every creator whose proposal applies.

Run from the project root:
    python -m pipeline.enrichment.one_time_reclassify_creator_niches --limit 5   # try a few
    python -m pipeline.enrichment.one_time_reclassify_creator_niches             # classify all, CSV only
    python -m pipeline.enrichment.one_time_reclassify_creator_niches --apply     # write the proposals
"""

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import text

from pipeline.db import InstagramUser, SessionLocal
from pipeline.helpers.gpt_llm import LLMUnavailableError, call_gpt_json, fill_template
from pipeline.helpers.prompts import CREATOR_NICHE_DEFAULT_PROMPT

logger = logging.getLogger(__name__)

CORE = ("music", "beauty", "health", "fitness")
_HERE = Path(__file__).with_name("one_time_reclassify_creator_niches")
CSV_PATH, JSON_PATH = _HERE.with_suffix(".csv"), _HERE.with_suffix(".json")
_CAPTIONS = 5
_HASHTAG = re.compile(r"#\w+")


def _load_creators(db) -> dict[str, dict]:
    """username -> {bio, captions, old_niche, row_ids} for creators outside the four niches."""
    rows = (
        db.query(InstagramUser)
        .filter(InstagramUser.user_type != "commenter", InstagramUser.niche.isnot(None))
        .all()
    )
    by_user: dict[str, list[InstagramUser]] = defaultdict(list)
    for r in rows:
        if (r.niche or "").strip() and r.niche.strip().lower() not in CORE:
            by_user[r.username].append(r)
    creators = {}
    for username, rs in by_user.items():
        rs.sort(key=lambda r: (r.post_timestamp or "", r.id or 0), reverse=True)
        creators[username] = {
            "bio": next((r.bio.strip() for r in rs if r.bio and r.bio.strip()), ""),
            "captions": [(r.caption or "").strip()[:300] for r in rs if (r.caption or "").strip()][:_CAPTIONS],
            "old_niche": next((r.niche.strip() for r in rs), ""),
            "row_ids": [r.id for r in rs],
        }
    return creators


def _classify(username: str, c: dict) -> str | None:
    captions = " | ".join(c["captions"])
    hashtags = sorted({h.lower() for h in _HASHTAG.findall(captions)})
    prompt = fill_template(
        CREATOR_NICHE_DEFAULT_PROMPT,
        bio=c["bio"][:600] or "none",
        captions=captions or "none",
        hashtags=", ".join(hashtags) if hashtags else "none",
    )
    result = call_gpt_json(prompt, context=f"one-time creator niche @{username}")
    niche = result.get("niche") if isinstance(result, dict) else None
    niche = niche.strip() if isinstance(niche, str) and niche.strip() else None
    return (niche[:1].upper() + niche[1:]) if niche else None


def classify(limit: int | None) -> None:
    db = SessionLocal()
    try:
        creators = _load_creators(db)
    finally:
        db.close()
    items = sorted(creators.items())
    if limit:
        items = items[:limit]
    logger.info("%d creator(s) outside Music/Beauty/Health/Fitness to classify", len(items))

    proposals: list[dict] = []
    try:
        for i, (username, c) in enumerate(items, 1):
            new = _classify(username, c)
            applies = bool(new and new.lower() in CORE)
            proposals.append({"username": username, "old_niche": c["old_niche"], "new_niche": new,
                              "applies": applies, "rows": len(c["row_ids"])})
            logger.info("[%d/%d] @%s: %s -> %s%s", i, len(items), username, c["old_niche"], new,
                        "" if applies else "  (kept)")
    except LLMUnavailableError:
        logger.error("OpenAI unavailable — saving the %d proposal(s) made so far", len(proposals))
    finally:
        JSON_PATH.write_text(json.dumps(proposals, indent=2, ensure_ascii=False), encoding="utf-8")
        with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["username", "old_niche", "new_niche", "applies", "rows"])
            w.writeheader()
            w.writerows(proposals)

    moved = [p for p in proposals if p["applies"]]
    print(f"\nclassified {len(proposals)} creator(s); {len(moved)} would move into the four niches")
    for niche in ("Music", "Beauty", "Health", "Fitness"):
        print(f"  -> {niche}: {sum(1 for p in moved if p['new_niche'].lower() == niche.lower())}")
    print(f"  unchanged (still outside the four / Unknown): {len(proposals) - len(moved)}")
    print(f"written: {CSV_PATH}\n         {JSON_PATH}\nre-run with --apply to write them to the database")


def apply() -> None:
    if not JSON_PATH.is_file():
        sys.exit(f"{JSON_PATH.name} not found — run without --apply first")
    moves = {p["username"]: p["new_niche"] for p in json.loads(JSON_PATH.read_text(encoding="utf-8")) if p["applies"]}
    if not moves:
        sys.exit("nothing to apply")
    backup = f"bak_instagram_users_niche_{date.today():%Y%m%d}_reclassify"
    db = SessionLocal()
    try:
        db.execute(text(f"CREATE TABLE {backup} (id integer, username text, old_niche text, new_niche text)"))
        total = 0
        for username, new in moves.items():
            ids = [r[0] for r in db.execute(
                text("SELECT id FROM instagram_users WHERE username = :u AND user_type <> 'commenter' "
                     "AND lower(btrim(niche)) NOT IN ('music','beauty','health','fitness')"),
                {"u": username}).all()]
            if not ids:
                continue
            db.execute(text(f"INSERT INTO {backup} SELECT id, username, niche, :n FROM instagram_users WHERE id = ANY(:ids)"),
                       {"n": new, "ids": ids})
            db.execute(text("UPDATE instagram_users SET niche = :n WHERE id = ANY(:ids)"), {"n": new, "ids": ids})
            total += len(ids)
        db.commit()
        print(f"updated {total} row(s) for {len(moves)} creator(s); backup table: {backup}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--limit", type=int, default=None, help="Only classify the first N creators.")
    parser.add_argument("--apply", action="store_true", help="Write the saved proposals to the database.")
    args = parser.parse_args()
    apply() if args.apply else classify(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
