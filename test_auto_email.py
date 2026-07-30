import unittest
from datetime import datetime, timezone

import app


class AutomaticEmailTests(unittest.TestCase):
    def lead(self, **changes):
        value = {
            "lead_id": "lead_1",
            "company_name": "Genious Communications",
            "ai_buying_score": "63",
            "email": "tickets@genious.net",
            "contact_status": "New",
            "sent_at": "",
            "gmail_message_id": "",
        }
        value.update(changes)
        return value

    def run_sender(self, lead, send_email=None):
        sent = []
        updates = []
        result = app.auto_send_analyzed_lead(
            lead,
            "A complete personalised message for this qualified business lead.",
            send_email=send_email or (lambda recipient, subject, body: sent.append(
                (recipient, subject, body)
            ) or "gmail-123"),
            update_status=lambda lead_id, timestamp, message_id: updates.append(
                (lead_id, timestamp, message_id)
            ),
            now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        )
        return result, sent, updates

    def test_score_at_least_50_sends_and_updates_status(self):
        result, sent, updates = self.run_sender(self.lead())
        self.assertTrue(result)
        self.assertEqual(sent[0][0], "tickets@genious.net")
        self.assertEqual(updates, [("lead_1", "2026-07-30 12:00:00 +0000", "gmail-123")])

    def test_score_below_50_is_skipped(self):
        result, sent, updates = self.run_sender(self.lead(ai_buying_score="49"))
        self.assertFalse(result)
        self.assertEqual(sent, [])
        self.assertEqual(updates, [])

    def test_already_sent_is_skipped(self):
        for lead in (
            self.lead(sent_at="2026-07-29 10:00:00 +0000"),
            self.lead(gmail_message_id="gmail-existing"),
        ):
            result, sent, updates = self.run_sender(lead)
            self.assertFalse(result)
            self.assertEqual(sent, [])
            self.assertEqual(updates, [])

    def test_missing_email_is_skipped(self):
        result, sent, updates = self.run_sender(self.lead(email=""))
        self.assertFalse(result)
        self.assertEqual(sent, [])
        self.assertEqual(updates, [])

    def test_gmail_failure_does_not_update_status(self):
        def fail(*_args):
            raise RuntimeError("Gmail unavailable")

        with self.assertLogs(level="ERROR") as captured:
            result, _, updates = self.run_sender(self.lead(), send_email=fail)
        self.assertFalse(result)
        self.assertEqual(updates, [])
        self.assertEqual(self.lead()["contact_status"], "New")
        self.assertTrue(any("AUTO EMAIL FAILED" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
