"""BKZ Lead Dashboard - a polished Streamlit app for qualifying business leads."""

from __future__ import annotations

import json
import logging
import os
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import parseaddr
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import gspread
import requests
import streamlit as st
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GmailCredentials
from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from gspread.utils import rowcol_to_a1


APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_GOOGLE_SHEET_ID = "1V4sKtbuJQy-9fMhg3GHyS16BcYW6A0Euheew8FutMvc"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
REQUIRED_COLUMNS = [
    "lead_id",
    "company_name",
    "industry",
    "location",
    "email",
    "phone",
    "score",
    "contact_status",
    "personalised_message",
    "sent_at",
    "recommended_service",
    "why_good_prospect",
    "website",
    "business_description",
    "automation_opportunity",
    "replied_at",
    "reply_status",
    "reply_summary",
    "gmail_message_id",
    "ai_buying_score",
]


def streamlit_secret(name: str) -> str | None:
    """Return one Streamlit secret without failing during local execution."""
    try:
        value = st.secrets[name]
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def streamlit_json_secret(name: str) -> dict[str, Any] | None:
    """Parse a JSON TOML secret in memory without exposing its contents."""
    raw_value = streamlit_secret(name)
    if raw_value is None:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Streamlit secret {name} is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Streamlit secret {name} must contain a JSON object.")
    return parsed
QUALIFICATION_LABELS = {
    "high_priority": "High Priority",
    "qualified": "Qualified",
    "manual_review": "Manual Review",
    "low_priority": "Low Priority",
}


def qualification_for_score(score: int) -> str:
    """Return the only valid qualification for a 1-10 Groq score."""
    if score >= 8:
        return "high_priority"
    if score >= 6:
        return "qualified"
    if score >= 4:
        return "manual_review"
    return "low_priority"


def qualification_label(qualification: str) -> str:
    """Convert Groq's machine-readable qualification to a dashboard label."""
    return QUALIFICATION_LABELS[qualification]


def sample_leads() -> pd.DataFrame:
    """Return dependable sample data when the configured source is unavailable."""
    rows = [
        ["BKZ-001", "Northstar Analytics", "Technology", "London", "hello@northstar.example", "+447700900101", 9, "High Priority", "Hi Maya, we can help Northstar turn more analytics interest into qualified pipeline.", "2026-07-24 09:30", "", ""],
        ["BKZ-002", "Atlas & Co", "Consulting", "Manchester", "growth@atlas.example", "+447700900102", 7, "Qualified", "Hi James, I have a practical idea for improving Atlas & Co's lead follow-up.", "2026-07-22 14:15", "", ""],
        ["BKZ-003", "Verdant Works", "Sustainability", "Bristol", "team@verdant.example", "+447700900103", 8, "High Priority", "Hi Priya, your sustainability work stood out; here is a way to scale outreach without losing relevance.", "", "", ""],
        ["BKZ-004", "Forge Studio", "Design", "Leeds", "studio@forge.example", "+447700900104", 5, "Manual Review", "Hi Alex, we would love to show Forge Studio a simpler route from prospect research to conversation.", "", "", ""],
        ["BKZ-005", "Harbour Finance", "Financial Services", "London", "partnerships@harbour.example", "+447700900105", 8, "High Priority", "Hi Sofia, we have identified a focused outreach opportunity for Harbour Finance.", "2026-07-25 11:05", "", ""],
        ["BKZ-006", "Cedar Health", "Healthcare", "Birmingham", "contact@cedar.example", "+447700900106", 6, "Qualified", "Hi Daniel, our lead qualification workflow could help Cedar Health prioritise the right partnerships.", "", "", ""],
        ["BKZ-007", "Kite Commerce", "E-commerce", "Edinburgh", "sales@kite.example", "+447700900107", 10, "High Priority", "Hi Emma, we see a strong opportunity to accelerate Kite Commerce's outbound pipeline.", "2026-07-26 16:20", "", ""],
        ["BKZ-008", "Bluefield Logistics", "Logistics", "Liverpool", "ops@bluefield.example", "+447700900108", 3, "Low Priority", "Hi Noah, we prepared a short idea for making Bluefield's prospecting more efficient.", "", "", ""],
    ]
    return normalise_leads(pd.DataFrame(rows, columns=REQUIRED_COLUMNS[:12]))


def normalise_leads(data: pd.DataFrame) -> pd.DataFrame:
    """Guarantee the dashboard schema and safe data types."""
    frame = data.copy()
    for column in REQUIRED_COLUMNS:
        if column not in frame.columns:
            frame[column] = 0 if column == "score" else ""
    frame = frame[REQUIRED_COLUMNS]
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce").fillna(1).clip(1, 10)
    for column in REQUIRED_COLUMNS:
        if column != "score":
            frame[column] = frame[column].fillna("").astype(str)
    return frame


def connect_google_worksheet(sheet_id: str, worksheet_name: str, service_file: str) -> Any:
    """Authenticate securely and return the configured Google Sheets worksheet."""
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not configured.")
    service_account_info = streamlit_json_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if service_account_info is not None:
        credentials = Credentials.from_service_account_info(
            service_account_info, scopes=GOOGLE_SCOPES
        )
    else:
        credentials_path = Path(service_file)
        if not credentials_path.is_absolute():
            credentials_path = APP_DIR / credentials_path
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Service account file not found: {credentials_path.name}"
            )
        credentials = Credentials.from_service_account_file(
            str(credentials_path), scopes=GOOGLE_SCOPES
        )
    client = gspread.authorize(credentials)
    return client.open_by_key(sheet_id).worksheet(worksheet_name)


@st.cache_data(ttl=60, show_spinner=False)
def load_leads(
    sheet_id: str,
    worksheet_name: str,
    service_file: str,
    csv_path: str,
) -> tuple[pd.DataFrame, str | None, bool]:
    """Load Google Sheets leads, using the local CSV only as a fallback."""
    try:
        worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
        values = worksheet.get_all_values()
        if not values:
            raise ValueError("The Google Sheets worksheet is empty.")
        headers = [str(header).strip() for header in values[0]]
        for column in REQUIRED_COLUMNS:
            if column not in headers:
                headers.append(column)
                worksheet.update_cell(1, len(headers), column)
        rows = [
            row[:len(headers)] + [""] * max(0, len(headers) - len(row))
            for row in values[1:]
        ]
        return normalise_leads(pd.DataFrame(rows, columns=headers)), None, True
    except Exception as sheets_exc:
        sheets_error = str(sheets_exc).strip() or type(sheets_exc).__name__
    try:
        path = Path(csv_path)
        if not path.is_absolute():
            path = APP_DIR / path
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path.name}")
        # Read identifiers and phone numbers as text so formatting such as "+44" is preserved.
        leads = normalise_leads(pd.read_csv(path, dtype=str))
        warning = f"Google Sheets connection failed ({sheets_error}). Using leads.csv fallback."
        return leads, warning, False
    except (OSError, ValueError) as csv_exc:
        warning = (
            f"Google Sheets connection failed ({sheets_error}) and the CSV fallback "
            f"could not be loaded ({csv_exc}). Showing sample data."
        )
        return sample_leads(), warning, False


