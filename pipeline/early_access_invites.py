"""
pipeline/early_access_invites.py

Invite the people who filled in the Early Access form: for each one it makes a single-use token, stores only the token's SHA-256
(table account_invites) and writes the email that carries the link
    https://app.mavlit.com/set-password?token=<token>
That page shows their email (not editable) and asks for the password they want; saving it creates the account and signs them in, and the
link stops working. Opened again, the link just sends them to the app (or to the sign-in page when they are not signed in).

Who gets an invite: early_access_signups rows that have no account yet. Inviting someone again replaces their earlier token.

Run, giving the email(s) to invite as arguments - only those people are invited and emailed:
    python -m pipeline.early_access_invites new@example.com                   # token + email, sent by SMTP
    python -m pipeline.early_access_invites a@x.com b@y.com --days 7
    python -m pipeline.early_access_invites new@example.com --no-send        # token + .eml file in ./invites/, nothing sent
    python -m pipeline.early_access_invites                                  # no email given: only LISTS the sign-ups without an account
    python -m pipeline.early_access_invites --all                            # every sign-up without an account (explicit, sends to all)

An email that already has an account is skipped. An email need not be in the Early Access table (it is used as given). Sending needs
SMTP_HOST, SMTP_PORT (default 587, STARTTLS), SMTP_USER, SMTP_PASSWORD and MAIL_FROM in .env; with --no-send the .eml files can be opened in
a mail client and sent by hand. The links are printed too: they are the only place the tokens exist.
"""

import argparse
import hashlib
import html
import logging
import os
import secrets
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import AccountInvite, BatchSessionLocal, CreatorProfile, EarlyAccessSignup

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.mavlit.com"
DEFAULT_DAYS = 30
SUBJECT = "Your MAVLIT early access is ready"


def make_token() -> str:
    return secrets.token_urlsafe(32)   # 256 random bits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invite_link(token: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/set-password?token={token}"


def build_message(email: str, link: str, days: int, sender: str | None = None) -> EmailMessage:
    """The invitation email (plain text and HTML)."""
    text = (
        "Hi,\n\n"
        "Thanks for joining the MAVLIT founding creators. Your access is ready.\n\n"
        f"Choose your password and sign in here:\n{link}\n\n"
        f"This link works once and expires in {days} days. If you did not ask for early access, you can ignore this email.\n\n"
        "The MAVLIT team\n"
    )
    page = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;color:#111">'
        "<p>Hi,</p><p>Thanks for joining the MAVLIT founding creators. Your access is ready.</p>"
        f'<p><a href="{html.escape(link)}" style="display:inline-block;background:#5b5bd6;color:#fff;text-decoration:none;'
        'padding:12px 22px;border-radius:10px;font-weight:bold">Choose your password</a></p>'
        f'<p style="font-size:13px;color:#555">Or paste this link into your browser:<br>{html.escape(link)}</p>'
        f'<p style="font-size:13px;color:#555">This link works once and expires in {days} days. '
        "If you did not ask for early access, you can ignore this email.</p><p>The MAVLIT team</p></div>"
    )
    message = EmailMessage()
    message["Subject"] = SUBJECT
    message["To"] = email
    if sender:
        message["From"] = sender
    message["Date"] = formatdate(localtime=False)                 # a mail without Date / Message-ID looks like bulk mail to spam filters
    message["Message-ID"] = make_msgid(domain=(sender or "mavlit.com").rsplit("@", 1)[-1].strip("> ") or "mavlit.com")
    message.set_content(text)
    message.add_alternative(page, subtype="html")
    return message


def people_to_invite(db: Session, only_emails: list[str] | None = None) -> list[EarlyAccessSignup]:
    """Sign-ups that have no account yet (optionally just these emails), oldest first."""
    query = db.query(EarlyAccessSignup).filter(~db.query(CreatorProfile.id).filter(CreatorProfile.email == EarlyAccessSignup.email).exists())
    if only_emails:
        query = query.filter(EarlyAccessSignup.email.in_([e.strip().lower() for e in only_emails]))
    return query.order_by(EarlyAccessSignup.created_at, EarlyAccessSignup.id).all()


