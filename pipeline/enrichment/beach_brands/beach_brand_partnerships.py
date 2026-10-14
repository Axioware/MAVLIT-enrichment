"""
pipeline/enrichment/beach_brands/beach_brand_partnerships.py

Which beach-related US brands run creator partnerships?

  1. DISCOVER  — every brand from wikidata_beach_brands.py (all categories,
     operating in the US, no limit). Or --input <json> written by
     `wikidata_beach_brands --output`, to skip the Wikidata queries.
  2. SEARXNG   — look up each brand's official Instagram account and website
     with the local SearXNG instance (link_pages.search_brand_instagram /
     search_brand_site). A handle only counts when it carries the brand name.
     The Wikidata Instagram / website is the fallback when SearXNG finds none.
  3. FACEBOOK  — give every Instagram username to the Branded Content Library
     checker (facebook_branded_content_checker.py): result_count > 0 means the
     account published branded-content / paid-partnership posts in the
     checker's date window.
  4. SAVE      — every brand with partnerships that is NOT already in
     brands_raw is added to it: name, Wikidata ID, description, entity type,
     website + domain, country, headquarters, operating area, and the
     Instagram handle found in step 2 — everything comes from Wikidata
     (source = 'wikidata'); the website falls back to the SearXNG result when
     Wikidata has none. Brands already there (same Instagram handle,
     Wikidata ID or normalized name) are left untouched. --no-add-brands
     turns this step off.
  5. REPORT    — brand name + Instagram URL (+ result count, website) of every
     brand with partnerships, and which were added to brands_raw.

Check outcomes are saved to facebook_branded_content_checks (one row per
username, same table discover_collaborator_creators.py uses), so a re-run
skips usernames already checked and an interrupted run continues where it
stopped. --recheck ignores saved results. --no-db neither reads nor writes
any table (no resume, nothing added to brands_raw).

Run from the project root (FACEBOOK_PROXIES / SEARXNG_URL from .env):
    python -m pipeline.enrichment.beach_brands.beach_brand_partnerships
    python -m pipeline.enrichment.beach_brands.beach_brand_partnerships --output partnerships.json
    python -m pipeline.enrichment.beach_brands.beach_brand_partnerships --category surfing --limit 5
    python -m pipeline.enrichment.beach_brands.beach_brand_partnerships --input beach_brands.json
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from pipeline.enrichment.beach_brands import wikidata_beach_brands as wbb
from pipeline.enrichment_re.facebook_branded_content_checker import (
    END_DATE,
    START_DATE,
    check_branded_content,
)
from pipeline.helpers.link_pages import root_url, search_brand_instagram, search_brand_site
from pipeline.helpers.normalize import normalize
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_RESUMABLE_STATUSES = ("found", "not_found", "not_in_dropdown")   # not "error"


def _handle_from_url(url: str | None) -> str | None:
    """Instagram URL (Wikidata P2003 value) -> lower-case handle."""
    if not url:
        return None
    if "instagram.com" not in url:
        return url.strip().lstrip("@").lower() or None
    seg = urlparse(url).path.strip("/").split("/", 1)[0].lower()
    return seg or None


def _load_brands(args) -> list[dict]:
    """[{name, wikidata_id, instagram, website, categories, coverage}] from --input or live Wikidata."""
    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        rows = payload.get("brands", payload) if isinstance(payload, dict) else payload
        logger.info("Loaded %d brand(s) from %s", len(rows), args.input)
    else:
        client = wbb.WikidataClient()
        try:
            results = wbb.discover(client, category=args.category, min_relevance=args.min_relevance,
                                   all_coverage=args.all_coverage)
        finally:
            client.close()
        rows = [r.to_json() for r in results]
        logger.info("Wikidata: %d qualifying brand(s)", len(rows))
    brands = [{
        "name": r.get("brand"), "wikidata_id": r.get("wikidata_id"),
        "wikidata_instagram": r.get("instagram"), "wikidata_website": r.get("website"),
        "categories": r.get("beach_categories") or [], "coverage": r.get("coverage_level"),
        "description": r.get("description"), "entity_type": r.get("entity_type"),
        "country": r.get("country") or [], "headquarters": r.get("headquarters") or [],
        "operating_area": r.get("operating_area") or [],
    } for r in rows if r.get("brand")]
    return brands[:args.limit] if args.limit else brands


def _resolve_accounts(brands: list[dict]) -> None:
    """SearXNG lookup of each brand's Instagram handle + website, Wikidata as the fallback."""
    for i, b in enumerate(brands, 1):
        names = [b["name"]]
        handle = search_brand_instagram(names)
        b["instagram_source"] = "searxng" if handle else None
        wd_handle = _handle_from_url(b["wikidata_instagram"])
        if not handle and wd_handle:
            handle, b["instagram_source"] = wd_handle, "wikidata"
        b["handle"] = handle

        site = search_brand_site(names)
        if site:
            b["website"], b["website_source"] = root_url(site), "searxng"
        elif b["wikidata_website"]:
            b["website"], b["website_source"] = root_url(b["wikidata_website"]), "wikidata"
        else:
            b["website"], b["website_source"] = None, None
        logger.info("[%d/%d] %s -> instagram %s (%s) | website %s",
                    i, len(brands), b["name"], f"@{handle}" if handle else "not found",
                    b["instagram_source"] or "-", b["website"] or "not found")


