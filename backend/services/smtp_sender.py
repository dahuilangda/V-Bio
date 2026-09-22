"""Dependency-free plain-text SMTP sender shared by the backend mailer and
the frontend management API. Reads config straight from the environment so
no heavy import leaks into the management API process. Never raises."""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))


def frontend_base_url() -> str:
    return str(os.environ.get("FRONTEND_URL") or "").rstrip("/")


def send_plain_smtp(to_email: str, subject: str, body: str) -> bool:
    """Send one plain-text email. Returns True on success; never raises."""
    if not smtp_configured():
        logger.warning("SMTP not configured; dropping email to %s", to_email)
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(
            (str(os.environ.get("SMTP_SENDER_NAME") or "V-Bio"),
             str(os.environ["SMTP_USER"])))
        msg["To"] = to_email.strip()
        with smtplib.SMTP_SSL(
            str(os.environ["SMTP_HOST"]),
            int(os.environ.get("SMTP_PORT") or 465),
            timeout=30,
        ) as server:
            server.login(str(os.environ["SMTP_USER"]),
                         str(os.environ.get("SMTP_PASS") or ""))
            server.sendmail(str(os.environ["SMTP_USER"]), [to_email.strip()],
                            msg.as_string())
        logger.info("email sent to %s (%s)", to_email, subject)
        return True
    except Exception:
        logger.error("email FAILED to %s (%s)", to_email, subject, exc_info=True)
        return False
