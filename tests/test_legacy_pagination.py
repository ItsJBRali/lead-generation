"""Regression cases use synthetic portal responses, not live council snapshots."""
from copy import deepcopy
from datetime import date
import json
import unittest

from lead_generator.planning.http import CouncilFetchError, FetchResponse
from lead_generator.planning.adapters.legacy_forms import (
    AppSearchServPlanningScraper, AstunPlanningScraper, CcedPlanningScraper,
    EnterpriseStorePlanningScraper, FastwebPlanningScraper, HtmlListPlanningScraper,
    NorthLincsPlanningScraper, QueryFormPlanningScraper, WebFormsPlanningScraper,
    LegacyFormsCouncilConfig, SocrataPlanningScraper,
    StatMapPlanningScraper, TascomiPlanningScraper, parse_header_tables,
)

BASE = 'https://planning.example.gov.uk'
CONFIG = LegacyFormsCouncilConfig('Example', BASE)


def table(*refs):
    return '<table><tr><th>Reference</th><th>Location</th></tr>' + ''.join(
        f'<tr><td>{ref}</td><td>High Street</td></tr>' for ref in refs
    ) + '</table>'


class ApiClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, deepcopy(params)))
        offset = int(params.get('$offset', 0))
        return FetchResponse(url, 200, json.dumps(self.pages.get(offset, [])))

    def post_json(self, url, data):
        self.calls.append((url, deepcopy(data)))
        page = data['pagination']['page']
        return FetchResponse(url, 200, json.dumps({'records': self.pages.get(page, [])}))


class LegacyApiPaginationTest(unittest.TestCase):
    def test_statmap_continues_after_server_capped_page_and_deduplicates(self):
        client = ApiClient({0: [{'id': '1', 'name': '26/00001/FUL'}],
                            1: [{'id': '1', 'name': '26/00001/FUL'}, {'id': '2', 'name': '26/00002/FUL'}]})
        apps = StatMapPlanningScraper(CONFIG, http_client=client).discover_ids(listing_url=BASE+'/horizoNext/').applications
        self.assertEqual([app.uid for app in apps], ['1', '2'])
        self.assertEqual([call[1]['pagination']['page'] for call in client.calls], [0, 1, 2])

    def test_socrata_offsets_count_raw_rows_and_limit_counts_retained_rows(self):
        client = ApiClient({0: [{'pk': '0', 'registered_date': '2025-01-01'}],
                            1: [{'pk': '1', 'registered_date': '2026-01-02'}],
                            2: [{'pk': '2', 'registered_date': '2026-01-03'}]})
        apps = SocrataPlanningScraper(CONFIG, http_client=client).discover_ids(
            listing_url=BASE+'/abcd-1234/about_data', start_date=date(2026, 1, 1), limit=2).applications
        self.assertEqual([app.uid for app in apps], ['1', '2'])
        self.assertEqual([call[1]['$offset'] for call in client.calls], ['0', '1', '2'])
        self.assertTrue(all(call[1]['$where'] == client.calls[0][1]['$where'] for call in client.calls))

    def test_socrata_unlimited_fetches_beyond_first_hundred(self):
        client = ApiClient({0: [{'pk': str(i)} for i in range(100)], 100: [{'pk': '100'}]})
        apps = SocrataPlanningScraper(CONFIG, http_client=client).discover_ids(listing_url=BASE+'/abcd-1234/').applications
        self.assertEqual(len(apps), 101)

    def test_api_duplicate_only_overlap_does_not_hide_later_rows(self):
        for scraper_class, pages in (
            (StatMapPlanningScraper, {0: [{'id': '1'}, {'id': '2'}], 1: [{'id': '2'}], 2: [{'id': '3'}]}),
            (SocrataPlanningScraper, {0: [{'pk': '1'}, {'pk': '2'}], 2: [{'pk': '2'}], 3: [{'pk': '3'}]}),
        ):
            with self.subTest(family=scraper_class.family):
                apps = scraper_class(CONFIG, http_client=ApiClient(pages)).discover_ids(listing_url=BASE).applications
                self.assertEqual([app.uid for app in apps], ['1', '2', '3'])

    def test_api_exact_repeated_page_is_an_error(self):
        for scraper_class, pages in (
            (StatMapPlanningScraper, {0: [{'id': '1'}], 1: [{'id': '1'}]}),
            (SocrataPlanningScraper, {0: [{'pk': '1'}], 1: [{'pk': '1'}]}),
        ):
            with self.subTest(family=scraper_class.family), self.assertRaises(CouncilFetchError):
                scraper_class(CONFIG, http_client=ApiClient(pages)).discover_ids(listing_url=BASE)

    def test_cced_postback_preserves_event_argument_and_latest_state(self):
        class Client:
            def post_form(self, url, data):
                self.data = data
                return FetchResponse(url, 200, '<html/>')
        client = Client()
        scraper = CcedPlanningScraper(CONFIG, http_client=client)
        scraper.post_results_page('''<form action="/results"><input name="__VIEWSTATE" value="page2"/>
            <a href="javascript:__doPostBack('grid','Page$3')">3</a></form>''', BASE, 'grid')
        self.assertEqual(client.data['__EVENTARGUMENT'], 'Page$3')
        self.assertEqual(client.data['__VIEWSTATE'], 'page2')

    def test_cced_parses_fragment_without_body_element(self):
        apps = CcedPlanningScraper(CONFIG).parse_results(
            'P/HOU/2026/03140 Location: High Street Proposal: Gates Decision: Pending Decision Date: View this application', BASE)
        self.assertEqual([app.reference for app in apps], ['P/HOU/2026/03140'])


