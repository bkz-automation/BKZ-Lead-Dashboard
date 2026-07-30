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

    def test_insufficient_global_budget_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "at least 8 searches are required"):
            collector.collect(settings(max_searches=4))

    def test_contact_form_detection(self):
        soup = BeautifulSoup(
            '<form action="/send"><input type="email"><textarea name="message"></textarea></form>',
            "html.parser",
        )
        self.assertEqual(
            collector.contact_form_from_soup(soup, "https://example.ma/contact"),
            "https://example.ma/contact",
        )

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
