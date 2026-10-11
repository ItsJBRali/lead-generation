import unittest
from unittest.mock import patch

from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient, monitor_council_requests
import lead_generator.planning.http as planning_http


class HttpControlsTest(unittest.TestCase):
    def test_request_spacing_uses_current_monotonic_time(self):
        client = CouncilHttpClient(min_delay_seconds=1)
        with (
            patch.object(planning_http, 'monotonic', side_effect=[1000.0, 1000.1]),
            patch.object(planning_http, '_interruptible_sleep') as pause,
            patch.dict(CouncilHttpClient._shared_last_request_at, {}, clear=True),
            patch.dict(CouncilHttpClient._blocked_until, {}, clear=True),
        ):
            client._wait_for_turn('https://example.test/search')
            client._wait_for_turn('https://example.test/search')
        pause.assert_called_once()
        self.assertAlmostEqual(pause.call_args.args[0], 0.9)

    def test_modern_agile_request_honours_cancellation(self):
        client = CouncilHttpClient(min_delay_seconds=0)
        scraper = AgilePlanningScraper(
            AgileCouncilConfig('Council', 'https://planning.agileapplications.co.uk/council'),
            http_client=client,
        )
        class DirectResponse:
            status = 200
            class headers:
                @staticmethod
                def get_content_charset():
                    return 'utf-8'
            @staticmethod
            def read():
                return b'[]'
        with (
            patch('http.client.HTTPSConnection') as direct_connection,
            patch.object(client, '_opener') as opener,
            monitor_council_requests(lambda: None, should_cancel=lambda: True),
        ):
            direct_connection.return_value.getresponse.return_value = DirectResponse()
            with self.assertRaises(CouncilFetchError):
                scraper._api_get('application/search', {}, 'client')
        direct_connection.assert_not_called()
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
