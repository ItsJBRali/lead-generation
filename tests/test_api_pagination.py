from __future__ import annotations

from datetime import date
import json
import unittest
from urllib.parse import quote

from lead_generator.planning.adapters.arcus import ArcusCouncilConfig, ArcusPlanningScraper
from lead_generator.planning.adapters.civica import CivicaCouncilConfig, CivicaPlanningScraper
from lead_generator.planning.http import CouncilFetchError, FetchResponse


class CappedCivicaHttp:
    """A row-range endpoint that enforces its own smaller response size."""

    def __init__(self, ids, *, cap=2, include_total=True, repeat=False):
        self.records = [{"KeyNumber": uid} for uid in ids]
        self.cap = cap
        self.include_total = include_total
        self.repeat = repeat
        self.ranges = []

    def get(self, url):
        return FetchResponse(url=url, status_code=200, text=json.dumps({"SearchItems": []}))

    def post_json(self, url, data):
        self.ranges.append((data["fromRow"], data["toRow"]))
        if len(self.ranges) > 10:
            raise AssertionError("Pagination did not terminate")
        start = 0 if self.repeat else data["fromRow"] - 1
        count = min(self.cap, data["toRow"] - data["fromRow"] + 1)
        payload = {"KeyObjects": self.records[start:start + count]}
        if self.include_total:
            payload["TotalRows"] = len(self.records)
        return FetchResponse(url=url, status_code=200, text=json.dumps(payload))


class ArcusWindowHttp:
    def __init__(self, windows):
        self.windows = windows
        self.requested = []

    def get(self, url):
        boot = {"fwuid": "test", "loaded": {"app": "test"}}
        encoded = quote(json.dumps(boot), safe="")
        return FetchResponse(url=url, status_code=200, text=f'<script src="/s/sfsites/l/{encoded}/bootstrap.js"></script>')

    def post_form(self, url, data):
        request = json.loads(data["message"])["actions"][0]["params"]["params"]["request"]
        fields = {field["fieldDeveloperName"]: field["fieldValue"] for field in request["searchFilters"]}
        window = (fields["PA_ADV_DateValidFrom"], fields["PA_ADV_DateValidTo"])
        self.requested.append(window)
        ids, threshold = self.windows[window]
        result = {"records": [{"Id": uid, "Name": uid} for uid in ids], "thresholdHit": threshold}
        return FetchResponse(url=url, status_code=200, text=json.dumps({"actions": [{"returnValue": {"returnValue": result}}]}))