def _db_session(no_db: bool):
    if no_db:
        return None
    from pipeline.db import Base, FacebookBrandedContentCheck, SessionLocal, engine
    Base.metadata.create_all(bind=engine, tables=[FacebookBrandedContentCheck.__table__])
    return SessionLocal()


def _saved_check(db, username: str) -> dict | None:
    if db is None:
        return None
    from pipeline.db import FacebookBrandedContentCheck as F
    row = (db.query(F).filter(F.username == username)
           .filter(F.status.in_(_RESUMABLE_STATUSES))
           .filter(F.start_date == START_DATE, F.end_date == END_DATE).first())
    if row is None:
        return None
    return {"status": row.status, "instagram_id": row.instagram_id, "result_count": row.result_count or 0}


def _save_check(db, username: str, status: str, instagram_id: str | None = None,
                result_count: int | None = None, error: str | None = None) -> None:
    if db is None:
        return
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert
    from pipeline.db import FacebookBrandedContentCheck as F
    values = {"username": username, "instagram_id": instagram_id, "status": status,
              "result_count": result_count, "error": (error or "")[:2000] or None,
              "start_date": START_DATE, "end_date": END_DATE}
    stmt = insert(F).values(**values)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[F.username],
        set_={**{k: stmt.excluded[k] for k in values if k != "username"}, "checked_at": func.now()},
    ))
    db.commit()


def _check_accounts(brands: list[dict], db, recheck: bool, headed: bool) -> None:
    """Facebook Branded Content Library check, once per distinct username."""
    done: dict[str, dict] = {}
    todo = [b for b in brands if b.get("handle")]
    for i, b in enumerate(todo, 1):
        username = b["handle"]
        tag = f"[{i}/{len(todo)}] {b['name']} (@{username})"
        if username in done:
            b.update(done[username])
            continue
        saved = None if recheck else _saved_check(db, username)
        if saved:
            logger.info("%s: already checked (%s, %d result(s)) — reusing", tag, saved["status"], saved["result_count"])
            b.update(saved)
            done[username] = saved
            continue
        try:
            r = asyncio.run(check_branded_content(username, headless=not headed))
        except KeyboardInterrupt:
            raise
        except Exception as exc:   # CheckError / proxy / browser — recorded, retried next run
            logger.warning("%s: check failed (%s)", tag, exc)
            _save_check(db, username, "error", error=f"{type(exc).__name__}: {exc}")
            b.update({"status": "error", "instagram_id": None, "result_count": None})
            continue
        status = "found" if r.result_count >= 1 else ("not_found" if r.instagram_id else "not_in_dropdown")
        _save_check(db, username, status, instagram_id=r.instagram_id, result_count=r.result_count)
        res = {"status": status, "instagram_id": r.instagram_id, "result_count": r.result_count}
        b.update(res)
        done[username] = res
        logger.info("%s: %s — %d branded-content result(s)", tag, status, r.result_count)


def _add_new_brands_to_brands_raw(brands: list[dict], db) -> None:
    """
    Add brands with branded-content partnerships that are not in brands_raw yet.
    Sets b["added"] to True (inserted) / False (already there or skipped).
    """
    from pipeline.db import BrandRaw, insert_brand

    partners = [b for b in brands if (b.get("result_count") or 0) > 0 and b.get("handle")]
    if not partners:
        return
    existing = db.query(BrandRaw.instagram_handle, BrandRaw.wikidata_id, BrandRaw.name_normalized).all()
    have_handles = {normalize_handle(h).lower() for h, _, _ in existing if h and normalize_handle(h)}
    have_qids = {q for _, q, _ in existing if q}
    have_names = {n for _, _, n in existing if n}

    for b in partners:
        handle, qid = b["handle"].lower(), b.get("wikidata_id")
        normalized = normalize(b["name"])
        if handle in have_handles or (qid and qid in have_qids) or normalized in have_names:
            b["added"] = False
            b["add_note"] = "already in brands_raw"
            continue
        # Wikidata P856 first; the SearXNG website only when Wikidata has none.
        website, website_source = b.get("wikidata_website"), "wikidata"
        if not website:
            website, website_source = b.get("website"), "searxng_search"
        website = root_url(website) if website else None   # root URL, as the other enrichment steps expect
        domain = urlparse(website).netloc.lower().removeprefix("www.") if website else None
        row = {
            "name": b["name"], "normalized": normalized,
            "source": "wikidata", "source_url": f"https://www.wikidata.org/wiki/{qid}" if qid else None,
            "source_confidence": 100,
            "wikidata_id": qid, "entity_type": b.get("entity_type"),
            "description": b.get("description"),
            "website": website, "domain": domain,
            "country": ", ".join(b.get("country") or []) or None,
            "headquarters": ", ".join(b.get("headquarters") or []) or None,
            "operating_area": ", ".join(b.get("operating_area") or []) or None,
            "instagram_handle": f"https://www.instagram.com/{b['handle']}",
            "refferls": False,
        }
        inserted = insert_brand(db, row)
        if inserted and website:   # insert_brand stamps website_source='wikidata'; keep the real source
            db.query(BrandRaw).filter(BrandRaw.name_normalized == normalized).update(
                {"website_source": website_source}, synchronize_session=False)
            db.commit()
        b["added"] = inserted
        b["add_note"] = "added to brands_raw" if inserted else "skipped (conflict with an existing row)"
        if inserted:
            have_handles.add(handle)
            have_names.add(normalized)
            if qid:
                have_qids.add(qid)
        logger.info("%s (@%s): %s", b["name"], b["handle"], b["add_note"])


