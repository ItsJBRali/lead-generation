import csv
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch
from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import lead_generator.planning.leads as leads
import lead_generator.planning.http as planning_http
from lead_generator.planning.adapters.achieveforms import AchieveFormsMetadata, AchieveFormsPlanningScraper
from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.enrichment import ContactEnrichment
from lead_generator.planning.http import CouncilHttpClient, monitor_council_requests
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from test_leads import write_search_fixture


class JsonResponse:
    status = 200
    headers = Message()

    def __init__(self, body, url):
        self.body, self.url = body, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body

    def geturl(self):
        return self.url


class ReviewEdgeCases(unittest.TestCase):
    def test_saved_lead_does_not_hide_failed_coverage(self):
        for pipelined in (False, True):
            with self.subTest(pipelined=pipelined), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                area, catalogue = write_search_fixture(root, ['Council'])
                config = leads.LeadSearchConfig(
                    area, root, date(2026, 7, 1), date(2026, 7, 31), [],
                    catalogue_path=catalogue, worker_count=1,
                    download_application_files=False, pipelined=pipelined,
                )
                applications = [PlanningApplication(
                    authority='Council', uid=str(i), reference=f'26/{i}',
                    url=f'https://council.test/{i}', date_received='2026-07-15',
                ) for i in range(2)]
                original_append = leads.append_csv_row

                def append(path, fields, row):
                    if row.get('Reference') == '26/1':
                        raise OSError('Could not save second lead')
                    return original_append(path, fields, row)

                with (
                    patch.object(leads, 'discover_portal_applications', return_value=applications),
                    patch.object(leads, 'discover_planit_reconciliation_applications', side_effect=RuntimeError('PlanIt unavailable')),
                    patch.object(leads, 'append_csv_row', side_effect=append),
                ):
                    result = leads.run_lead_search(config)
                self.assertEqual(result.leads_found, 1)
                self.assertEqual(result.completion, 'Failed')
                self.assertEqual(result.failed_councils, ['Council'])

    def test_deferred_achieveforms_lookup_obeys_cancellation(self):
        metadata = AchieveFormsMetadata(
            listing_url='https://council.test/search', form_uri='form', form_id='id',
            form_name='Planning', weekly_lookup_id='weekly', detail_lookup_id='detail',
            documents_lookup_id='documents',
        )
        app = PlanningApplication(
            authority='Council', uid='A', reference='A', url='https://council.test/A',
            raw={'portal_family': 'achieveforms', 'achieveforms_metadata': asdict(metadata)},
        )
        with (
            patch.object(AchieveFormsPlanningScraper, '_session_id', return_value='sid'),
            patch('lead_generator.planning.http.CouncilHttpClient._opener') as opener,
        ):
            with self.assertRaises(leads.DocumentDownloadCancelledError):
                leads.discover_application_documents(app, should_cancel=lambda: True)
            opener.assert_not_called()

    def test_achieveforms_cancellation_during_auth_stops_document_request(self):
        metadata = AchieveFormsMetadata(
            listing_url='https://council.test/search', form_uri='form', form_id='id',
            form_name='Planning', weekly_lookup_id='weekly', detail_lookup_id='detail',
            documents_lookup_id='documents',
        )
        app = PlanningApplication(
            authority='Council', uid='A', reference='A', url='https://council.test/A',
            raw={'portal_family': 'achieveforms', 'achieveforms_metadata': asdict(metadata)},
        )
        cancelled = threading.Event()

        def open_request(request, timeout):
            cancelled.set()
            return JsonResponse(b'{"auth-session":"sid"}', request.full_url)

        with patch.object(CouncilHttpClient, '_opener') as opener:
            opener.return_value.open.side_effect = open_request
            with self.assertRaises(leads.DocumentDownloadCancelledError):
                leads.discover_application_documents(app, should_cancel=cancelled.is_set)
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_real_agile_client_preserves_request_and_retries_429_with_activity(self):
        client = CouncilHttpClient(min_delay_seconds=0, retries=1)
        scraper = AgilePlanningScraper(AgileCouncilConfig('Council', 'https://planning.agileapplications.co.uk/council'), http_client=client)
        url = scraper.API_URL + 'application/search'
        headers = Message()
        headers['Retry-After'] = '0'
        activity = []
        params = {'registrationDateFrom': '2026-07-01T00:00:00+01:00', 'status': 'registered'}
        with (
            patch.object(client, '_opener') as opener,
            patch.object(planning_http, '_interruptible_sleep'),
            patch.dict(CouncilHttpClient._shared_last_request_at, {}, clear=True),
            patch.dict(CouncilHttpClient._blocked_until, {}, clear=True),
            monitor_council_requests(lambda: activity.append(True)),
        ):
            opener.return_value.open.side_effect = [
                HTTPError(url, 429, 'Too many requests', headers, None),
                JsonResponse(b'[]', url),
            ]
            text, _ = scraper._api_get('application/search', params, 'client-code')
            self.assertEqual(text, '[]')
            self.assertEqual(opener.return_value.open.call_count, 2)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(urlsplit(request.full_url).path, '//api/application/search')
            self.assertIn('T00:00:00%2B01:00', request.full_url)
            self.assertEqual(parse_qs(urlsplit(request.full_url).query), {key: [value] for key, value in params.items()})
            self.assertEqual(request.get_header('X-client'), 'client-code')
            self.assertEqual(request.get_header('X-service'), 'PA')
        self.assertGreaterEqual(len(activity), 5)

    def test_request_spacing_is_shared_between_clients_and_resets_after_idle(self):
        first = CouncilHttpClient(min_delay_seconds=1)
        second = CouncilHttpClient(min_delay_seconds=1)
        with (
            patch.object(planning_http, 'monotonic', side_effect=[1000.0, 1000.1, 1010.0, 1010.1]),
            patch.object(planning_http, '_interruptible_sleep') as pause,
            patch.dict(CouncilHttpClient._shared_last_request_at, {}, clear=True),
            patch.dict(CouncilHttpClient._blocked_until, {}, clear=True),
        ):
            for client in (first, second, second, first):
                client._wait_for_turn('https://shared.test/search')
        self.assertEqual(pause.call_count, 2)
        for invocation in pause.call_args_list:
            self.assertAlmostEqual(invocation.args[0], 0.9)

    def test_pipeline_document_retries_preserve_council_identity_and_enrichment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council A', 'Council B'])
            config = leads.LeadSearchConfig(
                area, root, date(2026, 7, 1), date(2026, 7, 31), [],
                catalogue_path=catalogue, worker_count=1, pipelined=True,
            )
            attempts = {}

            def primary(target, *args, **kwargs):
                return [PlanningApplication(
                    authority=target.authority, uid='A', reference='26/001',
                    url=target.base_url + '/A', date_received='2026-07-15',
                )]

            def documents(app, **kwargs):
                attempts[app.authority] = attempts.get(app.authority, 0) + 1
                return leads.DocumentDiscoveryResult(
                    documents=[PlanningDocument(title='Plans', url=app.url + '.pdf')],
                    successful_sources=[app.url],
                    failed_sources=[leads.DocumentSourceFailure(app.url, 'Temporary failure')]
                    if attempts[app.authority] == 1 else [],
                )

            def enrich(folder, **kwargs):
                self.assertEqual(attempts[folder.parent.name], 2)
                return ContactEnrichment(architect_company_names=['Verified Architect'])

            with (
                patch.object(leads, 'discover_portal_applications', side_effect=primary),
                patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]),
                patch.object(leads, 'discover_application_documents', side_effect=documents),
                patch.object(leads, '_wait_for_document_retry_cooldown', return_value=True),
                patch.object(leads, '_download_pdf_documents_once', return_value=leads.DocumentDownloadBatchResult(downloaded_count=1)) as download,
                patch.object(leads, 'enrich_application_folder', side_effect=enrich) as enrichment,
            ):
                result = leads.run_lead_search(config)
            with result.csv_path.open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(result.leads_found, 2)
            self.assertEqual(result.captured_documents, 2)
            self.assertEqual(download.call_count, 2)
            self.assertEqual(enrichment.call_count, 2)
            self.assertTrue(all(row['Architect / Company Name'] == 'Verified Architect' for row in rows))

    def test_enrichment_cancellation_flushes_changes_after_last_checkpoint(self):
        for pipelined in (False, True):
            with self.subTest(pipelined=pipelined), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                area, catalogue = write_search_fixture(root, ['Council'])
                config = leads.LeadSearchConfig(
                    area, root, date(2026, 7, 1), date(2026, 7, 31), [],
                    catalogue_path=catalogue, worker_count=1, pipelined=pipelined,
                )
                applications = [PlanningApplication(
                    authority='Council', uid=str(i), reference=f'26/{i}',
                    url=f'https://council.test/{i}', date_received='2026-07-15',
                ) for i in range(28)]
                cancelled = threading.Event()
                enriched = []

                def enrich(folder, **kwargs):
                    enriched.append(folder)
                    if len(enriched) == 26:
                        cancelled.set()
                    return ContactEnrichment(architect_company_names=['Saved Architect'])

                with (
                    patch.object(leads, 'discover_portal_applications', return_value=applications),
                    patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]),
                    patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult()),
                    patch.object(leads, 'enrich_application_folder', side_effect=enrich),
                    patch.object(leads, 'write_csv', wraps=leads.write_csv) as write,
                ):
                    result = leads.run_lead_search(config, should_cancel=cancelled.is_set)
                with result.csv_path.open(newline='', encoding='utf-8') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(result.completion, 'Cancelled')
                self.assertEqual(len(rows), 28)
                self.assertEqual(sum(row['Architect / Company Name'] == 'Saved Architect' for row in rows), 26)
                self.assertEqual(write.call_count, 2)

    def test_pipeline_drains_more_work_than_queue_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            area, catalogue = write_search_fixture(root, ['Council'])
            config = leads.LeadSearchConfig(
                area, root, date(2026, 7, 1), date(2026, 7, 31), [],
                catalogue_path=catalogue, worker_count=1, pipelined=True,
            )
            applications = [PlanningApplication(
                authority='Council', uid=str(i), reference=f'26/{i}',
                url=f'https://council.test/{i}', date_received='2026-07-15',
            ) for i in range(270)]
            with (
                patch.object(leads, 'discover_portal_applications', return_value=applications),
                patch.object(leads, 'discover_planit_reconciliation_applications', return_value=[]),
                patch.object(leads, 'discover_application_documents', return_value=leads.DocumentDiscoveryResult()),
                patch.object(leads, 'enrich_application_folder', return_value=ContactEnrichment(architect_company_names=['Architect'])) as enrich,
            ):
                result = leads.run_lead_search(config)
            with result.csv_path.open(newline='', encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(result.completion, 'Completed')
            self.assertEqual(result.leads_found, 270)
            self.assertEqual(enrich.call_count, 270)
            self.assertEqual(len(rows), 270)
            self.assertTrue(all(row['Architect / Company Name'] == 'Architect' for row in rows))


if __name__ == '__main__':
    unittest.main()
