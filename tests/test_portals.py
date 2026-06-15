from __future__ import annotations

import unittest

from lead_generator.planning.portals import detect_portal_family


class PortalDetectionTest(unittest.TestCase):
    def test_detects_major_portal_families(self) -> None:
        cases = {
            "idox": ("<html>PublicAccess applicationDetails.do</html>", "https://example.gov.uk/online-applications"),
            "ocella": ("<meta name='generator' content='OcellaWeb'>", "https://example.gov.uk/planning"),
            "civica": ("<html>Civica Authority Public Access</html>", "https://example.gov.uk/planning"),
            "agile": ("<html>Agile Applications</html>", "https://example.gov.uk/apas/run/WPHAPPDETAIL.DisplayUrl"),
            "northgate": ("<html>Planning Explorer</html>", "https://example.gov.uk/PlanningExplorer/GeneralSearch.aspx"),
        }

        for expected, (html, url) in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(detect_portal_family(html, url), expected)

    def test_unknown_when_no_signature_matches(self) -> None:
        self.assertIsNone(detect_portal_family("<html>No known marker</html>", "https://example.gov.uk"))


if __name__ == "__main__":
    unittest.main()
