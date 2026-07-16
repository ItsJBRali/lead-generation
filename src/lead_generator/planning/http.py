from __future__ import annotations

from datetime import datetime, timezone
from http.cookiejar import CookieJar
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
import json
import re
import ssl
import threading
from time import monotonic, sleep
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
)

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as selenium_conditions
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover - optional outside the packaged GUI
    webdriver = None
    WebDriverException = Exception
    By = None
    selenium_conditions = None
    WebDriverWait = None

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


@dataclass(slots=True)
class BinaryFetchResponse:
    url: str
    status_code: int
    body: bytes


@dataclass(slots=True)
class _RawFetchResponse:
    url: str
    status_code: int
    body: bytes
    charset: str


_REQUEST_MONITOR = threading.local()
_BROWSER_FALLBACK_GATE = threading.BoundedSemaphore(1)


@contextmanager
def monitor_council_requests(
    on_activity,
    *,
    should_cancel=None,
):
    """Report HTTP progress and allow an abandoned council search to stop cleanly."""

    previous = getattr(_REQUEST_MONITOR, "state", None)
    _REQUEST_MONITOR.state = (on_activity, should_cancel)
    try:
        yield
    finally:
        if previous is None:
            try:
                del _REQUEST_MONITOR.state
            except AttributeError:
                pass
        else:
            _REQUEST_MONITOR.state = previous


def _report_request_activity() -> None:
    state = getattr(_REQUEST_MONITOR, "state", None)
    if not state or not state[0]:
        return
    try:
        state[0]()
    except Exception:
        pass


def _request_cancelled() -> bool:
    state = getattr(_REQUEST_MONITOR, "state", None)
    return bool(state and state[1] and state[1]())


def _raise_if_request_cancelled() -> None:
    if _request_cancelled():
        raise CouncilFetchError("Council search request was cancelled")


def _interruptible_sleep(seconds: float) -> None:
    deadline = monotonic() + max(seconds, 0.0)
    while True:
        _raise_if_request_cancelled()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(remaining, 0.25))


class CouncilHttpClient:
    _rate_limit_lock = threading.Lock()
    _concurrency_lock = threading.Lock()
    _shared_last_request_at: dict[str, float] = {}
    _blocked_until: dict[str, float] = {}
    _concurrency_gates: dict[tuple[str, int], threading.BoundedSemaphore] = {}

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        min_delay_seconds: float = 1.0,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        retries: int = 2,
        verify_tls: bool = True,
        ca_file: str | None = None,
        rate_limit_key: str | None = None,
        concurrency_key: str | None = None,
        concurrency_limit: int = 2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.user_agent = user_agent
        self.retries = retries
        self.verify_tls = verify_tls
        self.ca_file = ca_file
        self.rate_limit_key = rate_limit_key
        self.concurrency_key = concurrency_key
        self.concurrency_limit = max(concurrency_limit, 1)
        self._tls_compat = False
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

    def get_bytes(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> BinaryFetchResponse:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"

        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        request = Request(url, headers=request_headers)
        response = self._send_raw(request, url)
        return BinaryFetchResponse(
            url=response.url,
            status_code=response.status_code,
            body=response.body,
        )

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        encoded = urlencode(data).encode("utf-8")
        request_headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if headers:
            request_headers.update(headers)
        request = Request(
            url,
            data=encoded,
            headers=request_headers,
            method="POST",
        )
        return self._send(request, url)

    def post_json(
        self,
        url: str,
        data: object,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        encoded = json.dumps(data).encode("utf-8")
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            request_headers.update(headers)
        request = Request(
            url,
            data=encoded,
            headers=request_headers,
            method="POST",
        )
        return self._send(request, url)

    def _send(self, request: Request, url: str) -> FetchResponse:
        response = self._send_raw(request, url)
        body = response.body.decode(response.charset, errors="replace")
        if _looks_like_waf_challenge(body):
            raise CouncilFetchError(f"Blocked by web application firewall while fetching {url}")
        return FetchResponse(
            url=response.url,
            status_code=response.status_code,
            text=body,
        )

    def _send_raw(self, request: Request, url: str) -> _RawFetchResponse:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            _raise_if_request_cancelled()
            _report_request_activity()
            try:
                with self._request_slot():
                    _raise_if_request_cancelled()
                    self._wait_for_turn(url)
                    _raise_if_request_cancelled()
                    _report_request_activity()
                    with self._opener().open(request, timeout=self.timeout_seconds) as response:
                        charset = response.headers.get_content_charset() or "utf-8"
                        body = response.read()
                        status_code = getattr(response, "status", 200)
                        response_url = response.geturl()
                _report_request_activity()
                if not body.strip():
                    last_error = CouncilFetchError(f"Empty response while fetching {url}")
                    if attempt == self.retries:
                        raise last_error
                    self._pause_before_retry(url, attempt, minimum_seconds=3.0)
                    continue
                return _RawFetchResponse(
                    url=response_url,
                    status_code=status_code,
                    body=body,
                    charset=charset,
                )
            except HTTPError as exc:
                if exc.code in {429, 503} and attempt < self.retries:
                    last_error = exc
                    self._pause_before_retry(url, attempt, exc=exc)
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

            self._pause_before_retry(url, attempt, minimum_seconds=0.5 * (attempt + 1))

        raise CouncilFetchError(f"Could not fetch {url}") from last_error

    def _wait_for_turn(self, url: str) -> None:
        key = self._throttle_key(url)
        with self._rate_limit_lock:
            now = monotonic()
            next_allowed = max(
                self._shared_last_request_at.get(key, 0.0) + self.min_delay_seconds,
                self._blocked_until.get(key, 0.0),
            )
            self._shared_last_request_at[key] = next_allowed
        if next_allowed > now:
            _interruptible_sleep(next_allowed - now)
        _report_request_activity()

    def _pause_before_retry(
        self,
        url: str,
        attempt: int,
        *,
        exc: HTTPError | None = None,
        minimum_seconds: float = 0.0,
    ) -> None:
        delay = _retry_delay_seconds(exc, attempt) if exc else minimum_seconds
        delay = max(delay, minimum_seconds)
        key = self._throttle_key(url)
        with self._rate_limit_lock:
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), monotonic() + delay)
        _interruptible_sleep(delay)
        _report_request_activity()

    def _throttle_key(self, url: str | None = None) -> str:
        if self.rate_limit_key:
            return self.rate_limit_key
        source = url or ""
        return urlsplit(source).netloc.casefold() or self.configured_host_key()

    def configured_host_key(self) -> str:
        return f"client:{id(self)}"

    def _request_slot(self):
        if not self.concurrency_key:
            return nullcontext()
        gate_key = (self.concurrency_key, self.concurrency_limit)
        with self._concurrency_lock:
            gate = self._concurrency_gates.get(gate_key)
            if gate is None:
                gate = threading.BoundedSemaphore(self.concurrency_limit)
                self._concurrency_gates[gate_key] = gate
        return self._cancellable_request_slot(gate)

    @contextmanager
    def _cancellable_request_slot(self, gate):
        while not gate.acquire(timeout=0.25):
            _raise_if_request_cancelled()
        try:
            yield
        finally:
            gate.release()

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


