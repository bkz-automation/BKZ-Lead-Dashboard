# BKZ Lead Dashboard

A professional dark Streamlit dashboard for reviewing, filtering, qualifying, and contacting business leads. Google Sheets is the primary live data source, with `leads.csv` retained as a local fallback.

## Features

- Google Sheets lead loading and direct row updates
- Automatic CSV fallback with clear connection warnings
- KPI cards for total leads, qualified leads, emails sent, and average score
- Filters for contact status, minimum score, and location
- Full 12-column lead pipeline table with phone numbers preserved as text
- Direct Groq AI scoring, qualification, service recommendation, and outreach generation
- Persistent success and error notifications
- Per-lead email and WhatsApp actions using the saved personalised message
- AI Outreach Message panel with the complete saved Groq result

## Setup

1. Create and activate a Python virtual environment (Python 3.10 or newer recommended).
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a Google Cloud service account with the Google Sheets API and Google Drive API enabled.
4. Download its JSON key as `service_account.json` into this project folder.
5. Share the target spreadsheet with the service account's `client_email` address as an editor.
6. Copy `.env.example` to `.env` and configure it:

   ```env
   GOOGLE_SHEET_ID=1V4sKtbuJQy-9fMhg3GHyS16BcYW6A0Euheew8FutMvc
   GOOGLE_WORKSHEET_NAME=Leads
   GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
   LEADS_CSV_PATH=leads.csv
   GROQ_API_KEY=your_groq_api_key_here
   ```

   Create the Groq key in the [Groq Console](https://console.groq.com/keys). Keep `.env` and `service_account.json` local and never commit either file. The dashboard does not display or print credentials.
7. Start the app:

   ```bash
   streamlit run app.py
   ```

Streamlit normally opens the dashboard at `http://localhost:8501`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_SHEET_ID` | Project spreadsheet | Google spreadsheet identifier |
| `GOOGLE_WORKSHEET_NAME` | `Leads` | Worksheet containing lead records |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account.json` | Local service-account JSON path |
| `LEADS_CSV_PATH` | `leads.csv` | Local fallback CSV path |
| `GROQ_API_KEY` | empty | Required key for Groq AI analysis |

Google Sheets is always attempted first. If authentication, access, or connectivity fails, the dashboard displays a warning and loads `leads.csv`. The sidebar shows **GOOGLE SHEETS** when connected and **CSV FALLBACK** otherwise.

## Groq analysis and updates

Select a lead and click **Analyse with Groq AI**. The app validates Groq's JSON response, finds the matching Google Sheets row by `lead_id`, and saves `score`, `contact_status`, `personalised_message`, `recommended_service`, and `why_good_prospect`. It then refreshes the dashboard and retains the success or error message until dismissed.

If Google Sheets was unavailable when the dashboard loaded, the same fields are written to the local CSV fallback instead.

## Lead schema

The worksheet and fallback CSV use these columns:

`lead_id`, `company_name`, `industry`, `location`, `email`, `phone`, `score`, `contact_status`, `personalised_message`, `sent_at`, `recommended_service`, `why_good_prospect`.

Missing columns are normalized safely for display and added to Google Sheets when an update requires them. Sheet values are read as strings before normalization, preserving international phone-number formatting.
