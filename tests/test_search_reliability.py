import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
import threading

import lead_generator.planning.leads as leads
from lead_generator.planning.models import PlanningApplication, DiscoveryResult
from test_leads import write_search_fixture


class SearchReliabilityTest(unittest.TestCase):
    def test_date_priority_and_invalid_received_fallback(self):
        for received, validated, expected in [
            (None, '2026-07-15', True), ('bad date', '2026-07-15', True),
            ('2026-07-15', '2026-08-15', True), ('2026-08-15', '2026-07-15', False),
            ('2026-07-15garbage', '2026-07-15', True), ('bad', 'bad', False),
        ]:
            app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A', date_received=received, date_validated=validated)
            with self.subTest(received=received, validated=validated):
                self.assertEqual(leads.application_is_in_date_range(app, date(2026, 7, 1), date(2026, 7, 31)), expected)
                self.assertEqual(leads.application_matches(app, date(2026, 7, 1), date(2026, 7, 31), []), expected)

    def test_same_day_searches_keep_previous_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, download_application_files=False)
            app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A', date_received='2026-07-15')
            with patch.object(leads, 'discover_portal_applications', side_effect=[[app], []]), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]):
                first, second = leads.run_lead_search(config), leads.run_lead_search(config)
            self.assertNotEqual(first.output_dir, second.output_dir)
            with first.csv_path.open(newline='', encoding='utf-8') as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_failed_details_are_retried_and_report_partial_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, download_application_files=False)
            stub = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A')
            discovery = DiscoveryResult('Council', 'https://example.test/search', [stub])
            from types import SimpleNamespace
            scraper = SimpleNamespace(fetch_application=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('detail unavailable')), close=lambda: None)
            with patch.object(leads, '_discover_portal_listing', return_value=(discovery, scraper)), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]):
                result = leads.run_lead_search(config)
            self.assertEqual(result.completion, 'Partial')
            self.assertEqual(result.no_application_councils, [])
            self.assertEqual(result.failed_councils, ['Council'])

    def test_resume_preserves_enrichment_and_skips_completed_sources(self):
        from lead_generator.planning.enrichment import ContactEnrichment
        for pipeline in (False, True):
            with self.subTest(pipeline=pipeline), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                area, catalogue = write_search_fixture(root, ['Council'])
                config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, pipelined=pipeline)
                apps = [PlanningApplication('Council', str(i), f'https://example.test/{i}', reference=str(i), date_validated='2026-07-15') for i in range(2)]
                cancel = threading.Event()
                def enrich(*args, **kwargs):
                    cancel.set()
                    return ContactEnrichment(architect_company_names=['Cached Architect'])
                with patch.object(leads, 'discover_portal_applications', return_value=apps), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]), patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult()), patch.object(leads, 'enrich_application_folder', side_effect=enrich):
                    first = leads.run_lead_search(config, should_cancel=cancel.is_set)
                self.assertEqual(first.completion, 'Cancelled')
                from lead_generator.planning.run_storage import read_run_settings
                saved = read_run_settings(first.output_dir)
                self.assertEqual(saved['geojson_path'], str(area.resolve()))
                self.assertEqual(saved['start'], '2026-07-01')
                with patch.object(leads, 'discover_portal_applications', side_effect=AssertionError('Completed primary should be reused')), patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=AssertionError('Completed reconciliation should be reused')), patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult()), patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment(architect_company_names=['New Architect'])) as enrich:
                    resumed = leads.run_lead_search(replace(config, resume_from=first.output_dir))
                self.assertEqual(resumed.completion, 'Completed')
                self.assertEqual(resumed.leads_found, 2)
                self.assertEqual(enrich.call_count, 1)
                with resumed.csv_path.open(newline='', encoding='utf-8') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual({row['Architect / Company Name'] for row in rows}, {'Cached Architect', 'New Architect'})
                self.assertTrue((resumed.output_dir / 'search_metrics.csv').is_file())
                original = resumed.csv_path.read_bytes()
                with self.assertRaises(ValueError):
                    leads.run_lead_search(replace(config, keywords=['different'], resume_from=first.output_dir))
                self.assertEqual(resumed.csv_path.read_bytes(), original)

    def test_pdf_text_cache_reuses_unchanged_bytes_and_invalidates_changes(self):
        from types import SimpleNamespace
        from lead_generator.planning import enrichment
        from lead_generator.planning.pdf_cache import pdf_cache_directory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'drawing.pdf'
            pdf.write_bytes(b'original PDF bytes')
            reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: 'Professional drawing text with architect contact information ' * 5)])
            with pdf_cache_directory(root / 'cache'), patch.object(enrichment, 'PdfReader', return_value=reader) as open_pdf:
                first = enrichment.extract_pdf_first_page_text(pdf)
                enrichment._close_pdf_cache(first)
                second = enrichment.extract_pdf_first_page_text(pdf)
                enrichment._close_pdf_cache(second)
                self.assertEqual(first.text, second.text)
                self.assertEqual(open_pdf.call_count, 1)
                pdf.write_bytes(b'changed PDF bytes')
                third = enrichment.extract_pdf_first_page_text(pdf)
                enrichment._close_pdf_cache(third)
                self.assertEqual(open_pdf.call_count, 2)

    def test_document_cache_requires_server_validation(self):
        from email.message import Message
        from urllib.error import HTTPError
        from types import SimpleNamespace
        from lead_generator.planning.models import PlanningDocument
        from lead_generator.planning.document_cache import document_cache_directory
        class Response:
            headers = Message()
            headers['Content-Type'] = 'application/pdf'
            headers['ETag'] = '"version-1"'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'%PDF-1.7 verified document bytes'
            def geturl(self): return 'https://example.test/document.pdf'
        doc = PlanningDocument('Drawing', 'https://example.test/document.pdf')
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                return Response()
            self.assertEqual(request.get_header('If-none-match'), '"version-1"')
            raise HTTPError(request.full_url, 304, 'Not Modified', Message(), None)
        with tempfile.TemporaryDirectory() as directory, document_cache_directory(Path(directory)), patch.object(leads, '_throttle_request'):
            opener = SimpleNamespace(open=open_request)
            first = leads.download_document_file(doc, opener=opener)
            second = leads.download_document_file(doc, opener=opener)
            self.assertEqual(first.payload, second.payload)
            self.assertEqual(len(requests), 2)

    def test_metrics_count_requests_and_retries(self):
        from email.message import Message
        from urllib.error import HTTPError
        from lead_generator.planning.metrics import RunMetrics
        from lead_generator.planning.http import CouncilHttpClient
        from test_review_regressions import JsonResponse
        metrics = RunMetrics()
        client = CouncilHttpClient(min_delay_seconds=0, retries=1)
        headers = Message()
        headers['Retry-After'] = '0'
        with metrics.stage('Council', 'primary'), patch.object(client, '_opener') as opener, patch('lead_generator.planning.http._interruptible_sleep'):
            opener.return_value.open.side_effect = [HTTPError('https://metrics.test', 429, 'rate limited', headers, None), JsonResponse(b'[]', 'https://metrics.test')]
            client.get('https://metrics.test')
        row = metrics.rows[('Council', 'primary')]
        self.assertEqual(row['requests'], 2)
        self.assertEqual(row['retries'], 1)
        self.assertGreater(row['seconds'], 0)

    def test_audit_ignores_unrelated_performance_totals(self):
        from lead_generator.planning.completeness_audit import response_evidence, classify_completeness
        performance = response_evidence({'perfSummary': {'total': 251}, 'actions': [{'returnValue': '{"records":[]}'}]})
        self.assertEqual(performance['totals'], [])
        complete = response_evidence({'results': [{}, {}], 'totalCount': 2})
        self.assertEqual(classify_completeness(2, [complete], family='agile'), 'Complete against advertised total')
        incomplete = response_evidence({'results': [{}], 'totalCount': 2})
        self.assertTrue(classify_completeness(1, [incomplete], family='agile').startswith('Incomplete'))
        consumed = [{'totals': [2], 'continuation_advertised': True}, {'totals': [2], 'continuation_advertised': False}]
        self.assertEqual(classify_completeness(2, consumed, family='agile'), 'Complete against advertised total')

    def test_wiltshire_live_empty_response_is_valid(self):
        import json
        from lead_generator.planning.adapters.wiltshire import WiltshirePlanningScraper, WiltshireCouncilConfig
        from test_wiltshire_scraper import FakeWiltshireHttpClient
        scraper = WiltshirePlanningScraper(WiltshireCouncilConfig('Wiltshire', 'https://example.test'), http_client=FakeWiltshireHttpClient(json.dumps({'actions': [{'state': 'SUCCESS', 'returnValue': '[]'}]})))
        result = scraper.discover_ids(listing_url='https://example.test/pr/s/', start_date=date(2026, 7, 1), end_date=date(2026, 7, 1))
        self.assertEqual(result.applications, [])

    def test_detail_retry_recovers_a_transient_failure(self):
        from unittest.mock import Mock
        from types import SimpleNamespace
        stub = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A')
        full = PlanningApplication('Council', 'A', stub.url, reference='A', date_validated='2026-07-15')
        fetch = Mock(side_effect=[RuntimeError('temporary failure'), full])
        scraper = SimpleNamespace(fetch_application=fetch, close=lambda: None)
        target = leads.CouncilTarget('Council', 'idox', 'idox', 'https://example.test', 'https://example.test/search', {})
        with patch.object(leads, '_discover_portal_listing', return_value=(DiscoveryResult('Council', stub.url, [stub]), scraper)):
            result = leads.discover_portal_applications(target, date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result, [full])
        self.assertNotIn('detail_fetch_error', full.raw)

    def test_missing_dates_without_network_error_still_report_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, download_application_files=False)
            app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A')
            with patch.object(leads, 'discover_portal_applications', return_value=[app]), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]):
                result = leads.run_lead_search(config)
            self.assertEqual(result.completion, 'Partial')
            self.assertEqual(result.no_application_councils, [])

    def test_resume_retries_failed_extraction(self):
        from lead_generator.planning.enrichment import ContactEnrichment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, pipelined=True)
            app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A', date_received='2026-07-15')
            with patch.object(leads, 'discover_portal_applications', return_value=[app]), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]), patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult()), patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment(unreadable_documents=['drawing.pdf'])):
                first = leads.run_lead_search(config)
            with patch.object(leads, 'discover_portal_applications', side_effect=AssertionError('Primary already saved')), patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=AssertionError('Fallback already saved')), patch.object(leads, 'discover_application_documents', side_effect=AssertionError('Documents already complete')), patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment(architect_company_names=['Recovered'])) as enrich:
                result = leads.run_lead_search(replace(config, resume_from=first.output_dir))
            self.assertEqual(result.completion, 'Completed')
            self.assertEqual(enrich.call_count, 1)

    def test_resume_promotes_recovered_primary_over_saved_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, download_application_files=False)
            secondary = PlanningApplication('Council', 'A', 'https://fallback.test/A', reference='A', date_received='2026-07-15', raw={'source': 'planit_reconciliation'})
            primary = PlanningApplication('Council', 'A', 'https://primary.test/A', reference='A', date_received='2026-07-15')
            with patch.object(leads, 'discover_portal_applications', side_effect=RuntimeError('primary unavailable')), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[secondary]):
                first = leads.run_lead_search(config)
            self.assertEqual(first.completion, 'Partial')
            with patch.object(leads, 'discover_portal_applications', return_value=[primary]), patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=AssertionError('Use cached fallback')):
                result = leads.run_lead_search(replace(config, resume_from=first.output_dir))
            with result.csv_path.open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(result.completion, 'Completed')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['application link'], primary.url)

    def test_resume_downloads_documents_not_attempted_before_cancel(self):
        from lead_generator.planning.models import PlanningDocument
        from lead_generator.planning.enrichment import ContactEnrichment
        for pipeline in (False, True):
            with self.subTest(pipeline=pipeline), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                area, catalogue = write_search_fixture(root, ['Council'])
                config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue, pipelined=pipeline)
                app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A', date_received='2026-07-15')
                documents = [PlanningDocument(f'Proposed drawing {i}', f'https://example.test/{i}.pdf') for i in range(3)]
                cancelled = threading.Event()
                def download(document, **kwargs):
                    cancelled.set()
                    return leads.DownloadedFile(b'%PDF-1.7 ' + document.url.encode(), document.url, 'application/pdf')
                with patch.object(leads, 'DOCUMENT_DOWNLOAD_DELAY_SECONDS', 0), patch.object(leads, 'discover_portal_applications', return_value=[app]), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]), patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult(documents=documents)), patch.object(leads, 'download_document_file', side_effect=download) as first_download, patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment()):
                    first = leads.run_lead_search(config, should_cancel=cancelled.is_set)
                self.assertEqual(first_download.call_count, 1)
                self.assertEqual(first.completion, 'Cancelled')
                def remaining(document, **kwargs):
                    return leads.DownloadedFile(b'%PDF-1.7 ' + document.url.encode(), document.url, 'application/pdf')
                with patch.object(leads, 'DOCUMENT_DOWNLOAD_DELAY_SECONDS', 0), patch.object(leads, 'discover_portal_applications', side_effect=AssertionError('Use checkpoint')), patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=AssertionError('Use checkpoint')), patch.object(leads, 'discover_application_documents', side_effect=AssertionError('Use pending documents')), patch.object(leads, 'download_document_file', side_effect=remaining) as resumed_download, patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment()):
                    resumed = leads.run_lead_search(replace(config, resume_from=first.output_dir))
                self.assertEqual(resumed.completion, 'Completed')
                self.assertEqual(resumed_download.call_count, 2)
                self.assertEqual({call.args[0].url for call in resumed_download.call_args_list}, {d.url for d in documents[1:]})

    def test_failed_ocr_is_retried_on_next_run(self):
        from types import SimpleNamespace
        from lead_generator.planning import enrichment
        from lead_generator.planning.pdf_cache import pdf_cache_directory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'drawing.pdf'
            pdf.write_bytes(b'unchanged bytes')
            reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: 'Drawing')])
            with pdf_cache_directory(root / 'cache'), patch.object(enrichment, 'PdfReader', return_value=reader), patch.object(enrichment, '_ocr_pdf_pages', side_effect=[{}, {0: 'Architect company and professional contact details ' * 6}]) as ocr:
                first = enrichment.extract_pdf_text(pdf)
                enrichment._close_pdf_cache(first)
                second = enrichment.extract_pdf_text(pdf)
                enrichment._close_pdf_cache(second)
                self.assertEqual(ocr.call_count, 2)
                self.assertIn('Architect company', second.text)

    def test_final_transient_download_failure_is_retried_on_resume(self):
        from email.message import Message
        from urllib.error import HTTPError
        from lead_generator.planning.models import PlanningDocument
        from lead_generator.planning.enrichment import ContactEnrichment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(area, root, date(2026, 7, 1), date(2026, 7, 31), [], catalogue_path=catalogue)
            app = PlanningApplication('Council', 'A', 'https://example.test/A', reference='A', date_received='2026-07-15')
            doc = PlanningDocument('Proposed drawing', 'https://example.test/plans.pdf')
            with patch.object(leads, 'discover_portal_applications', return_value=[app]), patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]), patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult(documents=[doc])), patch.object(leads, '_wait_for_document_retry_cooldown', return_value=True), patch.object(leads, 'download_document_file', side_effect=HTTPError(doc.url, 503, 'Unavailable', Message(), None)), patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment()):
                first = leads.run_lead_search(config)
            with patch.object(leads, 'DOCUMENT_DOWNLOAD_DELAY_SECONDS', 0), patch.object(leads, 'discover_portal_applications', side_effect=AssertionError('Use saved primary')), patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=AssertionError('Use saved reconciliation')), patch.object(leads, 'discover_application_documents', side_effect=AssertionError('Use pending document')), patch.object(leads, 'download_document_file', return_value=leads.DownloadedFile(b'%PDF-1.7 recovered', doc.url, 'application/pdf')) as download, patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment()) as enrich:
                result = leads.run_lead_search(replace(config, resume_from=first.output_dir))
            self.assertEqual(result.captured_documents, 1)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(enrich.call_count, 1)

    def test_changed_redirect_target_is_downloaded_without_old_validator(self):
        from email.message import Message
        from urllib.error import HTTPError
        from types import SimpleNamespace
        from lead_generator.planning.models import PlanningDocument
        from lead_generator.planning.document_cache import document_cache_directory
        class Response:
            headers = Message()
            headers['Content-Type'] = 'application/pdf'
            headers['ETag'] = '"same-etag"'
            def __init__(self, version): self.version = version
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'%PDF-1.7 version ' + str(self.version).encode()
            def geturl(self): return f'https://example.test/v{self.version}.pdf'
        count = []
        def open_request(request, timeout):
            count.append(True)
            if len(count) == 1: return Response(1)
            if len(count) == 2:
                self.assertEqual(request.get_header('If-none-match'), '"same-etag"')
                raise HTTPError('https://example.test/v2.pdf', 304, 'Not Modified', Message(), None)
            self.assertIsNone(request.get_header('If-none-match'))
            return Response(2)
        doc = PlanningDocument('Drawing', 'https://example.test/document.pdf')
        with tempfile.TemporaryDirectory() as directory, document_cache_directory(Path(directory)), patch.object(leads, '_throttle_request'):
            opener = SimpleNamespace(open=open_request)
            first = leads.download_document_file(doc, opener=opener)
            second = leads.download_document_file(doc, opener=opener)
            self.assertNotEqual(first.payload, second.payload)
            self.assertEqual(second.final_url, 'https://example.test/v2.pdf')

    def test_saved_run_has_exclusive_ownership(self):
        from lead_generator.planning.run_storage import RunStorage
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.sqlite3'
            first = RunStorage(path, {'schema': 1})
            try:
                with self.assertRaisesRegex(ValueError, 'already running'):
                    RunStorage(path, {'schema': 1}, resume=True)
            finally:
                first.close()
            resumed = RunStorage(path, {'schema': 1}, resume=True)
            resumed.close()


if __name__ == '__main__':
    unittest.main()
