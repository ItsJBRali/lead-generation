from __future__ import annotations

from http.cookiejar import CookieJar
from dataclasses import dataclass
import ssl
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
)

try:
    import certifi
except ImportError:  # pragma: no cover - depends on the runtime environment
    certifi = None


class CouncilFetchError(RuntimeError):
    """Raised when a council page cannot be fetched."""


@dataclass(slots=True)
class FetchResponse:
    url: str
    status_code: int
    text: str


class CouncilHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        min_delay_seconds: float = 1.0,
        user_agent: str = "LeadGeneratorPlanningScraper/0.1 (+responsible planning data collection)",
        retries: int = 2,
        verify_tls: bool = True,
        ca_file: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.user_agent = user_agent
        self.retries = retries
        self.verify_tls = verify_tls
        self.ca_file = ca_file
        self._last_request_at = 0.0
        self._cookies = CookieJar()

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        request = Request(url, headers={"User-Agent": self.user_agent})
        return self._send(request, url)

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        encoded = urlencode(data).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        return self._send(request, url)

    def _send(self, request: Request, url: str) -> FetchResponse:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_for_turn()
            try:
                with self._opener().open(request, timeout=self.timeout_seconds) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = response.read().decode(charset, errors="replace")
                    return FetchResponse(
                        url=response.geturl(),
                        status_code=getattr(response, "status", 200),
                        text=body,
                    )
            except HTTPError as exc:
                if exc.code < 500 or attempt == self.retries:
                    raise CouncilFetchError(f"HTTP {exc.code} while fetching {url}") from exc
                last_error = exc
            except URLError as exc:
                if attempt == self.retries:
                    raise CouncilFetchError(f"Network error while fetching {url}: {exc.reason}") from exc
                last_error = exc

            sleep(0.5 * (attempt + 1))

        raise CouncilFetchError(f"Could not fetch {url}") from last_error

    def _wait_for_turn(self) -> None:
        elapsed = monotonic() - self._last_request_at
        if elapsed < self.min_delay_seconds:
            sleep(self.min_delay_seconds - elapsed)
        self._last_request_at = monotonic()

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.verify_tls and not self.ca_file and certifi is None:
            return None
        if not self.verify_tls:
            return ssl._create_unverified_context()
        cafile = self.ca_file or certifi.where()
        return ssl.create_default_context(cafile=cafile)

    def _opener(self):
        handlers = [HTTPCookieProcessor(self._cookies)]
        context = self._ssl_context()
        if context is not None:
            handlers.append(HTTPSHandler(context=context))
        return build_opener(*handlers)
