from __future__ import annotations

import base64
import json
import unittest
from datetime import date

from lead_generator.planning.adapters.authority_specific import (
    CentralBedfordshirePlanningScraper,
    EastSussexPlanningScraper,
    ElmbridgePlanningScraper,
    TandridgePlanningScraper,
    TauntonDeanePlanningScraper,
)
from lead_generator.planning.adapters.bespoke_portals import (
    ColchesterPlanningScraper,
    TelfordPlanningScraper,
    WestDunbartonshirePlanningScraper,
)
from lead_generator.planning.adapters.legacy_forms import LegacyFormsCouncilConfig
from lead_generator.planning.http import CouncilFetchError, FetchResponse

ROOT = 'https://planning.example.gov.uk'
START = date(2026, 7, 6)
END = date(2026, 7, 12)


def table(reference, received='10/07/2026'):
    return ('<table><tr><th>Application number</th><th>Address</th><th>Date received</th></tr>'
            f'<tr><td>{reference}</td><td>1 High Street</td><td>{received}</td></tr></table>')


class ScriptedHttp:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def _fetch(self, method, url, data):
        self.calls.append((method, url, data))
        if not self.steps:
            raise AssertionError(f'Unexpected request: {method} {url}')
        expected, text = self.steps.pop(0)
        if method != expected:
            raise AssertionError(f'Expected {expected}, got {method}')
        return FetchResponse(url, 200, text)

    def get(self, url, params=None, headers=None):
        return self._fetch('GET', url, params)

    def post_form(self, url, data, headers=None):
        return self._fetch('POST', url, data)


class SpecialHtmlPaginationTests(unittest.TestCase):
    def run_search(self, cls, http, limit=None):
        scraper = cls(LegacyFormsCouncilConfig('Test', ROOT), http_client=http)
        return scraper.search(ROOT + '/search', start_date=START, end_date=END, limit=limit)

    def test_central_bedfordshire_filters_before_limit_and_keeps_paging(self):
        http = ScriptedHttp([
            ('GET', '<form action="/results"><input name="regdate1"></form>'),
            ('GET', table('2026/0001', '01/07/2026') + '<a href="?page=2">Next</a>'),
            ('GET', table('2026/0002')),
        ])
        apps = self.run_search(CentralBedfordshirePlanningScraper, http, limit=1)
        self.assertEqual([a.reference for a in apps], ['2026/0002'])

    def test_east_sussex_follows_next_link(self):
        http = ScriptedHttp([
            ('GET', table('2026/0001') + '<a rel="next" href="?page=2">Next</a>'),
            ('GET', table('2026/0002')),
        ])
        apps = self.run_search(EastSussexPlanningScraper, http)
        self.assertEqual([a.reference for a in apps], ['2026/0001', '2026/0002'])

    def test_elmbridge_follows_next_link_after_search(self):
        http = ScriptedHttp([
            ('GET', '<form><input name="daterec_from:PARAM"><select name="pagerecs"><option value="50">50</option></select></form>'),
            ('GET', table('2026/0001') + '<a href="?page=2">Next</a>'),
            ('GET', table('2026/0002')),
        ])
        apps = self.run_search(ElmbridgePlanningScraper, http)
        self.assertEqual([a.reference for a in apps], ['2026/0001', '2026/0002'])

    def test_tandridge_pages_with_result_viewstate(self):
        http = ScriptedHttp([
            ('GET', '<form><input name="__VIEWSTATE" value="search"></form>'),
            ('POST', '<form><input name="__VIEWSTATE" value="dates"></form>'),
            ('POST', '<form><input name="__VIEWSTATE" value="results">' + table('2026/0001') +
             '<a href="javascript:__doPostBack(\'results\',\'Page$Next\')">Next</a></form>'),
            ('POST', '<form><input name="__VIEWSTATE" value="last">' + table('2026/0002') + '</form>'),
        ])
        apps = self.run_search(TandridgePlanningScraper, http)
        self.assertEqual([a.reference for a in apps], ['2026/0001', '2026/0002'])
        self.assertEqual(http.calls[-1][2]['__VIEWSTATE'], 'results')
        self.assertEqual(http.calls[-1][2]['__EVENTARGUMENT'], 'Page$Next')

    def test_taunton_follows_pager_when_view_all_is_absent(self):
        def result(ref):
            return f'<table><tr><td><a href="PlAppDets.asp?casefullref={ref}">{ref}</a></td><td>Registered : 10/07/2026</td></tr></table>'
        http = ScriptedHttp([
            ('GET', '<form action="PLAppList.asp"><input name="regdate1"></form>'),
            ('POST', result('2026/0001') + '<a href="?page=2">Next</a>'),
            ('GET', result('2026/0002')),
        ])
        apps = self.run_search(TauntonDeanePlanningScraper, http)
        self.assertEqual([a.reference for a in apps], ['2026/0001', '2026/0002'])

    def test_telford_follows_pager_within_a_single_day(self):
        def result(ref):
            return ('<table><tr><td><a href="pa-applicationsummary.aspx?applicationnumber=' + ref + '">' + ref +
                    '</a></td><td>10/07/2026</td><td>1 High Street</td><td>Extension</td></tr></table>')
        http = ScriptedHttp([
            ('GET', '<form><input name="ctl00$ContentPlaceHolder1$DCdatefrom"></form>'),
            ('POST', result('TWC/2026/0001') + '<a href="?page=2">Next</a>'),
            ('GET', result('TWC/2026/0002')),
        ])
        scraper = TelfordPlanningScraper(LegacyFormsCouncilConfig('Test', ROOT), http_client=http)
        apps = scraper.search(ROOT + '/search', start_date=START, end_date=START, limit=None)
        self.assertEqual([a.reference for a in apps], ['TWC/2026/0001', 'TWC/2026/0002'])

    def test_west_dunbartonshire_follows_pager_and_deduplicates(self):
        def result(ref):
            return f'<table><tr><td>1 High Street</td><td><form action="dcdisplayfullx.asp"><input name="vUPRN" value="{ref}"></form></td></tr></table>'
        http = ScriptedHttp([
            ('GET', '<form action="dcdisplayinitial.asp"><input name="vDateRcvFr"></form>'),
            ('GET', result('DC26/001/FUL') + '<a href="?page=2">Next</a>'),
            ('GET', result('DC26/001/FUL') + result('DC26/002/FUL')),
        ])
        apps = self.run_search(WestDunbartonshirePlanningScraper, http)
        self.assertEqual([a.reference for a in apps], ['DC26/001/FUL', 'DC26/002/FUL'])