def parse_groq_analysis(content: str) -> dict[str, Any]:
    """Parse and validate the structured lead analysis returned by Groq."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Groq returned invalid JSON.") from exc

    required = {
        "leadScore",
        "qualification",
        "recommendedService",
        "whyGoodProspect",
        "concreteProblem",
        "automationWorkflow",
        "practicalBenefit",
    }
    if not isinstance(result, dict) or not required.issubset(result):
        raise ValueError("Groq's response is missing one or more required fields.")
    if isinstance(result["leadScore"], bool) or not isinstance(result["leadScore"], int):
        raise ValueError("Groq's leadScore must be an integer.")
    if not 1 <= result["leadScore"] <= 10:
        raise ValueError("Groq's leadScore must be between 1 and 10.")
    if not isinstance(result["qualification"], str) or not result["qualification"].strip():
        raise ValueError("Groq's qualification must be a non-empty string.")
    qualification = result["qualification"].strip().lower()
    if qualification not in QUALIFICATION_LABELS:
        allowed = ", ".join(QUALIFICATION_LABELS)
        raise ValueError(f"Groq's qualification must be one of: {allowed}.")
    expected_qualification = qualification_for_score(result["leadScore"])
    if qualification != expected_qualification:
        raise ValueError(
            "Groq's qualification does not match its leadScore. "
            f"A score of {result['leadScore']} requires {expected_qualification}."
        )
    result["qualification"] = qualification
    for field in (
        "recommendedService",
        "whyGoodProspect",
        "concreteProblem",
        "automationWorkflow",
        "practicalBenefit",
    ):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(f"Groq's {field} must be a non-empty string.")
        result[field] = result[field].strip()
    return result


def build_personalised_message(company_name: str, analysis: dict[str, Any]) -> str:
    """Build and validate outreach using the controlled French template."""
    company_name = str(company_name).strip()
    if not company_name:
        raise ValueError("The lead's company_name is required to build outreach.")

    concrete_problem = analysis["concreteProblem"].strip()
    automation_workflow = " ".join(analysis["automationWorkflow"].split())
    practical_benefit = analysis["practicalBenefit"].strip()
    workflow_prefix = "bkz peut mettre en place"
    normalized_workflow = automation_workflow.casefold()
    if not normalized_workflow.startswith(workflow_prefix):
        raise ValueError(
            'Groq\'s automationWorkflow must start with "BKZ peut mettre en place".'
        )
    if normalized_workflow.count(workflow_prefix) != 1:
        raise ValueError(
            'Groq repeated "BKZ peut mettre en place" in automationWorkflow.'
        )
    message = f"""Bonjour {company_name},

{concrete_problem}

{automation_workflow}

{practical_benefit}

Nous pouvons vous préparer une courte démonstration gratuite adaptée à votre activité.

Site : https://bkz-automation.github.io/bkz-automation/
WhatsApp : +212708434058"""

    banned_phrases = (
        "défis opérationnels importants",
        "améliorer votre productivité",
        "tâches répétitives",
        "nous sommes convaincus",
        "nous serions ravis de vous rencontrer",
    )
    lowered_message = message.casefold()
    used_banned = [phrase for phrase in banned_phrases if phrase in lowered_message]
    if used_banned:
        raise ValueError("Groq returned prohibited generic wording.")

    components = [
        concrete_problem.casefold(),
        automation_workflow.casefold(),
        practical_benefit.casefold(),
    ]
    if len(set(components)) != len(components):
        raise ValueError("Groq repeated the same outreach component.")

    word_count = len(message.split())
    if not 65 <= word_count <= 140:
        raise ValueError(
            f"The controlled outreach message must contain 65 to 140 words; received {word_count}."
        )
    return message


def analyse_with_groq(api_key: str, lead: pd.Series) -> dict[str, Any]:
    """Send one lead to Groq and return a validated analysis."""
    if not api_key:
        raise ValueError("GROQ_API_KEY is missing. Add it to the local .env file.")
    lead_payload = lead.to_dict()
    for field in ("website", "business_description", "automation_opportunity"):
        lead_payload[field] = str(lead.get(field, ""))
    prompt = f"""Analyse this business lead for BKZ and return raw JSON only.
Do not use Markdown, commentary, or code fences. Use exactly these fields:
{{"leadScore": <integer 1 to 10, never 0>,
"qualification": <"high_priority", "qualified", "manual_review", or "low_priority">,
"recommendedService": <non-empty string>,
"whyGoodProspect": <non-empty string>,
"concreteProblem": <non-empty string>,
"automationWorkflow": <non-empty string>,
"practicalBenefit": <non-empty string>}}

BKZ business context:
- Business name: BKZ
- Services: n8n automation, AI agents, dashboards, lead qualification, email automation,
  WhatsApp automation, and business process automation
- Website: https://bkz-automation.github.io/bkz-automation/
- WhatsApp: +212708434058

The qualification must exactly match the leadScore:
- 8 to 10: "high_priority"
- 6 to 7: "qualified"
- 4 to 5: "manual_review"
- 1 to 3: "low_priority"
Never reject a lead. Scores 4 or 5 must always be "manual_review".

Evaluate the leadScore using balanced evidence from:
- the company name, industry, location, and other available company information;
- the website value and whether it appears usable;
- the business description;
- the identified automation opportunity;
- the availability of public email and phone contact details;
- the company's realistic fit with BKZ services.

Missing information reduces confidence but must not automatically produce a score of 1/10.
If the lead has a usable website, public contact details, a clear business description, and a
realistic automation opportunity, it should normally score at least 4 unless the supplied data
contains a clear, specific reason for a lower score. The qualification must always match the
score range exactly.

Controlled outreach components:
- Do not write "personalisedMessage". Python builds the final message from your three components.
- Write concreteProblem, automationWorkflow, and practicalBenefit in professional French, using
  only facts supported by company_name, industry, location, website, business_description, and
  automation_opportunity.
- Use automation_opportunity as the central structure. Do not introduce a second problem,
  workflow, service, or benefit.
- concreteProblem: 25 to 35 words describing one precise likely operational or commercial issue.
  Do not include a greeting, company name salutation, BKZ introduction, solution, or call to action.