class ApiPaginationTest(unittest.TestCase):
    def civica_search(self, http, *, limit=None):
        scraper = CivicaPlanningScraper(CivicaCouncilConfig("Example", "https://example.test/"), http_client=http)
        return scraper._discover_json_applications(
            "https://example.test/api/", "https://example.test/planning", "", "GFPlanning", limit=limit
        )

    def test_civica_server_cap_does_not_skip_rows_with_total(self):
        http = CappedCivicaHttp(["1", "2", "3", "4", "5"])
        applications = self.civica_search(http)
        self.assertEqual([app.uid for app in applications], ["1", "2", "3", "4", "5"])
        self.assertEqual([start for start, _ in http.ranges], [1, 3, 5])

    def test_civica_server_cap_without_total_continues_until_empty(self):
        http = CappedCivicaHttp(["1", "2", "3"], include_total=False)
        applications = self.civica_search(http)
        self.assertEqual([app.uid for app in applications], ["1", "2", "3"])
        self.assertEqual([start for start, _ in http.ranges], [1, 3, 4])

    def test_civica_total_counts_rows_before_deduplication(self):
        http = CappedCivicaHttp(["1", "2", "2", "3"])
        applications = self.civica_search(http)
        self.assertEqual([app.uid for app in applications], ["1", "2", "3"])
        self.assertEqual(len(http.ranges), 2)

    def test_civica_explicit_limit_applies_across_capped_pages(self):
        http = CappedCivicaHttp(["1", "2", "3", "4", "5"])
        applications = self.civica_search(http, limit=3)
        self.assertEqual([app.uid for app in applications], ["1", "2", "3"])
        self.assertEqual(len(http.ranges), 2)

    def test_civica_repeated_page_reports_incomplete_discovery(self):
        http = CappedCivicaHttp([str(i) for i in range(200)], cap=100, repeat=True)
        with self.assertRaisesRegex(CouncilFetchError, "repeat"):
            self.civica_search(http)
        self.assertEqual(len(http.ranges), 2)

    def test_civica_empty_page_before_reported_total_is_incomplete(self):
        class MissingPage(CappedCivicaHttp):
            def post_json(self, url, data):
                response = super().post_json(url, data)
                payload = json.loads(response.text)
                if data['fromRow'] >= 3:
                    payload['KeyObjects'] = []
                return FetchResponse(url=url, status_code=200, text=json.dumps(payload))
        with self.assertRaisesRegex(CouncilFetchError, 'incomplete'):
            self.civica_search(MissingPage(['1', '2', '3']))

    def test_arcus_keeps_splitting_threshold_window_without_immediate_growth(self):
        http = ArcusWindowHttp({
            ("2026-06-01", "2026-06-04"): (["1", "2"], True),
            ("2026-06-01", "2026-06-02"): (["1", "2"], True),
            ("2026-06-03", "2026-06-04"): ([], False),
            ("2026-06-01", "2026-06-01"): (["1", "2"], False),
            ("2026-06-02", "2026-06-02"): (["3"], False),
        })
        scraper = ArcusPlanningScraper(ArcusCouncilConfig("Example", "https://example.test/pr"), http_client=http)
        result = scraper.discover_ids(listing_url="https://example.test/pr/s/register", start_date=date(2026, 6, 1), end_date=date(2026, 6, 4))
        self.assertEqual([app.uid for app in result.applications], ["1", "2", "3"])

    def test_arcus_reports_threshold_when_date_window_cannot_be_split(self):
        day = date(2026, 6, 1)
        for start, end in ((day, day), (None, None), (day, None), (None, day)):
            with self.subTest(start=start, end=end):
                window = (start.isoformat() if start else "", end.isoformat() if end else "")
                http = ArcusWindowHttp({window: (["1"], True)})
                scraper = ArcusPlanningScraper(ArcusCouncilConfig("Example", "https://example.test/pr"), http_client=http)
                with self.assertRaisesRegex(CouncilFetchError, "incomplete.*threshold"):
                    scraper.discover_ids(listing_url="https://example.test/pr/s/register", start_date=start, end_date=end)
                self.assertEqual(len(http.requested), 1)

    def test_arcus_reports_threshold_when_server_ignores_date_splits(self):
        http = ArcusWindowHttp({
            ("2026-06-01", "2026-06-02"): (["1"], True),
            ("2026-06-01", "2026-06-01"): (["1"], True),
            ("2026-06-02", "2026-06-02"): (["1"], True),
        })
        scraper = ArcusPlanningScraper(ArcusCouncilConfig("Example", "https://example.test/pr"), http_client=http)
        with self.assertRaisesRegex(CouncilFetchError, "incomplete.*threshold"):
            scraper.discover_ids(listing_url="https://example.test/pr/s/register", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2))

    def test_arcus_explicit_limit_can_be_satisfied_by_a_capped_page(self):
        http = ArcusWindowHttp({('', ''): (['1', '2'], True)})
        scraper = ArcusPlanningScraper(ArcusCouncilConfig('Example', 'https://example.test/pr'), http_client=http)
        result = scraper.discover_ids(listing_url='https://example.test/pr/s/register', limit=1)
        self.assertEqual([app.uid for app in result.applications], ['1'])
        self.assertEqual(len(http.requested), 1)

    def test_arcus_limit_counts_results_from_both_date_windows(self):
        http = ArcusWindowHttp({
            ('2026-06-01', '2026-06-02'): (['1', '2'], True),
            ('2026-06-01', '2026-06-01'): (['1', '2'], False),
            ('2026-06-02', '2026-06-02'): (['3', '4'], True),
        })
        scraper = ArcusPlanningScraper(ArcusCouncilConfig('Example', 'https://example.test/pr'), http_client=http)
        result = scraper.discover_ids(listing_url='https://example.test/pr/s/register', start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), limit=3)
        self.assertEqual([app.uid for app in result.applications], ['1', '2', '3'])


if __name__ == "__main__":
    unittest.main()