class GridHttp:
    def __init__(self, pages=251, repeat=False, empty=False):
        self.pages = pages
        self.repeat = repeat
        self.empty = empty
        self.payloads = []

    def get(self, url, params=None, headers=None):
        if url.endswith('/_layout/tokenhtml'):
            text = '<input name="__RequestVerificationToken" value="token">'
        else:
            layout = base64.b64encode(json.dumps([{'Base64SecureConfiguration': 'secure'}]).encode()).decode()
            text = f'<div data-get-url="/grid" data-view-layouts="{layout}"></div>'
        return FetchResponse(url, 200, text)

    def post_json(self, url, payload, headers=None):
        self.payloads.append(payload)
        if len(self.payloads) > 252:
            raise AssertionError('Pagination did not terminate')
        page = payload['page']
        number = 1 if self.repeat else page
        record = {'Id': str(number), 'Attributes': [{'Name': 'new_name', 'Value': str(number)}]}
        return FetchResponse(url, 200, json.dumps({
            'Records': [] if self.empty else [record],
            'MoreRecords': self.repeat or self.empty or page < self.pages,
            'NextPagePagingCookie': 'same' if self.repeat else f'cookie-{page}',
        }))


class ColchesterPaginationTests(unittest.TestCase):
    def search(self, http, limit=None):
        scraper = ColchesterPlanningScraper(LegacyFormsCouncilConfig('Colchester', ROOT), http_client=http)
        return scraper.search(ROOT + '/search', start_date=None, end_date=None, limit=limit)

    def test_retrieves_records_after_page_250(self):
        http = GridHttp()
        apps = self.search(http)
        self.assertEqual(len(apps), 251)
        self.assertEqual(apps[-1].reference, '251')
        self.assertEqual(http.payloads[1]['pagingCookie'], 'cookie-1')

    def test_repeated_page_with_more_records_is_reported(self):
        with self.assertRaises(CouncilFetchError):
            self.search(GridHttp(repeat=True))

    def test_empty_page_with_more_records_is_reported(self):
        with self.assertRaises(CouncilFetchError):
            self.search(GridHttp(empty=True))

    def test_terminal_duplicate_page_is_deduplicated(self):
        class TerminalDuplicateHttp(GridHttp):
            def post_json(self, url, payload, headers=None):
                response = super().post_json(url, payload, headers=headers)
                result = json.loads(response.text)
                result['MoreRecords'] = payload['page'] == 1
                return FetchResponse(url, 200, json.dumps(result))

        apps = self.search(TerminalDuplicateHttp(repeat=True))
        self.assertEqual([application.reference for application in apps], ['1'])

    def test_limit_stops_before_requesting_more_pages(self):
        http = GridHttp()
        self.assertEqual(len(self.search(http, limit=1)), 1)
        self.assertEqual(len(http.payloads), 1)


if __name__ == '__main__':
    unittest.main()