- automationWorkflow: a complete professional French sentence of 35 to 50 words starting exactly
  with "BKZ peut mettre en place". Use that phrase once only. Explain the exact trigger, captured
  information, centralisation, qualification or routing, and relevant follow-up or visibility.
  Do not repeat the problem.
- practicalBenefit: 15 to 25 words stating one direct business result only. Do not include a
  greeting, solution, workflow, service, or call to action; the Python template adds the demo CTA.
- recommendedService must name the single BKZ service used by automationWorkflow.
- whyGoodProspect must be concise and based only on supplied lead data.
- For real-estate leads, focus only on supported flows among availability requests, property
  information, buyer or tenant qualification, WhatsApp and email follow-up, reservation or visit
  follow-up, and lead centralisation.
- Mention WhatsApp, email, Google Sheets, lead qualification, automated follow-ups, or dashboards
  only where relevant to the supplied automation opportunity.
- Never use "défis opérationnels importants", "améliorer votre productivité", "tâches
  répétitives", "nous sommes convaincus", or "nous serions ravis de vous rencontrer".
- Do not claim access to private systems or internal processes. Do not invent facts, metrics,
  people, tools, problems, or outcomes.
- Return raw JSON only. Do not use Markdown, greetings, signatures, website links, or WhatsApp
  numbers inside the three outreach components.

