from __future__ import annotations

import ssl
from urllib.error import URLError

import pytest

from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient


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
