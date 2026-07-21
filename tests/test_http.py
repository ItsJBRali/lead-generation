from __future__ import annotations

import ssl
import threading
from time import monotonic
from urllib.error import URLError

import pytest

import lead_generator.planning.http as planning_http
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient, monitor_council_requests


class FakeHeaders:
    def get_content_charset(self) -> str:
        return "utf-8"


class FakeResponse:
    headers = FakeHeaders()
    status = 200

    def __init__(self, body: bytes = b"<html>ok</html>") -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self) -> str:
        return "https://planning.example.gov.uk/search"

    def read(self) -> bytes:
        return self.body


def test_http_client_retries_with_unverified_tls_after_certificate_error() -> None:
    class FakeOpener:
        def __init__(self, client: CouncilHttpClient) -> None:
            self.client = client

        def open(self, request, timeout):
            if self.client.verify_tls:
                raise URLError(ssl.SSLCertVerificationError("certificate has expired"))
            return FakeResponse()

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener(self)

    client = FakeClient(min_delay_seconds=0)

    response = client.get("https://planning.example.gov.uk/search")

    assert response.status_code == 200
    assert response.text == "<html>ok</html>"
    assert client.verify_tls is False


def test_http_client_uses_browser_like_user_agent_by_default() -> None:
    client = CouncilHttpClient(min_delay_seconds=0)

    assert client.user_agent.startswith("Mozilla/5.0")
    assert "Chrome/" in client.user_agent


def test_http_client_preserves_binary_response_bytes() -> None:
    body = b"\x02\x00\x00\x00\xff\x00planning"

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(body)

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener()

    response = FakeClient(min_delay_seconds=0).get_bytes(
        "https://planning.example.gov.uk/api/cases"
    )

    assert response.status_code == 200
    assert response.body == body


def test_http_request_monitor_reports_activity() -> None:
    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse()

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener()

    activity: list[float] = []
    client = FakeClient(min_delay_seconds=0)

    with monitor_council_requests(lambda: activity.append(monotonic())):
        client.get("https://planning.example.gov.uk/search")

    assert len(activity) >= 3


def test_http_request_monitor_cancels_abandoned_request() -> None:
    class UnexpectedOpener:
        def open(self, request, timeout):
            raise AssertionError("Cancelled request should not reach the network")

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return UnexpectedOpener()

    client = FakeClient(min_delay_seconds=0)

    with (
        monitor_council_requests(lambda: None, should_cancel=lambda: True),
        pytest.raises(CouncilFetchError, match="cancelled"),
    ):
        client.get("https://planning.example.gov.uk/search")


def test_http_client_retries_with_tls_compat_after_connection_reset() -> None:
    class FakeOpener:
        def __init__(self, client: CouncilHttpClient) -> None:
            self.client = client

        def open(self, request, timeout):
            if not self.client._tls_compat:
                raise URLError("[WinError 10054] An existing connection was forcibly closed by the remote host")
            return FakeResponse()

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener(self)

    client = FakeClient(min_delay_seconds=0)

    response = client.get("https://planning.example.gov.uk/search")

    assert response.status_code == 200
    assert client._tls_compat is True


def test_http_client_retries_with_tls_compat_after_unexpected_eof() -> None:
    class FakeOpener:
        def __init__(self, client: CouncilHttpClient) -> None:
            self.client = client

        def open(self, request, timeout):
            if not self.client._tls_compat:
                raise URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
            return FakeResponse()

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener(self)

    client = FakeClient(min_delay_seconds=0)

    response = client.get("https://planning.example.gov.uk/search")

    assert response.status_code == 200
    assert client._tls_compat is True


def test_http_client_rejects_waf_placeholder_pages() -> None:
    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(
                b"""
                <html><head><META NAME="robots" CONTENT="noindex,nofollow">
                <script src="/_Incapsula_Resource?SWJIYLWA=abc"></script></head><body></body></html>
                """
            )

    class FakeClient(CouncilHttpClient):
        def _opener(self):
            return FakeOpener()

    client = FakeClient(min_delay_seconds=0)

    with pytest.raises(CouncilFetchError, match="web application firewall"):
        client.get("https://planning.example.gov.uk/search")


def test_browser_client_falls_back_from_chrome_to_edge(monkeypatch) -> None:
    configured: list[tuple[str, str]] = []

    class FakeOptions:
        def add_argument(self, argument: str) -> None:
            configured.append(("argument", argument))

        def add_experimental_option(self, name: str, value: object) -> None:
            configured.append((name, str(value)))

    class FakeDriver:
        def execute_cdp_cmd(self, command: str, payload: object) -> None:
            configured.append(("cdp", command))

    def broken_chrome(*, options):
        raise planning_http.WebDriverException("Chrome unavailable")

    def working_edge(*, options):
        configured.append(("driver", "edge"))
        return FakeDriver()

    monkeypatch.setattr(planning_http, "ChromeOptions", FakeOptions)
    monkeypatch.setattr(planning_http, "ChromeWebDriver", broken_chrome)
    monkeypatch.setattr(planning_http, "EdgeOptions", FakeOptions)
    monkeypatch.setattr(planning_http, "EdgeWebDriver", working_edge)

    driver = planning_http.CouncilBrowserClient()._create_driver()

    assert isinstance(driver, FakeDriver)
    assert ("driver", "edge") in configured
    assert ("argument", "--disable-blink-features=AutomationControlled") in configured


def test_http_client_retries_empty_responses_before_returning_content() -> None:
    class FakeOpener:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, request, timeout):
            self.calls += 1
            return FakeResponse(b"" if self.calls == 1 else b"<html>ready</html>")

    class FakeClient(CouncilHttpClient):
        def __init__(self) -> None:
            super().__init__(min_delay_seconds=0, retries=1)
            self.fake_opener = FakeOpener()

        def _opener(self):
            return self.fake_opener

    client = FakeClient()

    response = client.get("https://planning.example.gov.uk/search")

    assert response.text == "<html>ready</html>"
    assert client.fake_opener.calls == 2


def test_http_client_limits_concurrent_requests_for_same_platform() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_opened = threading.Event()

    class BlockingResponse(FakeResponse):
        def __init__(self, first: bool) -> None:
            super().__init__()
            self.first = first

        def read(self) -> bytes:
            if self.first:
                first_started.set()
                assert release_first.wait(timeout=2)
            else:
                second_opened.set()
            return self.body

    class FakeOpener:
        def __init__(self, first: bool) -> None:
            self.first = first

        def open(self, request, timeout):
            return BlockingResponse(self.first)

    class FakeClient(CouncilHttpClient):
        def __init__(self, first: bool) -> None:
            super().__init__(
                min_delay_seconds=0,
                concurrency_key="portal:test-concurrency",
                concurrency_limit=1,
            )
            self.fake_opener = FakeOpener(first)

        def _opener(self):
            return self.fake_opener

    first_client = FakeClient(True)
    second_client = FakeClient(False)
    first_thread = threading.Thread(target=first_client.get, args=("https://one.example.gov.uk/search",))
    second_thread = threading.Thread(target=second_client.get, args=("https://two.example.gov.uk/search",))

    first_thread.start()
    assert first_started.wait(timeout=1)
    second_thread.start()
    assert not second_opened.wait(timeout=0.1)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert second_opened.is_set()
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
