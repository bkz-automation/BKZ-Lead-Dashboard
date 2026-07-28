"""Scheduled Gmail reply worker for the BKZ Lead Dashboard."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GmailCredentials
from googleapiclient.discovery import build


# Importing app reuses its production reply logic; suppress Streamlit runtime chatter.
logging.getLogger("streamlit").setLevel(logging.ERROR)
os.environ.setdefault("STREAMLIT_LOG_LEVEL", "error")

import app  # noqa: E402


def build_worker_gmail_service():
    """Authenticate non-interactively with the checked-out temporary token file."""
    token_path = Path(__file__).resolve().parent / "gmail_token.json"
    if not token_path.exists():
        raise FileNotFoundError("gmail_token.json is missing")
    credentials = GmailCredentials.from_authorized_user_file(
        str(token_path), app.GMAIL_SCOPES
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid or not credentials.has_scopes(app.GMAIL_SCOPES):
        raise RuntimeError("Gmail token is invalid or lacks gmail.modify access")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def run_reply_check() -> tuple[int, int, int, int]:
    """Scan recent inbox messages and return sanitized processing counters."""
    sheet_id = (
        os.getenv("GOOGLE_SHEET_ID", "").strip()
        or app.DEFAULT_GOOGLE_SHEET_ID
    )
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "Leads").strip() or "Leads"
    service_file = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
    ).strip()
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_api_key:
        raise ValueError("GROQ_API_KEY is missing")

    leads = app.load_fresh_google_leads(sheet_id, worksheet_name, service_file)
    processed_ids = {
        message_id.strip()
        for saved_ids in leads["gmail_message_id"]
        for message_id in str(saved_ids).split(",")
        if message_id.strip()
    }
    sent_leads_by_email = {
        str(row["email"]).strip().casefold(): row
        for _, row in leads.iterrows()
        if str(row["email"]).strip() and str(row["sent_at"]).strip()
    }

    gmail_service = build_worker_gmail_service()
    own_email = str(
        gmail_service.users().getProfile(userId="me").execute().get(
            "emailAddress", ""
        )
    ).strip().casefold()
    messages = app.recent_inbox_messages(gmail_service)
    replies_found = 0
    unmatched_replies = 0
    failed_messages = 0
    updated_lead_ids: set[str] = set()
    local_timezone = datetime.now().astimezone().tzinfo

    for message in messages:
        message_id = str(message.get("id", "")).strip()
        if not message_id or message_id in processed_ids:
            continue
        try:
            payload = message.get("payload", {})
            sender_email = parseaddr(
                app.gmail_header(payload, "From")
            )[1].strip().casefold()
            if not sender_email or sender_email == own_email:
                continue
            lead = sent_leads_by_email.get(sender_email)
            if lead is None:
                unmatched_replies += 1
                continue

            reply_timestamp = datetime.fromtimestamp(
                int(message.get("internalDate", "0")) / 1000,
                tz=local_timezone,
            )
            sent_timestamp = app.parsed_sheet_datetime(
                str(lead["sent_at"]), local_timezone
            )
            if sent_timestamp and reply_timestamp <= sent_timestamp:
                unmatched_replies += 1
                continue

            replies_found += 1
            reply_text = (
                app.gmail_plain_text(payload).strip()
                or str(message.get("snippet", "")).strip()
            )
            if not reply_text:
                failed_messages += 1
                continue
            classification = app.classify_reply_with_groq(
                groq_api_key, str(lead["company_name"]), reply_text
            )
            app.update_google_sheet_reply(
                sheet_id,
                worksheet_name,
                service_file,
                str(lead["lead_id"]),
                reply_timestamp.strftime("%Y-%m-%d %H:%M:%S %z"),
                classification["replyStatus"],
                classification["replySummary"],
                message_id,
            )
            processed_ids.add(message_id)
            updated_lead_ids.add(str(lead["lead_id"]))
        except Exception:
            # Never print message content or exception details from an individual email.
            failed_messages += 1
            continue

    return replies_found, len(updated_lead_ids), unmatched_replies, failed_messages


def main() -> int:
    try:
        found, updated, unmatched, failed = run_reply_check()
        print(
            "Reply check complete: "
            f"replies_found={found}, leads_updated={updated}, "
            f"unmatched_replies={unmatched}, failed_messages={failed}"
        )
        return 0
    except Exception as exc:
        # Top-level output remains useful but cannot leak credential or message content.
        print(f"Reply check failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
