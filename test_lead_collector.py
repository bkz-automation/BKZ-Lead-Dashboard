import unittest
from dataclasses import replace

from bs4 import BeautifulSoup

import lead_collector as collector


def settings(**changes):
    base = collector.Settings(
        cities=["Agadir", "Marrakech"], sectors=["Hotels", "Law Firms"],
        max_results=5, max_total=100, target_leads=20, max_searches=None,
        sources=["osm_overpass", "pages_maroc"], delay_min=0, delay_max=0,
        connect_timeout=1, read_timeout=1, fast_mode=True, allow_landlines=False,
        google_maps_api_key="", max_place_details=30, spreadsheet_id="x",
        worksheet_name="Leads", credentials_file="unused", dry_run=True,
    )
    return replace(base, **changes)


class CollectorTests(unittest.TestCase):
    def test_round_robin_plan_covers_cartesian_product_once(self):
        configured = settings()
        plan = collector.round_robin_search_plan(configured)
        triples = {(task.provider, task.city, task.sector) for task in plan}
        self.assertEqual(len(plan), 8)
        self.assertEqual(len(triples), 8)
        self.assertEqual({task.provider for task in plan[:2]}, set(configured.sources))

    def test_cursor_round_trip_through_plan(self):
        configured = settings()
        plan = collector.round_robin_search_plan(configured)
        for position, task in enumerate(plan):
            cursor = collector.cursor_for_task(configured, task)
            self.assertEqual(collector.cursor_position(configured, plan, cursor), position)

    def test_small_run_budget_stops_successfully_and_logs_next_cursor(self):
        configured = settings(max_searches=2)
        original_pipeline = collector.execute_source_pipeline
        calls = []
        collector.execute_source_pipeline = lambda _settings, provider, sector, city, *_args: (
            calls.append((provider, city, sector)) or ([], 0, 0, 0, set(), set(), 0, 0, 0, 0)
        )
        try:
            with self.assertLogs(level="INFO") as captured:
                result = collector.collect(configured)
        finally:
            collector.execute_source_pipeline = original_pipeline
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("run search budget exhausted; cursor saved" in line for line in captured.output))
        self.assertTrue(any("Next cursor position:" in line for line in captured.output))

    def test_contact_form_detection(self):
        soup = BeautifulSoup(
            '<form action="/send"><input type="email"><textarea name="message"></textarea></form>',
            "html.parser",
        )
        self.assertEqual(
            collector.contact_form_from_soup(soup, "https://example.ma/contact"),
            "https://example.ma/contact",
        )

    def test_phone_normalization_prefers_mobile_from_combined_provider_values(self):
        candidate = {
            "phone": "05 28 12 34 56 / 06 12 34 56 78",
            "_landline_phone": "0528998877",
        }
        self.assertEqual(collector.canonical_candidate_phone(candidate), "+212612345678")

    def test_landline_is_not_cleared_before_sheet_export(self):
        lead = collector.apply_phone_policy({"phone": "05 28 12 34 56"}, allow_landlines=False)
        self.assertEqual(lead["phone"], "+212528123456")
        self.assertEqual(lead["_phone_classification"], "landline")

    def test_all_provider_results_are_canonicalized_at_pipeline_boundary(self):
        original_enrich = collector.PublicWebClient.enrich_business
        collector.PublicWebClient.enrich_business = lambda self, item, sector, city: (
            collector.directory_lead_to_sheet(item, sector, city)
        )
        try:
            for provider in ("osm_overpass", "pages_maroc", "maroc_annuaire", "pj_ma"):
                original_adapter = collector.SOURCE_ADAPTERS[provider]
                collector.SOURCE_ADAPTERS[provider] = lambda *_: [{
                    "company_name": "Provider Company", "location": "Agadir, Morocco",
                    "website": "", "phone": "05 28 12 34 56", "email": "",
                    "source_url": "https://directory.example/company",
                    "business_description": "Business",
                }]
                try:
                    leads, *_ = collector.execute_source_pipeline(
                        settings(cities=["Agadir"], sectors=["Hotels"], sources=[provider]),
                        provider, "Hotels", "Agadir", 5, disabled_sources={provider},
                    )
                finally:
                    collector.SOURCE_ADAPTERS[provider] = original_adapter
                self.assertEqual(leads[0]["phone"], "+212528123456", provider)
        finally:
            collector.PublicWebClient.enrich_business = original_enrich

    def test_sheet_writer_canonicalizes_and_logs_phone(self):
        class Worksheet:
            rows = None

            def col_values(self, _column):
                return ["lead_id"]

            def update(self, rows, **_kwargs):
                self.rows = rows

        worksheet = Worksheet()
        lead = {
            "lead_id": "lead_1", "company_name": "Company", "industry": "Hotels",
            "location": "Agadir, Morocco", "email": "contact@example.ma",
            "phone": "05 28 12 34 56", "website": "https://example.ma",
        }
        with self.assertLogs(level="INFO") as captured:
            collector.write_new_leads(worksheet, [lead], collector.SHEET_COLUMNS)
        self.assertEqual(worksheet.rows[0][collector.SHEET_COLUMNS.index("phone")], "+212528123456")
        self.assertTrue(any("Company: Company" in line for line in captured.output))
        self.assertTrue(any("Phone: +212528123456" in line for line in captured.output))

    def test_ai_score_uses_requested_category_weights(self):
        result = collector.calculate_ai_buying_score({
            "company_name": "Atlas Luxury Group Hotel",
            "industry": "Hotels",
            "website": "https://atlas.ma",
            "email": "",
            "phone": "+212612345678",
            "whatsapp_confirmed": True,
            "contact_form_url": "https://atlas.ma/contact",
            "business_description": (
                "Premium multi-location resort offering multiple services, 24 7 guest support, "
                "online reservations, spa services, and active expansion."
            ),
            "automation_opportunity": "Automate reservations and repetitive guest inquiries.",
        })
        components = result["breakdown"]["components"]
        self.assertEqual(
            {name: component["max"] for name, component in components.items()},
            {"automation_opportunity": 40, "business_maturity": 20, "contactability": 15,
             "buying_intent": 15, "ai_fit": 10},
        )
        self.assertGreaterEqual(result["score"], 90)
        self.assertEqual(result["recommended_offer"], "AI Reservation Assistant")
        self.assertGreaterEqual(len(result["reasons"]), 3)
        self.assertLessEqual(len(result["reasons"]), 8)

    def test_missing_email_changes_contactability_only(self):
        base = {
            "company_name": "Atlas Hotel", "industry": "Hotels",
            "website": "https://atlas.ma", "phone": "+212612345678",
            "business_description": "Hotel with online reservations and guest support services.",
        }
        without_email = collector.calculate_ai_buying_score(base)
        with_email = collector.calculate_ai_buying_score({**base, "email": "info@atlas.ma"})
        left = without_email["breakdown"]["components"]
        right = with_email["breakdown"]["components"]
        for category in ("automation_opportunity", "business_maturity", "buying_intent", "ai_fit"):
            self.assertEqual(left[category]["points"], right[category]["points"])
        self.assertEqual(right["contactability"]["points"] - left["contactability"]["points"], 3)

    def test_website_only_candidate_is_accepted(self):
        configured = settings(cities=["Agadir"], sectors=["Hotels"], sources=["pages_maroc"])
        original_adapter = collector.SOURCE_ADAPTERS["pages_maroc"]
        original_enrich = collector.PublicWebClient.enrich_business
        collector.SOURCE_ADAPTERS["pages_maroc"] = lambda *_: [{
            "company_name": "Atlas Hotel", "location": "Agadir, Morocco",
            "website": "https://atlas.example", "phone": "", "email": "",
            "source_url": "https://directory.example/atlas", "business_description": "Hotel",
        }]
        collector.PublicWebClient.enrich_business = lambda self, item, sector, city: (
            collector.directory_lead_to_sheet(item, sector, city)
        )
        try:
            leads, rejected, discovered, enriched, *_ = collector.execute_source_pipeline(
                configured, "pages_maroc", "Hotels", "Agadir", 5,
                disabled_sources=set(configured.sources),
            )
        finally:
            collector.SOURCE_ADAPTERS["pages_maroc"] = original_adapter
            collector.PublicWebClient.enrich_business = original_enrich
        self.assertEqual(len(leads), 1)
        self.assertEqual(rejected, 0)
        self.assertEqual(discovered, 1)
        self.assertEqual(enriched, 1)


if __name__ == "__main__":
    unittest.main()
