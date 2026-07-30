# BKZ Lead Dashboard

A dark Streamlit lead-operations dashboard with Google Sheets, Groq AI qualification, Gmail sending and reply monitoring, WhatsApp outreach, safe email queuing, reply KPIs, and persistent notifications.

The same application supports local Windows use and Streamlit Community Cloud. Streamlit secrets take precedence online; local `.env` variables and JSON credential files remain the fallback.

## Local Windows setup

1. Create and activate a Python virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and configure it:

   ```env
   GROQ_API_KEY=your_groq_api_key
   GOOGLE_SHEET_ID=1V4sKtbuJQy-9fMhg3GHyS16BcYW6A0Euheew8FutMvc
   GOOGLE_WORKSHEET_NAME=Leads
   GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
   LEADS_CSV_PATH=leads.csv
   ```

3. Place these local credential files in the project root:

   - `service_account.json` — Google service account with spreadsheet access
   - `gmail_credentials.json` — Gmail OAuth client configuration
   - `gmail_token.json` — authorized Gmail token with `gmail.modify` scope

4. Share the spreadsheet with the service account's `client_email` as an editor.
5. Start the dashboard:

   ```powershell
   streamlit run app.py
   ```

If the local Gmail token expires and has a refresh token, it refreshes automatically. If no valid local token exists, the existing local browser OAuth flow is used.

## Streamlit Community Cloud deployment

1. Push the application code, `requirements.txt`, and non-secret project files to a private or appropriately secured GitHub repository.
2. Create a Streamlit Community Cloud app and select `app.py` as the entry point.
3. Open the app's **Advanced settings → Secrets** and add the following TOML configuration.

```toml
GROQ_API_KEY = "your_groq_api_key"
GOOGLE_SHEET_ID = "1V4sKtbuJQy-9fMhg3GHyS16BcYW6A0Euheew8FutMvc"
GOOGLE_WORKSHEET_NAME = "Leads"

GOOGLE_SERVICE_ACCOUNT_JSON = '''
{
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "your-private-key-id",
  "private_key": "-----BEGIN PRIVATE KEY-----\nREPLACE_WITH_PRIVATE_KEY\n-----END PRIVATE KEY-----\n",
  "client_email": "your-service-account@your-project.iam.gserviceaccount.com",
  "client_id": "your-client-id",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "your-certificate-url"
}
'''

GMAIL_CREDENTIALS_JSON = '''
{
  "installed": {
    "client_id": "your-client-id",
    "client_secret": "your-client-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"]
  }
}
'''

GMAIL_TOKEN_JSON = '''
{
  "token": "your-access-token",
  "refresh_token": "your-refresh-token",
  "token_uri": "https://oauth2.googleapis.com/token",
  "client_id": "your-client-id",
  "client_secret": "your-client-secret",
  "scopes": ["https://www.googleapis.com/auth/gmail.modify"]
}
'''
```

Use the complete, valid JSON from the corresponding local credential files—not the placeholders above. TOML triple-quoted strings preserve the embedded JSON and escaped private-key newlines.

On Streamlit Community Cloud:

- Google service-account credentials are created in memory with `Credentials.from_service_account_info(...)`.
- Gmail credentials are created in memory from `GMAIL_TOKEN_JSON` and refreshed automatically when possible.
- The app never writes Streamlit secret values into the repository or credential files.
- Interactive Gmail browser authorization is disabled. Supply a valid token with a refresh token and `gmail.modify` scope.

## Exact Streamlit secret names

- `GROQ_API_KEY`
- `GOOGLE_SHEET_ID`
- `GOOGLE_WORKSHEET_NAME`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GMAIL_CREDENTIALS_JSON`
- `GMAIL_TOKEN_JSON`

## Security warning

Never commit `.env`, `.streamlit/secrets.toml`, `service_account.json`, `gmail_credentials.json`, or `gmail_token.json`. They are covered by the project `.gitignore`, but verify staged files before every commit. Never paste real credentials into README files, source code, issues, logs, or screenshots.

## Data and fallback behavior

Google Sheets is the primary data source. When it is unavailable locally, the dashboard uses `leads.csv` as a fallback and displays that source in the sidebar. Google Sheets remains required for Gmail status and reply updates.

The application preserves all lead qualification, Gmail sending, safe queue, reply monitoring, filters, KPIs, WhatsApp, notification, and AI Outreach Message features in both environments.

## Collector scheduling and contact policy

The collector builds a deterministic round-robin plan over every configured
city, sector, and provider. Each workflow run processes 50 provider searches by
default. Its city, sector, and provider cursor is stored in the workbook's
`_collector_state` worksheet after successful lead writes. The next hourly run
resumes at that exact position, and completion of the full plan wraps the cursor
back to its beginning. Workflow concurrency prevents overlapping runs from
racing the persistent cursor. A run can still finish early after reaching its
deduplicated lead target; its next cursor is saved in the same way.

Each run reports searches by provider, city, and sector, remaining budget, and
the exact stop reason. Candidates qualify with any business contact channel:
website, phone, WhatsApp, or email. Official homepages and bounded Contact/About
pages are checked for email, Moroccan phone numbers, WhatsApp links, and contact
forms. WhatsApp and contact-form values are exported into worksheet columns that
the collector creates when absent.
