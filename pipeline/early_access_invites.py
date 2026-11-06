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
    python -m pipeline.early_access_invites new@example.com --name Ali       # greets "Hi Ali," (without --name: "Hi there,")
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
SUBJECT = "Welcome to MAVLIT - set up your account"


def make_token() -> str:
    return secrets.token_urlsafe(32)   # 256 random bits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invite_link(token: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/set-password?token={token}"


# The look of the email follows mavlit.com: violet #7c3aed, near-black #0d0d14 / #13131e, muted #8080a0, Inter (Arial where it is not installed).
BRAND_VIOLET = "#7c3aed"
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "contact@mavlit.com").strip() or "contact@mavlit.com"
LOGO_PATH = Path(__file__).resolve().parent / "email_assets" / "mavlit-logo.png"   # the MAVLIT mark (violet square, white peak), sent inline
LOGO_CID = "mavlit-logo"
BUTTON_TEXT = "Set Up My Account"
_FONT = "Inter,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _text_body(greeting: str, link: str, days: int) -> str:
    return (
        f"{greeting}\n\n"
        "Welcome to MAVLIT! 🎉\n\n"
        "We're excited to have you as one of our founding creators.\n\n"
        "Your MAVLIT account is ready, and you're just one step away from getting started.\n\n"
        "SET UP YOUR ACCOUNT\n\n"
        f"{BUTTON_TEXT}:\n{link}\n\n"
        f"This link can be used once and stays valid for {days} days.\n\n"
        "Once you've created your password, you'll be able to log in and explore MAVLIT, discover brands that align with your content, "
        "and find new partnership opportunities.\n\n"
        "A LITTLE ABOUT MAVLIT\n\n"
        "We're building a smarter way for creators and brands to connect. We're glad to have you as part of the journey from the beginning.\n\n"
        "If you have any questions or need help getting started, simply reply to this email. We'd love to hear from you.\n\n"
        "Welcome aboard!\n\n"
        "The MAVLIT Team\n"
        "https://mavlit.com\n\n"
        f"Need help? Reply to this email or write to {SUPPORT_EMAIL}.\n"
    )


def _html_body(greeting: str, link: str, days: int, with_logo: bool) -> str:
    """Table-based layout with inline CSS (Gmail, Outlook, Apple Mail); a <style> block only adds the small-screen padding."""
    safe_link, font = html.escape(link, quote=True), _FONT
    logo = (f'<img src="cid:{LOGO_CID}" width="44" height="44" alt="MAVLIT" style="display:block;border:0;outline:none;border-radius:10px">'
            if with_logo else "")
    brand = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center"><tr>'
        + (f'<td style="padding-right:12px;vertical-align:middle">{logo}</td>' if logo else "")
        + f'<td style="vertical-align:middle;font-family:{font};font-size:26px;font-weight:700;letter-spacing:-0.5px;color:#0d0d14">MAVLIT</td>'
        "</tr></table>"
    )
    button = (
        # Outlook (Word engine) gets a real rounded VML button; every other client gets the padded link.
        f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{safe_link}" '
        f'style="height:50px;v-text-anchor:middle;width:240px;" arcsize="22%" stroke="f" fillcolor="{BRAND_VIOLET}"><w:anchorlock/>'
        f'<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:16px;font-weight:bold;">{BUTTON_TEXT}</center></v:roundrect><![endif]-->'
        f'<!--[if !mso]><!--><a href="{safe_link}" target="_blank" style="display:inline-block;background-color:{BRAND_VIOLET};color:#ffffff;'
        f'font-family:{font};font-size:16px;font-weight:700;line-height:20px;text-decoration:none;padding:15px 36px;border-radius:12px;'
        f'border:1px solid {BRAND_VIOLET}">{BUTTON_TEXT}</a><!--<![endif]-->'
    )
    p = f'style="margin:0 0 16px;font-family:{font};font-size:16px;line-height:25px;color:#3b3b4f"'
    return f"""<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>Welcome to MAVLIT</title>
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<style>
  body {{ margin:0; padding:0; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  table, td {{ border-collapse:collapse; mso-table-lspace:0pt; mso-table-rspace:0pt; }}
  @media only screen and (max-width:620px) {{
    .card {{ border-radius:0 !important; }}
    .pad {{ padding-left:22px !important; padding-right:22px !important; }}
    .h1 {{ font-size:26px !important; line-height:32px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background-color:#f1f2f6">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#f1f2f6;font-size:1px;line-height:1px">Your MAVLIT account is ready. Set your password to get started.&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f2f6" style="background-color:#f1f2f6">
<tr><td align="center" style="padding:32px 12px">
  <table role="presentation" class="card" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="max-width:560px;background-color:#ffffff;border-radius:16px;border:1px solid #e6e7ee">
    <tr><td class="pad" align="center" style="padding:36px 40px 8px">{brand}</td></tr>
    <tr><td class="pad" align="center" style="padding:20px 40px 0">
      <h1 class="h1" style="margin:0;font-family:{font};font-size:30px;line-height:36px;font-weight:800;letter-spacing:-0.5px;color:#0d0d14">Welcome to MAVLIT!</h1>
    </td></tr>
    <tr><td class="pad" style="padding:22px 40px 0">
      <p {p}>{html.escape(greeting)}</p>
      <p {p}>We're excited to have you as one of our founding creators. 🎉</p>
      <p {p}>Your MAVLIT account is ready, and you're just one step away from getting started.</p>
    </td></tr>
    <tr><td class="pad" align="center" style="padding:10px 40px 6px">{button}</td></tr>
    <tr><td class="pad" align="center" style="padding:16px 40px 0">
      <p style="margin:0 0 6px;font-family:{font};font-size:13px;line-height:19px;color:#6b6b80">Button not working? Copy and paste this link into your browser:</p>
      <p style="margin:0;font-family:{font};font-size:13px;line-height:19px;word-break:break-all"><a href="{safe_link}" target="_blank" style="color:{BRAND_VIOLET};text-decoration:underline">{safe_link}</a></p>
      <p style="margin:10px 0 0;font-family:{font};font-size:12px;line-height:18px;color:#8080a0">This link can be used once and stays valid for {days} days.</p>
    </td></tr>
    <tr><td class="pad" style="padding:28px 40px 0">
      <p {p}>Once you've created your password, you'll be able to log in and explore MAVLIT, discover brands that align with your content, and find new partnership opportunities.</p>
      <p style="margin:6px 0 8px;font-family:{font};font-size:16px;line-height:25px;font-weight:700;color:#0d0d14">A little about MAVLIT</p>
      <p {p}>We're building a smarter way for creators and brands to connect. We're glad to have you as part of the journey from the beginning.</p>
      <p {p}>If you have any questions or need help getting started, simply reply to this email. We'd love to hear from you.</p>
      <p style="margin:0 0 4px;font-family:{font};font-size:16px;line-height:25px;color:#3b3b4f">Welcome aboard!</p>
      <p style="margin:0;font-family:{font};font-size:16px;line-height:25px;font-weight:700;color:#0d0d14">The MAVLIT Team</p>
    </td></tr>
    <tr><td class="pad" style="padding:30px 40px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #ececf2;font-size:0;line-height:0">&nbsp;</td></tr></table></td></tr>
    <tr><td class="pad" align="center" style="padding:6px 40px 32px">
      <p style="margin:0 0 4px;font-family:{font};font-size:12px;line-height:18px;color:#8080a0"><strong style="color:#0d0d14">MAVLIT</strong> &middot; <a href="https://mavlit.com" target="_blank" style="color:#8080a0;text-decoration:underline">mavlit.com</a></p>
      <p style="margin:0;font-family:{font};font-size:12px;line-height:18px;color:#8080a0">Need help? Reply to this email or write to <a href="mailto:{SUPPORT_EMAIL}" style="color:#8080a0;text-decoration:underline">{SUPPORT_EMAIL}</a></p>
    </td></tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px"><tr><td align="center" style="padding:16px 20px 0;font-family:{font};font-size:11px;line-height:16px;color:#9a9ab0">You're receiving this because you joined the MAVLIT early-access list.</td></tr></table>
</td></tr>
</table>
</body>
</html>
"""