def _report(brands: list[dict]) -> list[dict]:
    partners = [b for b in brands if (b.get("result_count") or 0) > 0]
    partners.sort(key=lambda b: (-(b["result_count"] or 0), b["name"].lower()))
    n = len(brands)
    with_ig = sum(1 for b in brands if b.get("handle"))
    print("\n" + "=" * 70)
    print("BEACH-RELATED BRANDS WITH CREATOR PARTNERSHIPS (Meta Branded Content Library)")
    print(f"Window: {START_DATE} .. {END_DATE}")
    print("=" * 70)
    for b in partners:
        note = f"   [{b['add_note']}]" if b.get("add_note") else ""
        print(f"{b['name']}\n    https://www.instagram.com/{b['handle']}/   "
              f"({b['result_count']} result(s))   {b.get('website') or ''}{note}")
    print("\n" + "-" * 70)
    print(f"Brands discovered:               {n}")
    print(f"With an Instagram account:       {with_ig}  "
          f"(SearXNG {sum(1 for b in brands if b.get('instagram_source') == 'searxng')}, "
          f"Wikidata {sum(1 for b in brands if b.get('instagram_source') == 'wikidata')})")
    print(f"Without an Instagram account:    {n - with_ig}")
    print(f"Branded-content partnerships:    {len(partners)}")
    print(f"Checked, none found:             {sum(1 for b in brands if b.get('status') in ('not_found', 'not_in_dropdown'))}")
    print(f"Check errors (re-run to retry):  {sum(1 for b in brands if b.get('status') == 'error')}")
    print(f"Added to brands_raw:             {sum(1 for b in brands if b.get('added') is True)}")
    print(f"Already in brands_raw:           {sum(1 for b in partners if b.get('added') is False)}")
    return partners


def _write_output(path: Path, brands: list[dict], partners: list[dict]) -> None:
    rows = [{**b, "instagram_url": f"https://www.instagram.com/{b['handle']}/" if b.get("handle") else None}
            for b in brands]
    if path.suffix.lower() == ".csv":
        cols = ["name", "instagram_url", "result_count", "status", "website", "instagram_source",
                "website_source", "wikidata_id", "categories", "coverage"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "categories": ", ".join(r.get("categories") or [])})
    else:
        path.write_text(json.dumps({
            "window": {"start": START_DATE, "end": END_DATE},
            "partnerships": [r for r in rows if (r.get("result_count") or 0) > 0],
            "all_brands": rows,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWritten to {path}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--category", default="all", choices=("all",) + wbb.CATEGORIES)
    parser.add_argument("--min-relevance", default="MEDIUM", choices=("HIGH", "MEDIUM"))
    parser.add_argument("--all-coverage", action="store_true",
                        help="Also include brands without evidence of operating in the US.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N brands (testing).")
    parser.add_argument("--input", type=Path, default=None,
                        help="JSON written by wikidata_beach_brands --output (skips the Wikidata queries).")
    parser.add_argument("--output", type=Path, default=None, help="Write results to a .json or .csv file.")
    parser.add_argument("--recheck", action="store_true", help="Ignore saved Facebook check results.")
    parser.add_argument("--no-add-brands", action="store_true",
                        help="Don't add brands with partnerships to brands_raw (default: add the new ones).")
    parser.add_argument("--no-db", action="store_true",
                        help="Don't read/write facebook_branded_content_checks (no resume).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (debugging).")
    args = parser.parse_args(argv)

    try:
        brands = _load_brands(args)
    except wbb.WikidataUnavailableError as exc:
        print(f"ERROR: Wikidata is unavailable — {exc}", file=sys.stderr)
        return wbb.EXIT_WIKIDATA_UNAVAILABLE

    _resolve_accounts(brands)
    db = _db_session(args.no_db)
    try:
        _check_accounts(brands, db, args.recheck, args.headed)
    except KeyboardInterrupt:
        logger.warning("Interrupted — reporting what was checked so far (re-run to continue).")
    try:
        if db is not None and not args.no_add_brands:
            _add_new_brands_to_brands_raw(brands, db)
    finally:
        if db is not None:
            db.close()

    partners = _report(brands)
    if args.output:
        _write_output(args.output, brands, partners)
    return 0


if __name__ == "__main__":
    sys.exit(main())