Lead data: {json.dumps(lead_payload, ensure_ascii=False)}"""
    try:
        response = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a business lead qualification analyst. Return valid raw JSON only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        analysis = parse_groq_analysis(payload["choices"][0]["message"]["content"])
        analysis["personalisedMessage"] = build_personalised_message(
            str(lead.get("company_name", "")), analysis
        )
        return analysis
    except requests.RequestException as exc:
        detail = ""
        if exc.response is not None:
            try:
                detail = exc.response.json().get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                pass
        raise RuntimeError(f"Groq API request failed{f': {detail}' if detail else '.'}") from exc
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Groq returned an unexpected response structure.") from exc


def update_lead_csv(csv_path: str, lead_id: str, analysis: dict[str, Any]) -> None:
    """Persist Groq results while retaining phone numbers as exact strings."""
    path = Path(csv_path)
    if not path.is_absolute():
        path = APP_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"Cannot update missing CSV file: {path.name}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "lead_id" not in frame.columns:
        raise ValueError("The CSV does not contain a lead_id column.")
    matches = frame["lead_id"].astype(str) == str(lead_id)
    if not matches.any():
        raise ValueError(f"Lead {lead_id} was not found in {path.name}.")
    frame.loc[matches, "score"] = str(analysis["leadScore"])
    frame.loc[matches, "contact_status"] = qualification_label(analysis["qualification"])
    frame.loc[matches, "personalised_message"] = analysis["personalisedMessage"]
    frame.loc[matches, "recommended_service"] = analysis["recommendedService"]
    frame.loc[matches, "why_good_prospect"] = analysis["whyGoodProspect"]
    frame.to_csv(path, index=False)


def update_google_sheet(
    sheet_id: str,
    worksheet_name: str,
    service_file: str,
    lead_id: str,
    analysis: dict[str, Any],
    preserve_contact_status: bool = False,
) -> None:
    """Update the matching lead directly in Google Sheets."""
    try:
        worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
        values = worksheet.get_all_values()
        if not values:
            raise ValueError("The Google Sheets worksheet is empty.")

        headers = [str(header).strip() for header in values[0]]
        for column in REQUIRED_COLUMNS:
            if column not in headers:
                headers.append(column)
                worksheet.update_cell(1, len(headers), column)
        lead_id_column = headers.index("lead_id")

        row_number = None
        for index, row in enumerate(values[1:], start=2):
            value = row[lead_id_column] if lead_id_column < len(row) else ""
            if str(value).strip() == str(lead_id):
                row_number = index
                break
        if row_number is None:
            raise ValueError(f"Lead {lead_id} was not found in Google Sheets.")

        updates = {
            "score": str(analysis["leadScore"]),
            "personalised_message": analysis["personalisedMessage"],
            "recommended_service": analysis["recommendedService"],
            "why_good_prospect": analysis["whyGoodProspect"],
        }
        if not preserve_contact_status:
            updates["contact_status"] = qualification_label(analysis["qualification"])
        worksheet.batch_update([
            {
                "range": rowcol_to_a1(row_number, headers.index(column) + 1),
                "values": [[value]],
            }
            for column, value in updates.items()
        ])
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Google Sheets update failed: {detail}") from exc


def gmail_credentials() -> GmailCredentials:
    """Load or create Gmail OAuth credentials without exposing token contents."""
    cloud_token_info = streamlit_json_secret("GMAIL_TOKEN_JSON")
    cloud_client_info = streamlit_json_secret("GMAIL_CREDENTIALS_JSON")
    cloud_mode = any(
        streamlit_secret(name) is not None
        for name in (
            "GROQ_API_KEY",
            "GOOGLE_SHEET_ID",
            "GOOGLE_WORKSHEET_NAME",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GMAIL_CREDENTIALS_JSON",
            "GMAIL_TOKEN_JSON",
        )
    )
    if cloud_mode:
        if cloud_token_info is None:
            raise RuntimeError("Streamlit secret GMAIL_TOKEN_JSON is required.")
        token_info = dict(cloud_token_info)
        client_config = cloud_client_info or {}
        oauth_client = client_config.get("installed") or client_config.get("web") or {}
        for key in ("client_id", "client_secret", "token_uri"):
            if not token_info.get(key) and oauth_client.get(key):
                token_info[key] = oauth_client[key]
        try:
            credentials = GmailCredentials.from_authorized_user_info(
                token_info, GMAIL_SCOPES
            )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError(
                "Streamlit Gmail credentials are invalid or could not be refreshed."
            ) from exc
        if not credentials.valid or not credentials.has_scopes(GMAIL_SCOPES):
            raise RuntimeError(
                "Streamlit Gmail token is invalid or lacks gmail.modify access."
            )
        return credentials

    credentials_path = APP_DIR / "gmail_credentials.json"
    token_path = APP_DIR / "gmail_token.json"
    if not credentials_path.exists():
        raise FileNotFoundError("Gmail credentials file not found: gmail_credentials.json")

    credentials = None
    if token_path.exists():
        try:
            credentials = GmailCredentials.from_authorized_user_file(
                str(token_path), GMAIL_SCOPES
            )
        except (ValueError, OSError):
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid or not credentials.has_scopes(GMAIL_SCOPES):
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), GMAIL_SCOPES
        )
        credentials = flow.run_local_server(port=0)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_gmail_service() -> Any:
    """Build an authenticated Gmail API service."""
    try:
        return build(
            "gmail", "v1", credentials=gmail_credentials(), cache_discovery=False
        )
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Gmail authentication failed: {detail}") from exc


def send_gmail_email(
    recipient: str, subject: str, body: str, gmail_service: Any | None = None
) -> str:
    """Send the saved outreach message from the authenticated Gmail account."""
    try:
        message = MIMEText(body, "plain", "utf-8")
        message["To"] = recipient
        message["Subject"] = subject
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        service = gmail_service or build_gmail_service()
        result = service.users().messages().send(
            userId="me", body={"raw": raw_message}
        ).execute()
        message_id = str(result.get("id", "")).strip()
        if not message_id:
            raise RuntimeError("Gmail send succeeded without returning a message ID")
        return message_id
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Gmail send failed: {detail}") from exc


def update_google_sheet_email_status(
    sheet_id: str,
    worksheet_name: str,
    service_file: str,
    lead_id: str,
    sent_at: str,
) -> None:
    """Mark the matching Google Sheets lead as sent with a timestamp."""
    try:
        worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
        values = worksheet.get_all_values()
        if not values:
            raise ValueError("The Google Sheets worksheet is empty.")

        headers = [str(header).strip() for header in values[0]]
        for column in ("contact_status", "sent_at"):
            if column not in headers:
                headers.append(column)
                worksheet.update_cell(1, len(headers), column)
        if "lead_id" not in headers:
            raise ValueError("The Google Sheets worksheet has no lead_id column.")

        lead_id_column = headers.index("lead_id")
        row_number = None
        for index, row in enumerate(values[1:], start=2):
            value = row[lead_id_column] if lead_id_column < len(row) else ""
            if str(value).strip() == str(lead_id):
                row_number = index
                break
        if row_number is None:
            raise ValueError(f"Lead {lead_id} was not found in Google Sheets.")

        updates = {"contact_status": "Sent", "sent_at": sent_at}
        worksheet.batch_update([
            {
                "range": rowcol_to_a1(row_number, headers.index(column) + 1),
                "values": [[value]],
            }
            for column, value in updates.items()
        ])
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Google Sheets email-status update failed: {detail}") from exc


def update_google_sheet_auto_email_status(
    sheet_id: str,
    worksheet_name: str,
    service_file: str,
    lead_id: str,
    sent_at: str,
    gmail_message_id: str,
) -> None:
    """Persist the three fields owned by a successful automatic Gmail send."""
    worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
    values = worksheet.get_all_values()
    if not values:
        raise ValueError("The Google Sheets worksheet is empty.")
    headers = [str(header).strip() for header in values[0]]
    required = {"lead_id", "contact_status", "sent_at", "gmail_message_id"}
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError("Google Sheets automatic-email columns missing: " + ", ".join(missing))
    lead_id_column = headers.index("lead_id")
    row_number = next((
        index for index, row in enumerate(values[1:], start=2)
        if lead_id_column < len(row) and str(row[lead_id_column]).strip() == str(lead_id)
    ), None)
    if row_number is None:
        raise ValueError(f"Lead {lead_id} was not found in Google Sheets.")
    updates = {
        "contact_status": "Email Sent",
        "sent_at": sent_at,
        "gmail_message_id": gmail_message_id,
    }
    worksheet.batch_update([{
        "range": rowcol_to_a1(row_number, headers.index(column) + 1),
        "values": [[value]],
    } for column, value in updates.items()])


def is_automatic_email_eligible(lead: Any, personalised_message: str) -> bool:
    """Apply the automatic-send gate without side effects."""
    try:
        ai_score = float(str(lead.get("ai_buying_score", "")).strip())
    except (TypeError, ValueError):
        return False
    return bool(
        ai_score >= 50
        and is_valid_email_address(str(lead.get("email", "")).strip())
        and str(lead.get("contact_status", "")).strip().casefold() == "new"
        and not str(lead.get("sent_at", "")).strip()
        and not str(lead.get("gmail_message_id", "")).strip()
        and str(personalised_message or "").strip()
    )


def auto_send_analyzed_lead(
    lead: Any,
    personalised_message: str,
    send_email: Any = send_gmail_email,
    update_status: Any | None = None,
    now: Any | None = None,
) -> bool:
    """Send one eligible analyzed lead; failures never mutate lead status."""
    if not is_automatic_email_eligible(lead, personalised_message):
        return False
    company = str(lead.get("company_name", "")).strip()
    recipient = str(lead.get("email", "")).strip()
    score = str(lead.get("ai_buying_score", "")).strip()
    subject = f"Une idÃ©e pour amÃ©liorer {company}"
    body = email_body_with_opt_out(personalised_message)
    try:
        message_id = send_email(recipient, subject, body)
        if not str(message_id or "").strip():
            raise RuntimeError("Gmail did not return a message ID")
        timestamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S %z")
        if update_status is None:
            raise RuntimeError("Automatic email status updater is not configured")
        update_status(str(lead.get("lead_id", "")), timestamp, str(message_id))
        logging.info("AUTO EMAIL SENT")
        logging.info("Lead: %s", company)
        logging.info("Score: %s", score)
        logging.info("Recipient: %s", recipient)
        return True
    except Exception as exc:
        logging.error("AUTO EMAIL FAILED | Lead: %s | Recipient: %s | Error: %s", company, recipient, exc)
        return False


def load_fresh_google_leads(
    sheet_id: str, worksheet_name: str, service_file: str
) -> pd.DataFrame:
    """Read uncached Google Sheets data for queue and rate-limit decisions."""
    try:
        worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
        values = worksheet.get_all_values()
        if not values:
            raise ValueError("The Google Sheets worksheet is empty.")
        headers = [str(header).strip() for header in values[0]]
        for column in REQUIRED_COLUMNS:
            if column not in headers:
                headers.append(column)
                worksheet.update_cell(1, len(headers), column)
        rows = [
            row[:len(headers)] + [""] * max(0, len(headers) - len(row))
            for row in values[1:]
        ]
        return normalise_leads(pd.DataFrame(rows, columns=headers))
    except (ValueError, OSError):
        raise
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Could not refresh Google Sheets before sending: {detail}") from exc


def enforce_email_send_limits(fresh_leads: pd.DataFrame) -> None:
    """Enforce a maximum of 10 daily sends and a five-minute cooldown."""
    now = datetime.now().astimezone()
    sent_times: list[datetime] = []
    for value in fresh_leads["sent_at"]:
        if not str(value).strip():
            continue
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            continue
        timestamp = parsed.to_pydatetime()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=now.tzinfo)
        else:
            timestamp = timestamp.astimezone(now.tzinfo)
        sent_times.append(timestamp)

    sent_today = sum(timestamp.date() == now.date() for timestamp in sent_times)
    if sent_today >= 10:
        raise RuntimeError(
            "Daily email limit reached: 10 emails have already been sent today."
        )
    if sent_times:
        next_allowed = max(sent_times) + timedelta(minutes=5)
        if now < next_allowed:
            wait_minutes = max(1, ceil((next_allowed - now).total_seconds() / 60))
            raise RuntimeError(
                f"Email cooldown active. Wait at least {wait_minutes} more minute(s) before sending."
            )


def email_body_with_opt_out(saved_message: str) -> str:
    """Insert the French opt-out sentence immediately before the website footer."""
    opt_out = (
        "Si ce message ne vous concerne pas, dites-le-moi et je ne vous recontacterai pas."
    )
    if opt_out in saved_message:
        return saved_message

    lines = saved_message.splitlines()
    footer_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "https://bkz-automation.github.io/bkz-automation/" in line
        ),
        None,
    )
    if footer_index is not None:
        lines.insert(footer_index, opt_out)
        return "\n".join(lines)

    return (
        f"{saved_message.rstrip()}\n\n{opt_out}\n"
        "Site : https://bkz-automation.github.io/bkz-automation/\n"
        "WhatsApp : +212708434058"
    )


def gmail_header(payload: dict[str, Any], name: str) -> str:
    """Read one Gmail message header without exposing message content."""
    for header in payload.get("headers", []):
        if str(header.get("name", "")).casefold() == name.casefold():
            return str(header.get("value", ""))
    return ""


def is_valid_email_address(value: str) -> bool:
    """Perform a conservative validation for an outreach recipient address."""
    email = str(value).strip()
    if not email or any(character.isspace() for character in email):
        return False
    parsed = parseaddr(email)[1]
    if parsed.casefold() != email.casefold():
        return False
    local_part, separator, domain = email.rpartition("@")
    return bool(separator and local_part and "." in domain and not domain.startswith("."))


def gmail_plain_text(payload: dict[str, Any]) -> str:
    """Extract the text/plain portion of a Gmail payload recursively."""
    if payload.get("mimeType") == "text/plain":
        encoded = payload.get("body", {}).get("data", "")
        if encoded:
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                return ""
    for part in payload.get("parts", []) or []:
        text = gmail_plain_text(part)
        if text:
            return text
    return ""


def classify_reply_with_groq(
    api_key: str, company_name: str, reply_text: str
) -> dict[str, str]:
    """Classify and summarize one reply using Groq raw JSON output."""
    if not api_key:
        raise ValueError("GROQ_API_KEY is missing. Add it to the local .env file.")
    prompt = f"""Classify this email reply to BKZ outreach for {company_name}.