class LegacyHtmlIntegrationTest(unittest.TestCase):
    def test_first_page_only_families_follow_advertised_next_links(self):
        for scraper_class in (EnterpriseStorePlanningScraper, AppSearchServPlanningScraper,
                AstunPlanningScraper, HtmlListPlanningScraper, QueryFormPlanningScraper,
                WebFormsPlanningScraper, NorthLincsPlanningScraper):
            with self.subTest(family=scraper_class.family):
                def page(number):
                    ref = f'26/{number:05d}/FUL'
                    content = f'<table><tr><th>Reference</th><th>Location</th></tr><tr><td><a href="/OnlinePlanningOverview?applicationNumber={ref}">{ref}</a></td><td>High Street</td></tr></table>'
                    return content + ('<a rel="next" href="/results?page=2">Next</a>' if number == 1 else '')
                class Client:
                    def get(self, url, params=None):
                        if 'page=2' in url: return FetchResponse(url, 200, page(2))
                        if scraper_class in (HtmlListPlanningScraper, NorthLincsPlanningScraper):
                            return FetchResponse(url, 200, page(1))
                        return FetchResponse(url, 200, '<form id="frmOnlinePlanningSearch" name="AppSearchForm" method="post" action="/results"><input name="SearchFor"><input name="received_date_from"><input name="template"><input name="requestType"></form>')
                    def post_form(self, url, data, headers=None): return FetchResponse(url, 200, page(1))
                apps = scraper_class(CONFIG, http_client=Client()).discover_ids(listing_url=BASE+'/search').applications
                self.assertEqual([app.reference for app in apps], ['26/00001/FUL', '26/00002/FUL'])

    def test_html_filters_dates_before_counting_limit(self):
        def page(number, day):
            return f'<table><tr><th>Reference</th><th>Received Date</th></tr><tr><td>26/{number:05d}/FUL</td><td>{day}</td></tr></table>'
        class Client:
            def get(self, url, params=None):
                if 'page=2' in url: return FetchResponse(url, 200, page(2, '2026-01-02'))
                return FetchResponse(url, 200, '<form method="post"/>')
            def post_form(self, url, data):
                return FetchResponse(url, 200, page(1, '2025-01-01')+'<a rel="next" href="?page=2">Next</a>')
        apps = QueryFormPlanningScraper(CONFIG, http_client=Client()).discover_ids(
            listing_url=BASE, start_date=date(2026, 1, 1), limit=1).applications
        self.assertEqual([app.reference for app in apps], ['26/00002/FUL'])

    def test_cced_numbered_pages_without_pager_class_use_latest_argument(self):
        def page(number):
            links = ''.join(f"<a href=\"javascript:__doPostBack('grid','Page${n}')\">{n}</a>" for n in range(1, 4) if n != number)
            return f'<form action="/results"><input name="__VIEWSTATE" value="{number}">Page {number} of 3 {links}</form>P/HOU/2026/0000{number} Location: High Street Proposal: Gates Decision: Pending Decision Date: View this application'
        class Client:
            def __init__(self): self.calls = []
            def get(self, url): return FetchResponse(url, 200, '<form action="/results"/>')
            def post_form(self, url, data):
                self.calls.append(data)
                number = int(data['__EVENTARGUMENT'].split('$')[1]) if '__EVENTARGUMENT' in data else 1
                return FetchResponse(url, 200, page(number))
        client = Client()
        apps = CcedPlanningScraper(CONFIG, http_client=client).discover_ids(listing_url=BASE).applications
        self.assertEqual(len(apps), 3)
        self.assertEqual([data['__EVENTARGUMENT'] for data in client.calls[1:]], ['Page$2', 'Page$3'])
        self.assertEqual([data['__VIEWSTATE'] for data in client.calls[1:]], ['1', '2'])

    def test_query_without_form_has_no_implicit_hundred_result_limit(self):
        content = ''.join(f'<a href="/planning/{i}">26/{i:05d}/FUL</a>' for i in range(101))
        class Client:
            def get(self, url): return FetchResponse(url, 200, content)
        apps = QueryFormPlanningScraper(CONFIG, http_client=Client()).discover_ids(listing_url=BASE).applications
        self.assertEqual(len(apps), 101)

    def test_fastweb_cycle_is_reported(self):
        class Client:
            def get(self, url): return FetchResponse(url, 200, '<form name="SearchForm" action="/results"/>')
            def post_form(self, url, data): return FetchResponse(url, 200, '<a href="/results">Next</a>')
        with self.assertRaises(CouncilFetchError):
            FastwebPlanningScraper(CONFIG, http_client=Client()).discover_ids(listing_url=BASE)

    def test_tascomi_weekly_limit_stops_before_next_page(self):
        class Client:
            def get(self, url):
                if 'page=2' in url: raise AssertionError('Requested page beyond limit')
                return FetchResponse(url, 200, '<form action="/results"><input name="week"/></form>')
            def post_form(self, url, data):
                return FetchResponse(url, 200, table('26/00001/FUL')+'<a rel="next" href="?page=2">Next</a>')
        apps = TascomiPlanningScraper(CONFIG, http_client=Client()).discover_ids(
            listing_url=BASE, start_date=date(2026, 1, 5), end_date=date(2026, 1, 11), limit=1).applications
        self.assertEqual(len(apps), 1)

    def test_tascomi_has_no_silent_250_page_ceiling(self):
        class Client:
            def get(self, url): return FetchResponse(url, 200, '<form action="/results"><input name="received_date_from"/></form>')
            def post_form(self, url, data, headers=None):
                number = int(data.get('page', 1))
                return FetchResponse(url, 200, table(f'26/{number:05d}/FUL') if number <= 251 else '')
        apps = TascomiPlanningScraper(CONFIG, http_client=Client()).discover_ids(listing_url=BASE).applications
        self.assertEqual(len(apps), 251)


