"""
scheduler.py — Nightly APScheduler job definitions.

Run with: python scheduler.py

Stages are staggered so each completes before the next begins.
seed.py is NOT scheduled — run it manually when adding a new niche.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from pipeline.contacts import run_contacts
from pipeline.db import SessionLocal
from pipeline.enrich import run_enrich
from pipeline.verify import run_verify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

scheduler = BlockingScheduler()


@scheduler.scheduled_job("cron", hour=2, minute=0)
def nightly_enrich():
    logging.info("Enrich started")
    with SessionLocal() as db:
        n = run_enrich(db)
    logging.info("Enrich done — %d brands enriched", n)


@scheduler.scheduled_job("cron", hour=2, minute=15)
def nightly_contacts():
    logging.info("Contacts started")
    with SessionLocal() as db:
        n = run_contacts(db)
    logging.info("Contacts done — %d contacts inserted", n)


@scheduler.scheduled_job("cron", hour=2, minute=45)
def nightly_verify():
    logging.info("Verify started")
    with SessionLocal() as db:
        n = run_verify(db)
    logging.info("Verify done — %d contacts verified", n)


if __name__ == "__main__":
    scheduler.start()
