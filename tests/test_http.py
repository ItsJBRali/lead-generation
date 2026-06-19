from __future__ import annotations

import ssl
from urllib.error import URLError

from lead_generator.planning.http import CouncilHttpClient


def test_http_client_retries_with_unverified_tls_after_certificate_error() -> None:
    class FakeHeaders:
        def get_content_charset(self) -> str:
            return "utf-8"

    class FakeResponse:
        headers = FakeHeaders()
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self) -> str:
            return "https://planning.example.gov.uk/search"

        def read(self) -> bytes:
            return b"<html>ok</html>"

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
