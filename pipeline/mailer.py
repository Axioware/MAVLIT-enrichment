"""
pipeline/mailer.py

The MAVLIT email look and the SMTP sending shared by every email the platform sends (the early-access invitation and the password reset):
a dark card on a dark page, the MAVLIT mark attached inline, a violet button, and a fallback link. Table-based HTML with inline CSS, so it
renders in Gmail, Outlook and Apple Mail. Settings come from the environment: SMTP_HOST, SMTP_PORT (default 587, STARTTLS), SMTP_USER,
SMTP_PASSWORD, MAIL_FROM, SUPPORT_EMAIL (default contact@mavlit.com).
"""

import html
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

# The look follows mavlit.com (its dark theme): page #0d0d14, card #13131e, violet #7c3aed, muted #8080a0, Inter (Arial where it is not installed).
BRAND_VIOLET = "#7c3aed"
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "contact@mavlit.com").strip() or "contact@mavlit.com"
LOGO_PATH = Path(__file__).resolve().parent / "email_assets" / "mavlit-logo.png"   # the MAVLIT mark (violet square, white peak), sent inline
LOGO_CID = "mavlit-logo"
FONT = "Inter,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
PARAGRAPH_STYLE = f'style="margin:0 0 16px;font-family:{FONT};font-size:16px;line-height:25px;color:#c9c9d8"'


def paragraph(text_html: str) -> str:
    """One body paragraph (the text must already be HTML-safe)."""
    return f"      <p {PARAGRAPH_STYLE}>{text_html}</p>\n"


def html_card(*, title: str, preheader: str, heading: str, top_html: str, button_text: str, link: str, note: str,
              bottom_html: str = "", footer_reason: str, with_logo: bool) -> str:
    """The email page: logo, heading, `top_html`, the button, the fallback link and `note`, optional `bottom_html`, then the footer."""
    safe_link, font, p = html.escape(link, quote=True), FONT, PARAGRAPH_STYLE
    logo = (f'<img src="cid:{LOGO_CID}" width="44" height="44" alt="MAVLIT" style="display:block;border:0;outline:none;border-radius:10px">'
            if with_logo else "")
    brand = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center"><tr>'
        + (f'<td style="padding-right:12px;vertical-align:middle">{logo}</td>' if logo else "")
        + f'<td style="vertical-align:middle;font-family:{font};font-size:26px;font-weight:700;letter-spacing:-0.5px;color:#ffffff">MAVLIT</td>'
        "</tr></table>"
    )
    button = (
        # Outlook (Word engine) gets a real rounded VML button; every other client gets the padded link.
        f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{safe_link}" '
        f'style="height:50px;v-text-anchor:middle;width:240px;" arcsize="22%" stroke="f" fillcolor="{BRAND_VIOLET}"><w:anchorlock/>'
        f'<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:16px;font-weight:bold;">{html.escape(button_text)}</center></v:roundrect><![endif]-->'
        f'<!--[if !mso]><!--><a href="{safe_link}" target="_blank" style="display:inline-block;background-color:{BRAND_VIOLET};color:#ffffff;'
        f'font-family:{font};font-size:16px;font-weight:700;line-height:20px;text-decoration:none;padding:15px 36px;border-radius:12px;'
        f'border:1px solid {BRAND_VIOLET}">{html.escape(button_text)}</a><!--<![endif]-->'
    )
    bottom_row = f'    <tr><td class="pad" style="padding:28px 40px 0">\n{bottom_html}    </td></tr>\n' if bottom_html else ""
    return f"""<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{html.escape(title)}</title>
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
<body style="margin:0;padding:0;background-color:#0d0d14">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#0d0d14;font-size:1px;line-height:1px">{html.escape(preheader)}&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#0d0d14" style="background-color:#0d0d14">
<tr><td align="center" style="padding:32px 12px">
  <table role="presentation" class="card" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#13131e" style="max-width:560px;background-color:#13131e;border-radius:16px;border:1px solid #26263a">
    <tr><td class="pad" align="center" style="padding:36px 40px 8px">{brand}</td></tr>
    <tr><td class="pad" align="center" style="padding:20px 40px 0">
      <h1 class="h1" style="margin:0;font-family:{font};font-size:30px;line-height:36px;font-weight:800;letter-spacing:-0.5px;color:#ffffff">{html.escape(heading)}</h1>
    </td></tr>
    <tr><td class="pad" style="padding:22px 40px 0">
{top_html}    </td></tr>
    <tr><td class="pad" align="center" style="padding:10px 40px 6px">{button}</td></tr>
    <tr><td class="pad" align="center" style="padding:16px 40px 0">
      <p style="margin:0 0 6px;font-family:{font};font-size:13px;line-height:19px;color:#9a9ab5">Button not working? Copy and paste this link into your browser:</p>
      <p style="margin:0;font-family:{font};font-size:13px;line-height:19px;word-break:break-all"><a href="{safe_link}" target="_blank" style="color:#a78bfa;text-decoration:underline">{safe_link}</a></p>
      <p style="margin:10px 0 0;font-family:{font};font-size:12px;line-height:18px;color:#8080a0">{html.escape(note)}</p>
    </td></tr>
{bottom_row}    <tr><td class="pad" style="padding:30px 40px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #26263a;font-size:0;line-height:0">&nbsp;</td></tr></table></td></tr>
    <tr><td class="pad" align="center" style="padding:6px 40px 32px">
      <p style="margin:0 0 4px;font-family:{font};font-size:12px;line-height:18px;color:#8080a0"><strong style="color:#ffffff">MAVLIT</strong> &middot; <a href="https://mavlit.com" target="_blank" style="color:#8080a0;text-decoration:underline">mavlit.com</a></p>
      <p style="margin:0;font-family:{font};font-size:12px;line-height:18px;color:#8080a0">Need help? Reply to this email or write to <a href="mailto:{SUPPORT_EMAIL}" style="color:#8080a0;text-decoration:underline">{SUPPORT_EMAIL}</a></p>
    </td></tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px"><tr><td align="center" style="padding:16px 20px 0;font-family:{font};font-size:11px;line-height:16px;color:#6e6e88">{html.escape(footer_reason)}</td></tr></table>
</td></tr>
</table>
</body>
</html>
"""


def attach_logo(message: EmailMessage) -> None:
    """Attaches the MAVLIT mark to the HTML part so `cid:mavlit-logo` shows it. Does nothing when the file is missing."""
    if LOGO_PATH.exists():
        message.get_payload()[1].add_related(LOGO_PATH.read_bytes(), "image", "png", cid=f"<{LOGO_CID}>", filename="mavlit-logo.png", disposition="inline")


def logo_available() -> bool:
    return LOGO_PATH.exists()


def new_message(subject: str, to: str, sender: str | None, text: str, html_page: str) -> EmailMessage:
    """Plain text + HTML message with Date and Message-ID headers (a mail without them looks like bulk mail to spam filters)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = to
    if sender:
        message["From"] = sender
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=(sender or "mavlit.com").rsplit("@", 1)[-1].strip("> ") or "mavlit.com")
    message.set_content(text)
    message.add_alternative(html_page, subtype="html")
    attach_logo(message)
    return message


def send_by_smtp(messages: list[EmailMessage]) -> int:
    host, user, password, sender = (os.environ.get(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))
    port = int((os.environ.get("SMTP_PORT") or "587").strip())
    if not (host and sender):
        raise RuntimeError("Sending needs SMTP_HOST and MAIL_FROM (and usually SMTP_USER / SMTP_PASSWORD) in .env")
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