Return raw JSON only with exactly these fields:
{{"replyStatus": <one of "Interested", "Wants Demo", "Needs Info", "Not Interested", "Other">,
"replySummary": <a concise factual French summary, maximum 40 words>}}
Use only the reply content. Do not add facts or Markdown.

Reply content:
{reply_text[:4000]}"""
    try:
        response = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "Classify email replies and return valid raw JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(content)
    except requests.RequestException as exc:
        raise RuntimeError("Groq reply classification request failed.") from exc
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Groq returned invalid reply-classification JSON.") from exc

    allowed = {"Interested", "Wants Demo", "Needs Info", "Not Interested", "Other"}
    status = result.get("replyStatus") if isinstance(result, dict) else None
    summary = result.get("replySummary") if isinstance(result, dict) else None
    if status not in allowed:
        raise ValueError("Groq returned an unsupported reply status.")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Groq returned an empty reply summary.")
    return {"replyStatus": status, "replySummary": summary.strip()}


def recent_inbox_messages(gmail_service: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Fetch recent inbox messages, isolating failures to individual messages."""
    response = gmail_service.users().messages().list(
        userId="me", q="in:inbox newer_than:30d", maxResults=limit
    ).execute()
    messages: list[dict[str, Any]] = []
    for reference in response.get("messages", []):
        try:
            messages.append(
                gmail_service.users().messages().get(
                    userId="me", id=reference["id"], format="full"
                ).execute()
            )
        except Exception:
            # A malformed or inaccessible message must not stop the remaining scan.
            continue
    return messages


def parsed_sheet_datetime(value: str, local_timezone: Any) -> datetime | None:
    """Parse a Sheets timestamp and normalize it to the local timezone."""
    if not str(value).strip():
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = parsed.to_pydatetime()
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=local_timezone)
    return timestamp.astimezone(local_timezone)


def update_google_sheet_reply(
    sheet_id: str,
    worksheet_name: str,
    service_file: str,
    lead_id: str,
    replied_at: str,
    reply_status: str,
    reply_summary: str,
    gmail_message_id: str,
) -> None:
    """Persist a classified Gmail reply and its processed message ID."""
    try:
        worksheet = connect_google_worksheet(sheet_id, worksheet_name, service_file)
        values = worksheet.get_all_values()
        if not values:
            raise ValueError("The Google Sheets worksheet is empty.")
        headers = [str(header).strip() for header in values[0]]
        for column in REQUIRED_COLUMNS:
            if column not in headers:
                headers.append(column)
                worksheet.update_cell(1, len(headers), column)
        lead_id_column = headers.index("lead_id")
        row_number = None
        row_values: list[str] = []
        for index, row in enumerate(values[1:], start=2):
            value = row[lead_id_column] if lead_id_column < len(row) else ""
            if str(value).strip() == str(lead_id):
                row_number = index
                row_values = row
                break
        if row_number is None:
            raise ValueError(f"Lead {lead_id} was not found in Google Sheets.")

        message_id_column = headers.index("gmail_message_id")
        saved_ids = (
            row_values[message_id_column]
            if message_id_column < len(row_values)
            else ""
        )
        processed_ids = [value.strip() for value in saved_ids.split(",") if value.strip()]
        if gmail_message_id not in processed_ids:
            processed_ids.append(gmail_message_id)
        updates = {
            "contact_status": "Replied",
            "replied_at": replied_at,
            "reply_status": reply_status,
            "reply_summary": reply_summary,
            "gmail_message_id": ",".join(processed_ids),
        }
        worksheet.batch_update([
            {
                "range": rowcol_to_a1(row_number, headers.index(column) + 1),
                "values": [[value]],
            }
            for column, value in updates.items()
        ])
    except (ValueError, OSError):
        raise
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"Google Sheets reply update failed: {detail}") from exc