def people_for_emails(db: Session, emails: list[str]) -> tuple[list[str], list[str]]:
    """(emails to invite, emails skipped because they already have an account), in the order given, without repeats."""
    wanted = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))
    have = {e for (e,) in db.query(CreatorProfile.email).filter(CreatorProfile.email.in_(wanted))} if wanted else set()
    return [e for e in wanted if e not in have], [e for e in wanted if e in have]


def store_invite(db: Session, email: str, token: str, days: int) -> None:
    """Saves the token's hash for this email, replacing an earlier invite (which stops working)."""
    values = {"email": email, "token_hash": hash_token(token), "expires_at": datetime.now(timezone.utc) + timedelta(days=days), "used_at": None}
    db.execute(pg_insert(AccountInvite).values(**values).on_conflict_do_update(
        index_elements=[AccountInvite.email], set_={k: v for k, v in values.items() if k != "email"}))
    db.commit()


def send_by_smtp(messages: list[EmailMessage]) -> int:
    host, user, password, sender = (os.environ.get(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
    port = int((os.environ.get("SMTP_PORT") or "587").strip())
    if not (host and sender):
        raise SystemExit("--send needs SMTP_HOST and MAIL_FROM (and usually SMTP_USER / SMTP_PASSWORD) in .env")
    sent = 0
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        for message in messages:
            if "From" in message:
                del message["From"]
            message["From"] = sender
            smtp.send_message(message)
            sent += 1
    return sent


def invite(db: Session, emails: list[str], days: int, base_url: str, out_dir: Path | None, sender: str | None):
    """Makes and stores one token per email. Returns [(email, link, message)]."""
    made = []
    for email in emails:
        token = make_token()
        store_invite(db, email, token, days)
        link = invite_link(token, base_url)
        message = build_message(email, link, days, sender)
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{email.replace('/', '_')}.eml").write_bytes(bytes(message))
        made.append((email, link, message))
    return made


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Create early-access invitation links and email them to the given addresses.")
    parser.add_argument("emails", nargs="*", help="the email(s) to invite; only these get a token and an email")
    parser.add_argument("--all", action="store_true", help="invite every Early Access sign-up that has no account yet (sends to all of them)")
    parser.add_argument("--no-send", action="store_true", help="make the tokens and the .eml files but do not send anything")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"days the link works (default {DEFAULT_DAYS})")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL", DEFAULT_BASE_URL), help="the app's address (default APP_BASE_URL or https://app.mavlit.com)")
    parser.add_argument("--out-dir", default="invites", help="folder for the .eml files (default ./invites)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    if args.all and args.emails:
        parser.error("give either email addresses or --all, not both")

    db = BatchSessionLocal()
    try:
        if args.all:
            emails, skipped = [s.email for s in people_to_invite(db)], []
        elif args.emails:
            emails, skipped = people_for_emails(db, args.emails)
        else:
            waiting = people_to_invite(db)
            print(f"{len(waiting)} early-access sign-up(s) without an account (nothing done: give their emails as arguments, or use --all):")
            for s in waiting:
                print(f"  {s.email:<40} @{s.handle} ({s.platform})")
            return
        for email in skipped:
            print(f"skipped {email}: already has an account")
        if not emails:
            print("Nobody to invite.")
            return
        sender = os.environ.get("MAIL_FROM", "").strip() or None
        made = invite(db, emails, args.days, args.base_url, Path(args.out_dir), sender)
        print(f"{len(made)} invitation(s) created, valid {args.days} days (emails also saved in {args.out_dir}/):")
        for email, link, _ in made:
            print(f"  {email}\n    {link}")
        if args.no_send:
            print("\nNothing sent (--no-send).")
        else:
            print(f"\nSent {send_by_smtp([m for _, _, m in made])} email(s) by SMTP.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
