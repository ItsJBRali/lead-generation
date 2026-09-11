"""Offline continuation regressions; synthetic pages use existing portal URL shapes."""
from datetime import date
import unittest

from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.atrium import AtriumCouncilConfig, AtriumPlanningScraper
from lead_generator.planning.adapters.civica import CivicaCouncilConfig, CivicaPlanningScraper
from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper
from lead_generator.planning.adapters.northgate import NorthgateCouncilConfig, NorthgatePlanningScraper
from lead_generator.planning.adapters.ocella import OcellaCouncilConfig, OcellaPlanningScraper
from lead_generator.planning.http import CouncilFetchError, FetchResponse


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.visited = []

    def get(self, url, params=None, headers=None):
        self.visited.append(url)
        return FetchResponse(url=url, status_code=200, text=self.pages[url])


class HtmlPaginationTest(unittest.TestCase):
    def test_inherited_and_ocella_html_searches_follow_next_on_every_page(self):
        for scraper_type, config_type, detail in [
            (AgilePlanningScraper, AgileCouncilConfig, 'appdetail?appID='),
            (CivicaPlanningScraper, CivicaCouncilConfig, 'details?refval='),
            (OcellaPlanningScraper, OcellaCouncilConfig, 'planningDetails?reference='),
        ]:
            with self.subTest(adapter=scraper_type.__name__):
                base = 'https://planning.example.gov.uk/'
                http = Pages({
                    base+'search': f'<a href="{detail}26/001">26/001</a><a rel="next" href="search?page=2">Next</a>',
                    base+'search?page=2': f'<a href="{detail}26/001">26/001</a><a href="{detail}26/002">26/002</a><a rel="next" href="search?page=3">Next</a>',
                    base+'search?page=3': f'<a href="{detail}26/003">26/003</a><span class="disabled">Next</span>',
                })
                scraper = scraper_type(config_type('Council', base), http_client=http)
                results = scraper.discover_ids(listing_url=base+'search')
                self.assertEqual([a.uid for a in results.applications], ['26/001', '26/002', '26/003'])

    def test_atrium_discovers_links_exposed_only_on_later_pages(self):
        base = 'https://planning.example.gov.uk'
        http = Pages({
            base+'/Search/ResultsPage/1': '<a href="/Planning/Display/26/001">26/001</a><a href="/Search/ResultsPage/2">2</a>',
            base+'/Search/ResultsPage/2': '<a href="/Planning/Display/26/002">26/002</a><a href="/Search/ResultsPage/3">3</a>',
            base+'/Search/ResultsPage/3': '<a href="/Planning/Display/26/003">26/003</a><a href="/Search/ResultsPage/2">2</a>',
        })
        scraper = AtriumPlanningScraper(AtriumCouncilConfig('Council', base), http_client=http)
        results = scraper.discover_ids(listing_url=base+'/Search/ResultsPage/1')
        self.assertEqual([a.uid for a in results.applications], ['26/001', '26/002', '26/003'])

    def test_idox_page_guard_reports_incomplete_search(self):
        base = 'https://planning.example.gov.uk/online-applications/'
        http = Pages({base+'search.do': '<a href="applicationDetails.do?keyVal=A">26/001</a><a href="pagedSearchResults.do?action=page&amp;searchCriteria.page=2">2</a>'})
        scraper = IdoxPublicAccessScraper(IdoxCouncilConfig('Council', base), http_client=http)
        scraper.MAX_PAGED_RESULT_PAGES = 1
        with self.assertRaisesRegex(CouncilFetchError, 'pagination|Pagination'):
            scraper.discover_ids(listing_url=base+'search.do')

    def test_atrium_skips_numbered_alias_of_initial_page(self):
        base = 'https://planning.example.gov.uk'
        first = '<a href="/Planning/Display/26/001">26/001</a><a href="/Search/ResultsPage/1">1</a><a href="/Search/ResultsPage/2">2</a>'
        http = Pages({base+'/Search/Results': first, base+'/Search/ResultsPage/1': first,
                      base+'/Search/ResultsPage/2': '<a href="/Planning/Display/26/002">26/002</a>'})
        scraper = AtriumPlanningScraper(AtriumCouncilConfig('Council', base), http_client=http)
        result = scraper.discover_ids(listing_url=base+'/Search/Results')
        self.assertEqual([a.uid for a in result.applications], ['26/001', '26/002'])

    def test_northgate_reads_total_from_fragment_and_stops_before_unrelated_page(self):
        base = 'https://planning.example.gov.uk/'
        http = Pages({
            base+'StdResults.aspx?p=0': 'Records 1 to 1 of 1 <a href="StdDetails.aspx?PARAM0=26/001">26/001</a><a href="StdResults.aspx?p=10">2</a>',
            base+'StdResults.aspx?p=10': '<a href="StdDetails.aspx?PARAM0=26/002">26/002</a>',
        })
        scraper = NorthgatePlanningScraper(NorthgateCouncilConfig('Council', base), http_client=http)
        result = scraper.discover_ids(listing_url=base+'StdResults.aspx?p=0')
        self.assertEqual([a.uid for a in result.applications], ['26/001'])
        self.assertEqual(http.visited, [base+'StdResults.aspx?p=0'])

    def test_ocella_single_day_cap_is_reported_as_incomplete(self):
        class Capped(Pages):
            def post_form(self, url, data):
                return FetchResponse(url=url, status_code=200, text='<p>First 100 results shown, there are 150 in total</p>')
        scraper = OcellaPlanningScraper(OcellaCouncilConfig('Council', 'https://planning.example.gov.uk'), http_client=Capped({}))
        with self.assertRaisesRegex(CouncilFetchError, 'cap|truncat|incomplete'):
            scraper._fetch_received_date_pages('https://planning.example.gov.uk/search', {}, start_date=date(2026, 7, 1), end_date=date(2026, 7, 1))

    def test_northgate_page_guard_reports_incomplete_search(self):
        base = 'https://planning.example.gov.uk/'
        http = Pages({
            base+'StdResults.aspx?p=0': '<a href="StdDetails.aspx?PARAM0=26/001">26/001</a><a href="StdResults.aspx?p=10">2</a>',
            base+'StdResults.aspx?p=10': '<a href="StdDetails.aspx?PARAM0=26/002">26/002</a>',
        })
        scraper = NorthgatePlanningScraper(NorthgateCouncilConfig('Council', base), http_client=http)
        scraper.MAX_PAGED_RESULT_PAGES = 1
        with self.assertRaisesRegex(CouncilFetchError, 'pagination|Pagination'):
            scraper.discover_ids(listing_url=base+'StdResults.aspx?p=0')

    def test_ocella_explicit_limit_can_be_satisfied_by_a_capped_day(self):
        class Capped(Pages):
            def get(self, url):
                return FetchResponse(url=url, status_code=200, text='<form><input name="receivedFrom"><input name="receivedTo"></form>')
            def post_form(self, url, data):
                return FetchResponse(url=url, status_code=200, text='<a href="planningDetails?reference=26/001">26/001</a><p>First 1 results shown, there are 2 in total</p>')
        scraper = OcellaPlanningScraper(OcellaCouncilConfig('Council', 'https://planning.example.gov.uk'), http_client=Capped({}))
        result = scraper.discover_ids(listing_url='https://planning.example.gov.uk/search', start_date=date(2026, 7, 1), end_date=date(2026, 7, 1), limit=1)
        self.assertEqual([a.uid for a in result.applications], ['26/001'])

    def test_northgate_limit_stops_before_fetching_unneeded_queued_pages(self):
        base = 'https://planning.example.gov.uk/'
        http = Pages({
            base+'StdResults.aspx?p=0': '<a href="StdDetails.aspx?PARAM0=26/001">26/001</a><a href="StdResults.aspx?p=10">2</a><a href="StdResults.aspx?p=20">3</a>',
            base+'StdResults.aspx?p=10': '<a href="StdDetails.aspx?PARAM0=26/002">26/002</a>',
            base+'StdResults.aspx?p=20': '<a href="StdDetails.aspx?PARAM0=26/003">26/003</a>',
        })
        scraper = NorthgatePlanningScraper(NorthgateCouncilConfig('Council', base), http_client=http)
        result = scraper.discover_ids(listing_url=base+'StdResults.aspx?p=0', limit=2)
        self.assertEqual([a.uid for a in result.applications], ['26/001', '26/002'])
        self.assertNotIn(base+'StdResults.aspx?p=20', http.visited)

    def test_ocella_limit_counts_results_from_both_date_windows(self):
        class Capped(Pages):
            def get(self, url):
                return FetchResponse(url=url, status_code=200, text='<form><input name="receivedFrom"><input name="receivedTo"></form>')
            def post_form(self, url, data):
                first_day = data['receivedFrom'] == '01-07-26'
                ids = ['26/001', '26/002'] if first_day else ['26/003', '26/004']
                text = ''.join(f'<a href="planningDetails?reference={uid}">{uid}</a>' for uid in ids)
                if data['receivedTo'] == '02-07-26':
                    text += '<p>First 2 results shown, there are 4 in total</p>'
                return FetchResponse(url=url, status_code=200, text=text)
        scraper = OcellaPlanningScraper(OcellaCouncilConfig('Council', 'https://planning.example.gov.uk'), http_client=Capped({}))
        result = scraper.discover_ids(listing_url='https://planning.example.gov.uk/search', start_date=date(2026, 7, 1), end_date=date(2026, 7, 2), limit=3)
        self.assertEqual([a.uid for a in result.applications], ['26/001', '26/002', '26/003'])


if __name__ == '__main__':
    unittest.main()