class CouncilBrowserClient:
    """Small Selenium-backed client used only when a portal requires JavaScript."""

    def __init__(self, *, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._driver = None
        self._owns_gate = False

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        del headers
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        driver = self._ensure_driver()
        try:
            _report_request_activity()
            driver.get(url)
            self._wait_for_usable_page()
            return self._response()
        except CouncilFetchError:
            raise
        except Exception as exc:
            raise CouncilFetchError(f"Browser fallback could not fetch {url}: {exc}") from exc

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        driver = self._ensure_driver()
        try:
            _report_request_activity()
            if str(data.get("ajax", "")).casefold() == "true" or (
                headers and headers.get("X-Requested-With", "").casefold() == "xmlhttprequest"
            ):
                return self._post_ajax(url, data)
            root = driver.find_element(By.TAG_NAME, "html")
            submitted = driver.execute_script(
                """
                const target = new URL(arguments[0], window.location.href);
                const values = arguments[1];
                const forms = Array.from(document.forms);
                const form = forms.find((candidate) => {
                    const action = new URL(candidate.action || window.location.href, window.location.href);
                    return action.pathname === target.pathname;
                }) || forms[0];
                if (!form) return false;
                for (const [name, value] of Object.entries(values)) {
                    const controls = Array.from(form.elements).filter((item) => item.name === name);
                    for (const control of controls) {
                        const type = (control.type || '').toLowerCase();
                        if (type === 'radio' || type === 'checkbox') {
                            control.checked = String(control.value) === String(value);
                        } else {
                            control.value = value == null ? '' : String(value);
                        }
                        control.dispatchEvent(new Event('input', {bubbles: true}));
                        control.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
                const submit = form.querySelector('button[type="submit"], input[type="submit"]');
                if (submit) submit.click(); else form.requestSubmit();
                return true;
                """,
                url,
                data,
            )
            if not submitted:
                raise CouncilFetchError(f"Browser fallback could not find the form for {url}")
            try:
                WebDriverWait(driver, self.timeout_seconds).until(selenium_conditions.staleness_of(root))
            except Exception:
                pass
            self._wait_for_usable_page()
            return self._response()
        except CouncilFetchError:
            raise
        except Exception as exc:
            raise CouncilFetchError(f"Browser fallback could not submit {url}: {exc}") from exc

    def _post_ajax(self, url: str, data: dict[str, str]) -> FetchResponse:
        driver = self._ensure_driver()
        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            fetch(arguments[0], {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: new URLSearchParams(arguments[1]).toString()
            }).then(async (response) => done({
                status: response.status,
                url: response.url,
                text: await response.text()
            })).catch((error) => done({error: String(error)}));
            """,
            url,
            data,
        )
        _report_request_activity()
        if not isinstance(result, dict) or result.get("error"):
            detail = result.get("error") if isinstance(result, dict) else "no response"
            raise CouncilFetchError(f"Browser fallback AJAX request failed for {url}: {detail}")
        status_code = int(result.get("status") or 0)
        if status_code >= 400:
            raise CouncilFetchError(f"HTTP {status_code} while fetching {url}")
        return FetchResponse(
            url=str(result.get("url") or url),
            status_code=status_code or 200,
            text=str(result.get("text") or ""),
        )

    def close(self) -> None:
        driver, self._driver = self._driver, None
        try:
            if driver is not None:
                driver.quit()
        finally:
            if self._owns_gate:
                self._owns_gate = False
                _BROWSER_FALLBACK_GATE.release()

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        if webdriver is None:
            raise CouncilFetchError("Browser fallback is unavailable because Selenium is not installed")
        while not _BROWSER_FALLBACK_GATE.acquire(timeout=0.25):
            _raise_if_request_cancelled()
            _report_request_activity()
        self._owns_gate = True
        try:
            self._driver = self._create_driver()
            self._driver.set_page_load_timeout(self.timeout_seconds)
            self._driver.set_script_timeout(self.timeout_seconds)
            return self._driver
        except Exception as exc:
            self.close()
            raise CouncilFetchError(f"Could not start the council browser fallback: {exc}") from exc

    def _create_driver(self):
        chrome_options = webdriver.ChromeOptions()
        self._configure_options(chrome_options)
        try:
            driver = webdriver.Chrome(options=chrome_options)
        except WebDriverException as chrome_error:
            edge_options = webdriver.EdgeOptions()
            self._configure_options(edge_options)
            try:
                driver = webdriver.Edge(options=edge_options)
            except WebDriverException as edge_error:
                raise CouncilFetchError(
                    f"Chrome could not start ({chrome_error}); Edge could not start ({edge_error})"
                ) from edge_error
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": (
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                        "Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});"
                    )
                },
            )
        except Exception:
            pass
        return driver

    def _configure_options(self, options) -> None:
        for argument in (
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--window-size=1280,1000",
            "--window-position=-32000,-32000",
            "--disable-blink-features=AutomationControlled",
            "--lang=en-GB",
            "--log-level=3",
        ):
            options.add_argument(argument)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

    def _wait_for_usable_page(self) -> None:
        driver = self._ensure_driver()

        def ready(_driver) -> bool:
            _raise_if_request_cancelled()
            _report_request_activity()
            try:
                source = _driver.page_source or ""
                lowered = source[:6000].casefold()
                title = (_driver.title or "").casefold()
                ready_state = _driver.execute_script("return document.readyState") == "complete"
                if ready_state and (
                    "403 forbidden" in title
                    or "service unavailable" in title
                    or "bad gateway" in title
                ):
                    return True
                challenge = len(source) < 5000 and (
                    "javascript is disabled" in lowered
                    or "awswaf" in lowered
                    or "captcha-sdk.awswaf.com" in lowered
                    or "incapsula incident id" in lowered
                    or "request unsuccessful" in lowered
                )
                challenge = challenge or "checking you're not a bot" in lowered or "azure waf" in title
                return ready_state and len(source) > 500 and not challenge
            except Exception:
                return False

        try:
            WebDriverWait(driver, self.timeout_seconds, poll_frequency=0.25).until(ready)
        except Exception as exc:
            raise CouncilFetchError(
                f"Browser challenge did not complete while fetching {getattr(driver, 'current_url', '')}"
            ) from exc
        self._raise_for_error_page()

    def _raise_for_error_page(self) -> None:
        driver = self._ensure_driver()
        source = driver.page_source or ""
        title = (driver.title or "").casefold()
        opening = source[:12000].casefold()
        if any(
            token in title
            for token in ("403 forbidden", "service unavailable", "internal server error", "bad gateway")
        ) or (
            len(source) < 12000
            and any(token in opening for token in ("403 forbidden", ">503<", "service unavailable", "bad gateway"))
        ):
            raise CouncilFetchError(f"Council website error page while fetching {driver.current_url}")

    def _response(self) -> FetchResponse:
        driver = self._ensure_driver()
        return FetchResponse(url=driver.current_url, status_code=200, text=driver.page_source)

    def __del__(self):  # pragma: no cover - best effort during interpreter shutdown
        try:
            self.close()
        except Exception:
            pass


def browser_fallback_recommended(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(
        token in text
        for token in (
            "http 403",
            "http 405",
            "http 503",
            "empty response",
            "web application firewall",
            "council website error page",
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "northgate date search was not accepted",
        )
    )


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
                "unexpected_eof_while_reading",
                "eof occurred in violation of protocol",
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