def build_message(email: str, link: str, days: int, sender: str | None = None, first_name: str | None = None) -> EmailMessage:
    """The invitation email: plain text and HTML (with the MAVLIT mark attached inline). Greets by first name when given, else "Hi there,"."""
    name = first_name.strip().split(" ")[0] if first_name and first_name.strip() else ""
    greeting = f"Hi {name}," if name else "Hi there,"
    logo = LOGO_PATH.read_bytes() if LOGO_PATH.exists() else None
    message = EmailMessage()
    message["Subject"] = SUBJECT
    message["To"] = email
    if sender:
        message["From"] = sender
    message["Date"] = formatdate(localtime=False)                 # a mail without Date / Message-ID looks like bulk mail to spam filters
    message["Message-ID"] = make_msgid(domain=(sender or "mavlit.com").rsplit("@", 1)[-1].strip("> ") or "mavlit.com")
    message.set_content(_text_body(greeting, link, days))
    message.add_alternative(_html_body(greeting, link, days, with_logo=logo is not None), subtype="html")
    if logo is not None:
        message.get_payload()[1].add_related(logo, "image", "png", cid=f"<{LOGO_CID}>", filename="mavlit-logo.png", disposition="inline")
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


def invite(db: Session, emails: list[str], days: int, base_url: str, out_dir: Path | None, sender: str | None, first_name: str | None = None):
    """Makes and stores one token per email. Returns [(email, link, message)]."""
    made = []
    for email in emails:
        token = make_token()
        store_invite(db, email, token, days)
        link = invite_link(token, base_url)
        message = build_message(email, link, days, sender, first_name)
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{email.replace('/', '_')}.eml").write_bytes(bytes(message))
        made.append((email, link, message))
    return made


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Create early-access invitation links and email them to the given addresses.")
    parser.add_argument("emails", nargs="*", help="the email(s) to invite; only these get a token and an email")
    parser.add_argument("--name", help="the person's first name for the greeting (only with a single email; otherwise the mail says \"Hi there,\")")
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
    if args.name and len(args.emails) != 1:
        parser.error("--name needs exactly one email address")

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
        made = invite(db, emails, args.days, args.base_url, Path(args.out_dir), sender, args.name)
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
