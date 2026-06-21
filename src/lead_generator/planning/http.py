from __future__ import annotations

from datetime import datetime, timezone
from http.cookiejar import CookieJar
from dataclasses import dataclass
import json
import re
import ssl
from time import monotonic, sleep
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
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
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36 LeadGeneratorPlanningScraper/0.1"
        ),
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
        self._tls_compat = False
        self._last_request_at = 0.0
        self._cookies = CookieJar()

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        request = Request(url, headers=request_headers)
        response = self._send(request, url)
        accept_url = _disclaimer_accept_url(response)
        if accept_url:
            self.post_form(accept_url, {})
            response = self._send(request, url)
        return response

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

    def post_json(self, url: str, data: object) -> FetchResponse:
        encoded = json.dumps(data).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
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
                    if _looks_like_waf_challenge(body):
                        raise CouncilFetchError(f"Blocked by web application firewall while fetching {url}")
                    return FetchResponse(
                        url=response.geturl(),
                        status_code=getattr(response, "status", 200),
                        text=body,
                    )
            except HTTPError as exc:
                if exc.code in {429, 503} and attempt < self.retries:
                    last_error = exc
                    sleep(_retry_delay_seconds(exc, attempt))
                    continue
                if exc.code < 500 or attempt == self.retries:
                    raise CouncilFetchError(f"HTTP {exc.code} while fetching {url}") from exc
                last_error = exc
            except URLError as exc:
                if self.verify_tls and _is_tls_certificate_error(exc):
                    self.verify_tls = False
                    last_error = exc
                    continue
                if not self._tls_compat and _is_tls_compatibility_error(exc):
                    self._tls_compat = True
                    last_error = exc
                    continue
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
        context = ssl.create_default_context(cafile=cafile)
        if self._tls_compat:
            context.set_ciphers("DEFAULT:@SECLEVEL=1")
        return context

    def _opener(self):
        handlers = [HTTPCookieProcessor(self._cookies)]
        context = self._ssl_context()
        if context is not None:
            handlers.append(HTTPSHandler(context=context))
        return build_opener(*handlers)


def _retry_delay_seconds(exc: HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 20.0)
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return min(max((retry_time - datetime.now(timezone.utc)).total_seconds(), 0.0), 20.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2.0 * (attempt + 1), 10.0)


def _is_tls_certificate_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    text = f"{reason or ''} {exc}".casefold()
    return "certificate" in text and "ssl" in text


def _is_tls_compatibility_error(exc: Exception) -> bool:
    reason = getattr(exc, "reason", None)
    text = f"{reason or ''} {exc}".casefold()
    return any(token in text for token in ("forcibly closed", "winerror 10054")) or (
        "ssl" in text
        and any(
            token in text
            for token in (
                "dh key too small",
                "legacy sigalg disallowed",
                "unsafe legacy renegotiation",
                "tlsv1 alert protocol version",
                "wrong version number",
            )
        )
    )


def _looks_like_waf_challenge(text: str) -> bool:
    lowered = text[:5000].casefold()
    return "_incapsula_resource" in lowered or "incapsula" in lowered and "noindex,nofollow" in lowered


def _disclaimer_accept_url(response: FetchResponse) -> str | None:
    if "disclaimer" not in response.url.casefold() and "disclaimer" not in response.text[:2000].casefold():
        return None
    match = re.search(r"<form[^>]+action=[\"']([^\"']*Disclaimer/Accept[^\"']*)[\"']", response.text, flags=re.IGNORECASE)
    if not match:
        return None
    return urljoin(response.url, match.group(1))
