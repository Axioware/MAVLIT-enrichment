"""The "reset your password" email: the same look as the invitation (pipeline/mailer.py), different words."""
import html
from email.message import EmailMessage

from pipeline.mailer import FONT, SUPPORT_EMAIL, html_card, logo_available, new_message, paragraph

SUBJECT = "Reset your MAVLIT password"
BUTTON_TEXT = "Reset My Password"


def _text(link: str, minutes: int) -> str:
    return (
        "Hi there,\n\n"
        "We received a request to reset the password of your MAVLIT account.\n\n"
        f"{BUTTON_TEXT}:\n{link}\n\n"
        f"This link can be used once and stays valid for {minutes} minutes.\n\n"
        "If you did not ask for this, you can ignore this email: your password stays the same.\n\n"
        "The MAVLIT Team\n"
        "https://mavlit.com\n\n"
        f"Need help? Reply to this email or write to {SUPPORT_EMAIL}.\n"
    )


def _html(link: str, minutes: int, with_logo: bool) -> str:
    top = (paragraph("Hi there,")
           + paragraph("We received a request to reset the password of your MAVLIT account. Choose a new one with the button below."))
    bottom = (
        paragraph("If you did not ask for this, you can ignore this email: your password stays the same.")
        + f'      <p style="margin:0;font-family:{FONT};font-size:16px;line-height:25px;font-weight:700;color:#ffffff">The MAVLIT Team</p>\n'
    )
    return html_card(title="Reset your MAVLIT password", preheader="Choose a new password for your MAVLIT account.",
                     heading="Reset your password", top_html=top, button_text=BUTTON_TEXT, link=link,
                     note=f"This link can be used once and stays valid for {minutes} minutes.", bottom_html=bottom,
                     footer_reason="You're receiving this because a password reset was requested for your MAVLIT account.", with_logo=with_logo)


def build_reset_message(email: str, link: str, minutes: int, sender: str | None = None) -> EmailMessage:
    return new_message(SUBJECT, email, sender, _text(link, minutes), _html(link, minutes, logo_available()))
