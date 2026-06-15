from __future__ import annotations

import unittest
from pathlib import Path

from lead_generator.planning.adapters.ocella import OcellaCouncilConfig, OcellaPlanningScraper


FIXTURES = Path(__file__).parent / "fixtures"


class OcellaPlanningScraperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = OcellaPlanningScraper(
            OcellaCouncilConfig(
                authority="Example Ocella Council",
                base_url="https://planning.example.gov.uk",
            )
        )

    def test_parse_listing_extracts_application_ids(self) -> None:
        listing_html = (FIXTURES / "ocella_listing.html").read_text(encoding="utf-8")

        applications = self.scraper.parse_listing(
            listing_html,
            "https://planning.example.gov.uk/planning/search-results",
        )

        self.assertEqual(len(applications), 2)
        self.assertEqual(applications[0].uid, "OCELLA001")
        self.assertEqual(applications[0].reference, "24/09999/FUL")
        self.assertEqual(
            applications[0].url,
            "https://planning.example.gov.uk/planning/application-details?id=OCELLA001",
        )

    def test_parse_detail_extracts_fields_and_documents(self) -> None:
        detail_html = (FIXTURES / "ocella_detail.html").read_text(encoding="utf-8")

        application = self.scraper.parse_detail(
            detail_html,
            "https://planning.example.gov.uk/planning/application-details?id=OCELLA001",
        )

        self.assertEqual(application.uid, "OCELLA001")
        self.assertEqual(application.reference, "24/09999/FUL")
        self.assertEqual(application.address, "14 Station Road, Cardiff, CF10 1AA")
        self.assertEqual(application.postcode, "CF10 1AA")
        self.assertEqual(application.description, "Change of use to office")
        self.assertEqual(application.status, "Pending consideration")
        self.assertEqual(application.date_received, "2026-06-01")
        self.assertEqual(application.date_validated, "2026-06-03")
        self.assertEqual(application.applicant_name, "Example Developments Ltd")
        self.assertEqual(application.agent_name, "Planning Agent LLP")
        self.assertEqual(len(application.documents), 1)
        self.assertEqual(application.documents[0].title, "Planning statement.pdf")
        self.assertEqual(application.documents[0].date_published, "2026-06-15")

    def test_parse_detail_accepts_short_received_and_validated_labels(self) -> None:
        detail_html = """
        <html><body>
          <table>
            <tr><td>Reference</td><td>BR/111/24/PL</td></tr>
            <tr><td>Location</td><td>8 Argyle Road, Bognor Regis, PO21 1DY</td></tr>
            <tr><td>Received</td><td>21-06-24</td></tr>
            <tr><td>Validated</td><td>10-07-24</td></tr>
          </table>
        </body></html>
        """

        application = self.scraper.parse_detail(
            detail_html,
            "https://planning.example.gov.uk/planningDetails?reference=BR/111/24/PL",
        )

        self.assertEqual(application.date_received, "2024-06-21")
        self.assertEqual(application.date_validated, "2024-07-10")


if __name__ == "__main__":
    unittest.main()
