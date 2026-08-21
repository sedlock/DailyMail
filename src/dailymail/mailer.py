"""MIME assembly and Gmail SMTP delivery.

Standard library only. The credential is loaded here, used here, and never
logged, never placed on a command line, never written to a unit file, and never
included in an exception message.

MIME shape (what Outlook wants for inline images):

    multipart/alternative
      +- text/plain
      +- multipart/related
           +- text/html
           +- image/jpeg  (Content-ID: <cid>)
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import credentials
from .errors import DailyMailError
from .images import EmbeddedImage
from .settings import Settings


class SmtpError(DailyMailError):
    exit_code = 9


@dataclass
class PreparedMessage:
    message: EmailMessage
    message_id: str
    size_bytes: int
    image_count: int

    @property
    def subject(self) -> str:
        return self.message["Subject"]


def build_message(
    *,
    settings: Settings,
    sender: str,
    subject: str,
    html: str,
    text: str,
    images: list[EmbeddedImage],
) -> PreparedMessage:
    """Assemble the multipart message. No network, no credentials."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.from_display_name, sender))
    message["To"] = settings.recipient
    message["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain="dailymail.local")
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"

    message.set_content(text, subtype="plain", charset="utf-8")
    message.add_alternative(html, subtype="html", charset="utf-8")

    if images:
        # The HTML alternative is the last part; attach images inside it so the
        # pair becomes multipart/related.
        html_part = message.get_payload()[-1]
        for image in images:
            maintype, _, subtype = image.content_type.partition("/")
            html_part.add_related(
                image.data,
                maintype=maintype,
                subtype=subtype or "jpeg",
                cid=f"<{image.cid}>",
                filename=f"{image.cid.split('@')[0]}.jpg",
            )

    raw = message.as_bytes()
    return PreparedMessage(
        message=message,
        message_id=message_id,
        size_bytes=len(raw),
        image_count=len(images),
    )


def build_alert_message(
    *,
    settings: Settings,
    sender: str,
    target_date: str,
    failure_class: str,
    detail: str,
    attempts: int,
) -> PreparedMessage:
    """Operator alert used when a validated digest cannot be produced.

    Carries no secrets and no raw source data -- only a failure class, a short
    message, and where to look.
    """
    subject = settings.alert_subject_for(target_date)
    lines = [
        "DailyMail could not produce a validated digest.",
        "",
        f"Digest date   : {target_date}",
        f"Failure class : {failure_class}",
        f"Attempts made : {attempts}",
        "",
        "Detail:",
        f"  {detail}",
        "",
        "No digest was sent for this date. Nothing was partially delivered.",
        "",
        "Check status:",
        "  cd ~/src/DailyMail && uv run dailymail status",
        "  journalctl --user -u dailymail.service -n 200 --no-pager",
        "",
        "Retry manually:",
        f"  cd ~/src/DailyMail && uv run dailymail run-daily --date {target_date}",
    ]
    text = "\n".join(lines)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.from_display_name, sender))
    message["To"] = settings.recipient
    message["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain="dailymail.local")
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text, subtype="plain", charset="utf-8")

    return PreparedMessage(
        message=message,
        message_id=message_id,
        size_bytes=len(message.as_bytes()),
        image_count=0,
    )


def send(prepared: PreparedMessage, settings: Settings) -> str:
    """Deliver over STARTTLS with certificate verification. Returns SMTP status.

    Raises SmtpError after exhausting retries. The credential never appears in
    any message this function produces.
    """
    creds = credentials.load()
    context = ssl.create_default_context()
    last_error: Exception | None = None

    for attempt in range(1, max(1, settings.send_retries) + 1):
        try:
            with smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            ) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(creds.username, creds.password)
                refused = smtp.send_message(prepared.message)
            if refused:
                raise SmtpError(f"recipients refused: {sorted(refused)}")
            return f"accepted (attempt {attempt})"
        except smtplib.SMTPAuthenticationError as exc:
            # Do not retry a credential problem, and do not echo the response
            # body, which can contain the attempted username.
            raise SmtpError(
                "SMTP authentication failed. Verify GMAIL_SMTP_USER and that "
                "GMAIL_APP_PASSWORD is a current Google App Password. "
                f"(SMTP code {exc.smtp_code})"
            ) from None
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            last_error = exc
            if attempt >= max(1, settings.send_retries):
                break

    scrubbed = credentials.scrub(f"{type(last_error).__name__}: {last_error}", creds)
    raise SmtpError(
        f"SMTP delivery failed after {max(1, settings.send_retries)} attempt(s): "
        f"{scrubbed[:300]}"
    )


def sender_address() -> str:
    """The authenticated Gmail address, read without exposing the password."""
    return credentials.load().username