class SharedHtmlPaginationTest(unittest.TestCase):
    def collect(self, client, body, limit=None):
        from lead_generator.planning.adapters.pagination import collect_listing_pages
        return collect_listing_pages(client, FetchResponse(BASE+'/results', 200, body),
            lambda text, url: parse_header_tables(text, url, 'Example', 'test'), limit=limit)

    def test_next_links_recurse_and_limit_prevents_extra_requests(self):
        class Client:
            def __init__(self): self.calls = []
            def get(self, url):
                self.calls.append(url)
                return FetchResponse(url, 200, table('26/00002/FUL')+'<a rel="next" href="?page=3">Next</a>')
        client = Client()
        apps = self.collect(client, table('26/00001/FUL')+'<a rel="next" href="?page=2">Next</a>', limit=2)
        self.assertEqual([app.reference for app in apps], ['26/00001/FUL', '26/00002/FUL'])
        self.assertEqual(client.calls, [BASE+'/results?page=2'])

    def test_postback_uses_rotating_state_and_arguments(self):
        def page(number):
            next_link = f'''<a href="javascript:__doPostBack('grid','Page${number+1}')">Next</a>''' if number < 3 else ''
            return table(f'26/{number:05d}/FUL')+f'<form action="/results"><input name="__VIEWSTATE" value="{number}">{next_link}</form>'
        class Client:
            def __init__(self): self.calls = []
            def post_form(self, url, data):
                self.calls.append(data)
                return FetchResponse(url, 200, page(int(data['__EVENTARGUMENT'].split('$')[1])))
        client = Client()
        apps = self.collect(client, page(1))
        self.assertEqual(len(apps), 3)
        self.assertEqual([data['__VIEWSTATE'] for data in client.calls], ['1', '2'])
        self.assertEqual([data['__EVENTARGUMENT'] for data in client.calls], ['Page$2', 'Page$3'])

    def test_explicit_next_cycle_raises_instead_of_hanging(self):
        body = table('26/00001/FUL')+'<a rel="next" href="?page=2">Next</a>'
        class Client:
            def get(self, url): return FetchResponse(url, 200, body)
        with self.assertRaises(CouncilFetchError): self.collect(Client(), body)

    def test_numeric_postbacks_rebuild_form_state_after_each_page(self):
        def page(number):
            links = ''.join(f"<span>{n}</span>" if n == number else
                f"<a href=\"javascript:__doPostBack('grid','Page${n}')\">{n}</a>" for n in range(1, 4))
            return table(f'26/{number:05d}/FUL')+f'<form><input name="__VIEWSTATE" value="{number}"><div class="pager">{links}</div></form>'
        class Client:
            def __init__(self): self.calls = []
            def post_form(self, url, data):
                self.calls.append(data)
                return FetchResponse(url, 200, page(int(data['__EVENTARGUMENT'].split('$')[1])))
        client = Client()
        apps = self.collect(client, page(1))
        self.assertEqual(len(apps), 3)
        self.assertEqual([data['__VIEWSTATE'] for data in client.calls], ['1', '2'])

    def test_numeric_links_with_wrapped_labels_are_followed(self):
        class Client:
            def get(self, url): return FetchResponse(url, 200, table('26/00002/FUL'))
        body = table('26/00001/FUL')+'<div class="pagination"><span>1</span><a href="?page=2"><span>2</span></a></div>'
        self.assertEqual(len(self.collect(Client(), body)), 2)

    def test_next_steps_navigation_does_not_replace_result_pager(self):
        class Client:
            def get(self, url):
                self_url = BASE+'/results?page=2'
                if url != self_url: raise AssertionError(url)
                return FetchResponse(url, 200, table('26/00002/FUL'))
        body = table('26/00001/FUL')+'<a href="/guidance">Next steps</a><div class="pagination"><span>1</span><a href="?page=2">2</a></div>'
        self.assertEqual(len(self.collect(Client(), body)), 2)

    def test_offsite_next_does_not_hide_same_site_numbered_pager(self):
        class Client:
            def get(self, url): return FetchResponse(url, 200, table('26/00002/FUL'))
        body = table('26/00001/FUL')+'<a rel="next" href="https://other.example/">Next</a><div class="pager"><span>1</span><a href="?page=2">2</a></div>'
        self.assertEqual(len(self.collect(Client(), body)), 2)

    def test_custom_get_pager_filters_first_page_alias(self):
        from lead_generator.planning.adapters.pagination import collect_listing_pages
        body = table('26/00001/FUL')+'<div class="pager"><a href="?page=1">1</a><a href="?page=2">2</a></div>'
        class Client:
            def get(self, url):
                if 'page=1' in url: raise AssertionError('First-page alias requested')
                return FetchResponse(url, 200, table('26/00002/FUL'))
        apps = collect_listing_pages(Client(), FetchResponse(BASE+'/results', 200, body),
            lambda text, url: parse_header_tables(text, url, 'Example', 'test'),
            pagination_urls=lambda text, url: [BASE+'/results?page=2'] if 'page=2' in text else [])
        self.assertEqual(len(apps), 2)

    def test_forward_postback_ellipsis_continues_numeric_window(self):
        body = table('26/00010/FUL')+"""<form><input name="__VIEWSTATE" value="10">Page 10 of 11
            <a href="javascript:__doPostBack('back','')">...</a><a href="javascript:__doPostBack('grid','Page$9')">9</a><span>10</span>
            <a href="javascript:__doPostBack('forward','')">...</a></form>"""
        class Client:
            def post_form(self, url, data):
                if data['__EVENTTARGET'] != 'forward': raise AssertionError(data)
                return FetchResponse(url, 200, table('26/00011/FUL')+'Page 11 of 11')
        self.assertEqual(len(self.collect(Client(), body)), 2)

    def test_external_next_link_is_not_a_listing_page(self):
        class Client:
            def get(self, url): raise AssertionError('Off-site link requested')
        apps = self.collect(Client(), table('26/00001/FUL')+'<a href="https://other.example/">Next</a>')
        self.assertEqual(len(apps), 1)

    def test_numbered_pager_skips_backlinks_and_disabled_next(self):
        class Client:
            def get(self, url):
                return FetchResponse(url, 200, table('26/00002/FUL')+'''<div class="pagination"><a href="/results">1</a><span>2</span>
                    <span class="disabled"><a href="?page=3">Next</a></span></div>''')
        apps = self.collect(Client(), table('26/00001/FUL')+'<div class="pagination"><span>1</span><a href="?page=2">2</a></div>')
        self.assertEqual(len(apps), 2)


if __name__ == '__main__': unittest.main()