def inject_theme() -> None:
    """Apply the dashboard's dark visual system."""
    st.markdown(
        """
        <style>
        .stApp { background: #080b12; color: #e7ecf5; }
        [data-testid="stSidebar"] { background: #0d111b; border-right: 1px solid #202738; }
        [data-testid="stHeader"] { background: rgba(8,11,18,.78); }
        .block-container { max-width: 1480px; padding-top: 2rem; padding-bottom: 3rem; }
        .eyebrow { color: #7c8ba5; font-size: .72rem; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }
        .hero-title { font-size: 2.25rem; font-weight: 750; letter-spacing: -.04em; margin: .2rem 0; }
        .hero-copy { color: #8f9bb0; margin-bottom: 1.6rem; }
        div[data-testid="stMetric"] { background: linear-gradient(145deg,#111724,#0d121d); border: 1px solid #222c3e; border-radius: 16px; padding: 1.15rem 1.2rem; box-shadow: 0 10px 30px rgba(0,0,0,.18); }
        div[data-testid="stMetric"] label { color: #8f9bb0; }
        div[data-testid="stMetricValue"] { color: #f4f7fb; font-weight: 700; }
        .section-title { font-size: 1.05rem; font-weight: 650; margin: 1.6rem 0 .35rem; }
        .section-copy { color: #7f8ba0; font-size: .9rem; margin-bottom: 1rem; }
        .status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#38d39f; box-shadow:0 0 12px #38d39f; margin-right:8px; }
        .source-note { color:#8996aa; font-size:.82rem; }
        .stButton button, .stLinkButton a { border-radius: 10px !important; font-weight: 600 !important; }
        [data-testid="stDataFrame"] { border: 1px solid #222c3e; border-radius: 14px; overflow: hidden; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="BKZ Lead Dashboard", page_icon="◈", layout="wide")
    inject_theme()

    csv_path = os.getenv("LEADS_CSV_PATH", "leads.csv")
    sheet_id = (
        streamlit_secret("GOOGLE_SHEET_ID")
        or os.getenv("GOOGLE_SHEET_ID", "").strip()
        or DEFAULT_GOOGLE_SHEET_ID
    )
    worksheet_name = (
        streamlit_secret("GOOGLE_WORKSHEET_NAME")
        or os.getenv("GOOGLE_WORKSHEET_NAME", "Leads").strip()
        or "Leads"
    )
    service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json").strip()
    groq_api_key = (
        streamlit_secret("GROQ_API_KEY")
        or os.getenv("GROQ_API_KEY", "").strip()
    )
    leads, warning, sheets_connected = load_leads(
        sheet_id, worksheet_name, service_file, csv_path
    )

    with st.sidebar:
        st.markdown("### ◈ BKZ")
        st.caption("LEAD INTELLIGENCE")
        st.divider()
        st.markdown("#### Filters")
        status_order = ["High Priority", "Qualified", "Manual Review", "Low Priority"]
        available_statuses = {value for value in leads["contact_status"].unique() if value}
        statuses = [status for status in status_order if status in available_statuses]
        statuses.extend(sorted(available_statuses.difference(status_order)))
        selected_statuses = st.multiselect("Contact status", statuses, placeholder="All statuses")
        min_score = st.slider("Minimum score", 1, 10, 1, 1)
        locations = sorted(value for value in leads["location"].unique() if value)
        selected_locations = st.multiselect("Location", locations, placeholder="All locations")
        st.divider()
        source_label = "GOOGLE SHEETS" if sheets_connected else "CSV FALLBACK"
        st.markdown(f'<p class="source-note"><span class="status-dot"></span>Source: {source_label}</p>', unsafe_allow_html=True)

    filtered = leads[leads["score"] >= min_score]
    if selected_statuses:
        filtered = filtered[filtered["contact_status"].isin(selected_statuses)]
    if selected_locations:
        filtered = filtered[filtered["location"].isin(selected_locations)]

    st.markdown('<div class="eyebrow">Revenue operations / Lead intelligence</div>', unsafe_allow_html=True)
    st.markdown('<div class="hero-title">BKZ Lead Dashboard</div>', unsafe_allow_html=True)
    st.markdown('<div class="hero-copy">Qualify prospects, prioritise outreach, and move every conversation forward.</div>', unsafe_allow_html=True)
    if warning:
        st.warning(warning)

    metric_cols = st.columns(4)
    total = len(leads)
    qualified_statuses = {"high priority", "qualified"}
    qualified = int(leads["contact_status"].str.casefold().isin(qualified_statuses).sum())
    emails_sent = int(leads["sent_at"].str.strip().ne("").sum())
    average_score = leads["score"].mean() if total else 0
    for column, label, value in zip(
        metric_cols,
        ["Total Leads", "Qualified Leads", "Emails Sent", "Average Score"],
        [f"{total:,}", f"{qualified:,}", f"{emails_sent:,}", f"{average_score:.1f}/10"],
    ):
        column.metric(label, value)

    reply_metric_cols = st.columns(4)
    replies_received = int(leads["replied_at"].str.strip().ne("").sum())
    reply_statuses = leads["reply_status"].str.strip().str.casefold()
    interested_leads = int(reply_statuses.eq("interested").sum())
    demo_requests = int(reply_statuses.eq("wants demo").sum())
    follow_ups_needed = int(
        reply_statuses.isin({"interested", "wants demo", "needs info"}).sum()
    )
    for column, label, value in zip(
        reply_metric_cols,
        ["Replies Received", "Interested Leads", "Demo Requests", "Follow-ups Needed"],
        [replies_received, interested_leads, demo_requests, follow_ups_needed],
    ):
        column.metric(label, f"{value:,}")

    title_col, refresh_col = st.columns([6, 1])
    with title_col:
        st.markdown('<div class="section-title">Lead pipeline</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="section-copy">Showing {len(filtered):,} of {total:,} leads</div>', unsafe_allow_html=True)
    with refresh_col:
        st.write("")
        if st.button("↻ Refresh Leads", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
        height=390,
        column_config={
            "score": st.column_config.ProgressColumn("score", min_value=1, max_value=10, format="%d"),
            "personalised_message": st.column_config.TextColumn("personalised_message", width="large"),
            "website": st.column_config.TextColumn("website", width="medium"),
            "business_description": st.column_config.TextColumn("business_description", width="large"),
            "automation_opportunity": st.column_config.TextColumn("automation_opportunity", width="large"),
        },
    )

    st.markdown('<div class="section-title">Reply Tracking</div>', unsafe_allow_html=True)
    reply_rows = leads[
        leads["replied_at"].str.strip().ne("")
        | leads["reply_status"].str.strip().ne("")
    ][
        [
            "company_name",
            "email",
            "contact_status",
            "replied_at",
            "reply_status",
            "reply_summary",
        ]
    ]
    if reply_rows.empty:
        st.info("No Gmail replies have been recorded yet.")
    else:
        st.dataframe(reply_rows, use_container_width=True, hide_index=True)

    st.markdown('<div class="section-title">Lead actions</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-copy">Choose a lead, then launch the next step.</div>', unsafe_allow_html=True)
    notification_message = st.session_state.get("notification_message")
    notification_type = st.session_state.get("notification_type")
    if notification_message:
        message_col, dismiss_col = st.columns([6, 1])
        with message_col:
            if notification_type == "success":
                st.success(notification_message)
            else:
                st.error(notification_message)
        with dismiss_col:
            if st.button("Dismiss message", key="dismiss_notification_message", use_container_width=True):
                st.session_state.pop("notification_message", None)
                st.session_state.pop("notification_type", None)
                st.rerun()

    if st.button("Check Replies", key="check_gmail_replies"):
        try:
            if not sheets_connected:
                raise RuntimeError(
                    "Google Sheets must be connected before checking Gmail replies."
                )
            fresh_leads = load_fresh_google_leads(
                sheet_id, worksheet_name, service_file
            )
            processed_message_ids = {
                message_id.strip()
                for saved_ids in fresh_leads["gmail_message_id"]
                for message_id in str(saved_ids).split(",")
                if message_id.strip()
            }
            sent_leads_by_email = {
                str(row["email"]).strip().casefold(): row
                for _, row in fresh_leads.iterrows()
                if str(row["email"]).strip() and str(row["sent_at"]).strip()
            }

            gmail_api = build_gmail_service()
            own_email = str(
                gmail_api.users().getProfile(userId="me").execute().get(
                    "emailAddress", ""
                )
            ).strip().casefold()
            messages = recent_inbox_messages(gmail_api)
            replies_found = 0
            unmatched_replies = 0
            failed_classifications = 0
            update_failures = 0
            updated_lead_ids: set[str] = set()
            local_timezone = datetime.now().astimezone().tzinfo

            with st.spinner("Checking recent Gmail replies..."):
                for message in messages:
                    message_id = str(message.get("id", "")).strip()
                    if not message_id or message_id in processed_message_ids:
                        continue
                    try:
                        payload = message.get("payload", {})
                        sender_email = parseaddr(
                            gmail_header(payload, "From")
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
                        sent_timestamp = parsed_sheet_datetime(
                            str(lead["sent_at"]), local_timezone
                        )
                        if sent_timestamp and reply_timestamp <= sent_timestamp:
                            unmatched_replies += 1
                            continue

                        replies_found += 1
                        reply_text = (
                            gmail_plain_text(payload).strip()
                            or str(message.get("snippet", "")).strip()
                        )
                        if not reply_text:
                            failed_classifications += 1
                            continue
                        try:
                            classification = classify_reply_with_groq(
                                groq_api_key,
                                str(lead["company_name"]),
                                reply_text,
                            )
                        except (ValueError, RuntimeError, OSError):
                            failed_classifications += 1
                            continue

                        replied_at = reply_timestamp.strftime(
                            "%Y-%m-%d %H:%M:%S %z"
                        )
                        try:
                            update_google_sheet_reply(
                                sheet_id,
                                worksheet_name,
                                service_file,
                                str(lead["lead_id"]),
                                replied_at,
                                classification["replyStatus"],
                                classification["replySummary"],
                                message_id,
                            )
                        except (ValueError, RuntimeError, OSError):
                            update_failures += 1
                            continue
                        processed_message_ids.add(message_id)
                        updated_lead_ids.add(str(lead["lead_id"]))
                    except Exception:
                        # Isolate malformed messages without exposing their content.
                        failed_classifications += 1
                        continue

            st.session_state["notification_message"] = (
                f"Reply scan summary — replies found: {replies_found}; "
                f"leads updated: {len(updated_lead_ids)}; "
                f"unmatched replies: {unmatched_replies}; "
                f"failed classifications: {failed_classifications}; "
                f"sheet update failures: {update_failures}."
            )
            st.session_state["notification_type"] = (
                "success"
                if failed_classifications == 0 and update_failures == 0
                else "error"
            )
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            if isinstance(exc, (ValueError, RuntimeError, OSError)):
                message = str(exc)
            else:
                message = (
                    f"Reply scan failed ({type(exc).__name__}). "
                    "Verify Gmail and Google Sheets access."
                )
            st.session_state["notification_message"] = message
            st.session_state["notification_type"] = "error"
            st.rerun()

    if st.button("Send Next Qualified", key="send_next_qualified"):
        try:
            if not sheets_connected:
                raise RuntimeError(
                    "Google Sheets must be connected before queued email sending."
                )

            fresh_leads = load_fresh_google_leads(
                sheet_id, worksheet_name, service_file
            )
            enforce_email_send_limits(fresh_leads)
            sent_this_session = set(
                st.session_state.get("gmail_sent_lead_ids", [])
            )
            eligible = fresh_leads[
                fresh_leads["contact_status"].str.casefold().isin(
                    {"high priority", "qualified"}
                )
                & fresh_leads["lead_id"].str.strip().ne("")
                & fresh_leads["email"].map(is_valid_email_address)
                & fresh_leads["personalised_message"].str.strip().ne("")
                & fresh_leads["sent_at"].str.strip().eq("")
                & ~fresh_leads["lead_id"].isin(sent_this_session)
            ]
            if eligible.empty:
                raise ValueError(
                    "No queued High Priority or Qualified lead is ready to email."
                )

            queued_lead = eligible.iloc[0]
            queued_lead_id = str(queued_lead["lead_id"])
            queued_company = str(queued_lead["company_name"])
            queued_recipient = str(queued_lead["email"]).strip()
            queued_body = email_body_with_opt_out(
                str(queued_lead["personalised_message"])
            )
            queued_subject = f"Une idée pour améliorer {queued_company}"

            with st.spinner(f"Sending queued email to {queued_company}..."):
                send_gmail_email(queued_recipient, queued_subject, queued_body)
                sent_this_session.add(queued_lead_id)
                st.session_state["gmail_sent_lead_ids"] = sorted(sent_this_session)
                sent_at = datetime.now().astimezone().strftime(
                    "%Y-%m-%d %H:%M:%S %z"
                )
                try:
                    update_google_sheet_email_status(
                        sheet_id,
                        worksheet_name,
                        service_file,
                        queued_lead_id,
                        sent_at,
                    )
                except (ValueError, RuntimeError, OSError) as exc:
                    raise RuntimeError(
                        f"Email sent to {queued_company}, but Google Sheets could not be updated: {exc}"
                    ) from exc

            st.session_state["notification_message"] = (
                f"Queued email sent successfully to {queued_company} "
                f"({queued_recipient})."
            )
            st.session_state["notification_type"] = "success"
            st.cache_data.clear()
            st.rerun()
        except (ValueError, RuntimeError, OSError) as exc:
            st.session_state["notification_message"] = str(exc)
            st.session_state["notification_type"] = "error"
            st.rerun()
    if filtered.empty:
        st.info("No leads match the current filters.")
        return

    options = filtered["lead_id"] + " — " + filtered["company_name"]
    selected_label = st.selectbox("Selected lead", options.tolist(), label_visibility="collapsed")
    selected_id = selected_label.split(" — ", 1)[0]
    lead = filtered.loc[filtered["lead_id"] == selected_id].iloc[0]

    analyse_col, email_col, whatsapp_col = st.columns(3)
    with analyse_col:
        if st.button("Analyse with Groq AI", use_container_width=True, type="primary"):
            try:
                with st.spinner("Analysing lead with Groq AI..."):
                    analysis = analyse_with_groq(groq_api_key, lead)
                    auto_eligible = bool(
                        sheets_connected
                        and is_automatic_email_eligible(lead, analysis["personalisedMessage"])
                    )
                    if sheets_connected:
                        update_google_sheet(
                            sheet_id,
                            worksheet_name,
                            service_file,
                            str(lead["lead_id"]),
                            analysis,
                            preserve_contact_status=auto_eligible,
                        )
                        if auto_eligible:
                            fresh_leads = load_fresh_google_leads(
                                sheet_id, worksheet_name, service_file
                            )
                            fresh_match = fresh_leads[
                                fresh_leads["lead_id"].astype(str).eq(str(lead["lead_id"]))
                            ]
                            if not fresh_match.empty:
                                enforce_email_send_limits(fresh_leads)
                                auto_send_analyzed_lead(
                                    fresh_match.iloc[0],
                                    analysis["personalisedMessage"],
                                    update_status=lambda lead_id, sent_at, message_id: (
                                        update_google_sheet_auto_email_status(
                                            sheet_id, worksheet_name, service_file,
                                            lead_id, sent_at, message_id,
                                        )
                                    ),
                                )
                    else:
                        update_lead_csv(csv_path, str(lead["lead_id"]), analysis)
                st.session_state["notification_message"] = (
                    f"Analysis saved: {qualification_label(analysis['qualification'])} "
                    f"({analysis['leadScore']}/10). Dashboard refreshed."
                )
                st.session_state["notification_type"] = "success"
                st.cache_data.clear()
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.session_state["notification_message"] = str(exc)
                st.session_state["notification_type"] = "error"
                st.rerun()
    outreach_message = str(lead["personalised_message"])
    recipient_email = str(lead["email"]).strip()
    email_subject = f"Une idée pour améliorer {lead['company_name']}"
    email_body = quote(outreach_message)
    with email_col:
        if st.button("✉ Send Email", use_container_width=True):
            try:
                if not is_valid_email_address(recipient_email):
                    raise ValueError("This lead does not have a valid email address.")
                if not outreach_message.strip():
                    raise ValueError("The selected lead has no saved personalised message.")
                if not sheets_connected:
                    raise RuntimeError(
                        "Google Sheets must be connected before sending so the lead can be updated."
                    )
                fresh_leads = load_fresh_google_leads(
                    sheet_id, worksheet_name, service_file
                )
                enforce_email_send_limits(fresh_leads)
                fresh_match = fresh_leads[
                    fresh_leads["lead_id"].astype(str).eq(str(lead["lead_id"]))
                ]
                if fresh_match.empty:
                    raise ValueError(
                        f"Lead {lead['lead_id']} was not found in the refreshed Google Sheet."
                    )
                fresh_lead = fresh_match.iloc[0]
                fresh_recipient = str(fresh_lead["email"]).strip()
                if not is_valid_email_address(fresh_recipient):
                    raise ValueError("This lead does not have a valid email address.")
                sent_lead_ids = set(
                    st.session_state.get("gmail_sent_lead_ids", [])
                )
                if (
                    str(fresh_lead["contact_status"]).strip().casefold() == "sent"
                    or bool(str(fresh_lead["sent_at"]).strip())
                    or str(lead["lead_id"]) in sent_lead_ids
                ):
                    raise ValueError("An email has already been sent to this lead.")
                recipient_email = fresh_recipient
                gmail_body = email_body_with_opt_out(outreach_message)
                with st.spinner("Sending email through Gmail..."):
                    send_gmail_email(recipient_email, email_subject, gmail_body)
                    sent_lead_ids.add(str(lead["lead_id"]))
                    st.session_state["gmail_sent_lead_ids"] = sorted(sent_lead_ids)
                    sent_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
                    try:
                        update_google_sheet_email_status(
                            sheet_id,
                            worksheet_name,
                            service_file,
                            str(lead["lead_id"]),
                            sent_at,
                        )
                    except (ValueError, RuntimeError, OSError) as exc:
                        raise RuntimeError(
                            f"Email sent, but Google Sheets could not be updated: {exc}"
                        ) from exc
                st.session_state["notification_message"] = (
                    f"Email sent successfully to {recipient_email}."
                )
                st.session_state["notification_type"] = "success"
                st.cache_data.clear()
                st.rerun()
            except (ValueError, RuntimeError, OSError) as exc:
                st.session_state["notification_message"] = str(exc)
                st.session_state["notification_type"] = "error"
                st.rerun()
    phone = "".join(char for char in str(lead["phone"]) if char.isdigit())
    with whatsapp_col:
        if phone:
            st.link_button("◉ Open WhatsApp", f"https://wa.me/{phone}?text={email_body}", use_container_width=True)
        else:
            st.button("◉ Open WhatsApp", disabled=True, use_container_width=True)

    st.markdown('<div class="section-title">AI Outreach Message</div>', unsafe_allow_html=True)
    recommended_service = str(lead["recommended_service"]).strip()
    why_good_prospect = str(lead["why_good_prospect"]).strip()
    has_ai_analysis = bool(recommended_service or why_good_prospect)

    if has_ai_analysis:
        with st.container(border=True):
            company_col, score_col, qualification_col = st.columns([2, 1, 1])
            company_col.metric("Company name", str(lead["company_name"]))
            score_col.metric("AI score", f"{int(lead['score'])}/10")
            qualification_col.metric("Qualification", str(lead["contact_status"]))

            service_col, prospect_col = st.columns(2)
            with service_col:
                st.markdown("**Recommended service**")
                st.write(recommended_service or "Not provided")
            with prospect_col:
                st.markdown("**Why this company is a good prospect**")
                st.write(why_good_prospect or "Not provided")

            st.text_area(
                "Complete personalised outreach message",
                value=outreach_message,
                height=220,
                disabled=True,
                key=f"ai_outreach_{lead['lead_id']}_{outreach_message}",
            )
    else:
        st.info("Analyse this lead with Groq AI to generate and display the complete outreach result.")


if __name__ == "__main__":
    main()
