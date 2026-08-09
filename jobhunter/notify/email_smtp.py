"""SMTP email delivery (multipart text + HTML)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send_email(
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipients: list[str],
    subject: str,
    text_body: str,
    html_body: str,
    use_tls: bool = True,
) -> None:
    if not recipients:
        raise ValueError("no recipients configured (notify.email.to)")
    if not username or not password:
        raise ValueError(
            "SMTP credentials missing. Set SMTP_USER / SMTP_PASS in .env — for Gmail "
            "this must be an App Password, not your account password."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender or username
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "SMTP login rejected. For Gmail: enable 2-Step Verification, then create an "
            "App Password at https://myaccount.google.com/apppasswords and use that as SMTP_PASS."
        ) from exc

    log.info("emailed report to %s", ", ".join(recipients))
