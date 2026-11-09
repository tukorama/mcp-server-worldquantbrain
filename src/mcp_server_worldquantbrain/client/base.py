import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2

from mcp_server_worldquantbrain.client.retry import (
    RETRY_SAFE,
    RETRY_UNSAFE,
    backoff,
    clamp,
    parse_retry_after,
)
from mcp_server_worldquantbrain.client.session import SessionData, SessionStore
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import (
    AuthError,
    BiometricRequired,
    BrainError,
    BrainValidationError,
    ConfigError,
    NotFound,
    PermissionDenied,
    PollTimeout,
    RateLimited,
    ServerError,
)
from mcp_server_worldquantbrain.models.auth import AuthStatus

logger = logging.getLogger(__name__)

_sleep = asyncio.sleep
_monotonic = time.monotonic

EXPIRY_MARGIN = 120.0
UNKNOWN_EXPIRY_RECHECK = 300.0
PROBE_CACHE_SECONDS = 60.0
LOGIN_FAILURE_COOLDOWN = 60.0
POLL_GRACE_SECONDS = 5.0
# A smaller remainder is clock rounding, not time worth another request.
POLL_MIN_WAIT = 0.001
_CONCURRENCY_ERROR = re.compile(r"concurrent|limit.?exceeded")
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}

Params = Mapping[str, Any] | Sequence[tuple[str, Any]] | None
Terminal = Callable[[httpx2.Response], bool]
OnPoll = Callable[[httpx2.Response, float], Awaitable[None]]


def parse_json(response: httpx2.Response) -> Any:
    if not response.content.strip():
        return None
    try:
        return response.json()
    except ValueError:
        return None


def default_terminal(response: httpx2.Response) -> bool:
    return (
        200 <= response.status_code < 300
        and "retry-after" not in response.headers
        and parse_json(response) is not None
    )


class BrainClient:
    """Async client for the BRAIN REST API. Handles login, retries and long polling."""

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.store = SessionStore(settings.resolved_session_file)
        self._http = httpx2.AsyncClient(
            base_url=settings.base_url,
            follow_redirects=False,
            timeout=settings.http_timeout,
            transport=transport,
            headers={
                "Accept": "application/json;version=2.0",
                "User-Agent": "mcp-server-worldquantbrain",
            },
        )
        self._lock = asyncio.Lock()
        self._generation = 0
        self._restored = False
        self._file_mtime: float | None = None
        self._valid_until = 0.0
        self._user_id: str | None = None
        self._expires_at: float | None = None
        self._probe: tuple[float, AuthStatus] | None = None
        self._login_failure: tuple[float, BrainError] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BrainClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # Authentication

    async def authenticate(self, *, force: bool = False) -> AuthStatus:
        async with self._lock:
            self._probe = None
            if force:
                self.store.clear_pending()
                self._login_failure = None
            elif await self._restore_and_probe() and _monotonic() < self._valid_until:
                return self._authenticated_status()
            try:
                await self._login()
            except BiometricRequired as exc:
                return self._biometric_status(exc.url)
            return self._authenticated_status()

    async def complete_authentication(self) -> AuthStatus:
        async with self._lock:
            self._probe = None
            pending = self.store.load_pending()
            if pending is None:
                raise AuthError(
                    "No biometric check is pending (they expire after 30 minutes).",
                    hint="Call authenticate first.",
                )
            url = self._own_host_url(pending.inquiry_url)
            credentials = self.settings.require_credentials()
            self._load_cookies(pending.cookies)
            response = await self._auth_call(
                "POST", url, auth=httpx2.BasicAuth(credentials.email, credentials.password)
            )
            if response.status_code >= 500:
                raise ServerError("BRAIN failed while finishing the biometric check.")
            if response.status_code == 429:
                raise RateLimited(
                    "BRAIN is throttling biometric completion.",
                    retry_after=parse_retry_after(response),
                    status=429,
                )
            if response.status_code in (403, 404):
                self.store.clear_pending()
                raise AuthError(
                    "BRAIN no longer recognizes this biometric check.",
                    status=response.status_code,
                    hint="Call authenticate to start a new one.",
                )
            if response.status_code != 201:
                return self._biometric_status(
                    url,
                    "BRAIN has not accepted the check yet. Finish it in the browser, then call "
                    "complete_authentication again.",
                )
            body = parse_json(response)
            # Saved before anything else can fail, so a finished check is never thrown away.
            self._persist()
            self.store.clear_pending()
            self._restored = True
            if isinstance(body, dict) and body.get("user"):
                self._adopt_login(body)
            elif await self._check_session():
                self._persist()
                self._mark_logged_in()
            else:
                self._http.cookies.clear()
                try:
                    await self._login()
                except BiometricRequired as exc:
                    return self._biometric_status(exc.url)
            return self._authenticated_status()

    async def auth_status(self) -> AuthStatus:
        """Report the session state without ever sending the password."""
        if self._probe and _monotonic() - self._probe[0] < PROBE_CACHE_SECONDS:
            return self._probe[1]
        async with self._lock:
            status = await self._current_status()
            self._probe = (_monotonic(), status)
            return status

    async def _current_status(self) -> AuthStatus:
        if await self._restore_and_probe():
            return self._authenticated_status()
        pending = self.store.load_pending()
        if pending:
            return self._biometric_status(pending.inquiry_url)
        configured = self._credentials_configured()
        return AuthStatus(
            state="unauthenticated",
            credentials_configured=configured,
            next_step="Call authenticate."
            if configured
            else "Run `mcp-server-worldquantbrain auth` in a terminal to store credentials.",
        )

    def _authenticated_status(self) -> AuthStatus:
        expires_in = None
        if self._expires_at is not None:
            expires_in = max(0, int(self._expires_at - _monotonic()))
        return AuthStatus(
            state="authenticated",
            user_id=self._user_id,
            expires_in_seconds=expires_in,
            credentials_configured=self._credentials_configured(),
        )

    def _credentials_configured(self) -> bool:
        try:
            return self.settings.credentials() is not None
        except ConfigError:
            return False

    def _biometric_status(self, url: str, next_step: str | None = None) -> AuthStatus:
        return AuthStatus(
            state="biometric_required",
            biometric_url=url,
            credentials_configured=self._credentials_configured(),
            next_step=next_step
            or "Ask the user to open biometric_url in a browser and finish the check, then call "
            "complete_authentication.",
        )

    def _dump_cookies(self) -> list[dict[str, str]]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self._http.cookies.jar
        ]

    def _load_cookies(self, cookies: list[dict[str, str]]) -> None:
        self._http.cookies.clear()
        for cookie in cookies:
            self._http.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )

    def _own_host_url(self, url: str) -> str:
        # The completion POST carries the password, so it may only go to the configured API host,
        # and over the configured scheme even if BRAIN's Location says http.
        target = urlsplit(url)
        base = urlsplit(self.settings.base_url)
        if target.hostname != base.hostname:
            self.store.clear_pending()
            raise AuthError(f"Refusing to send credentials to unexpected host in {url}.")
        return urlunsplit((base.scheme, base.netloc, target.path, target.query, ""))

    def _restore(self) -> bool:
        session = self.store.load()
        if session is None or session.base_url != self.settings.base_url or not session.cookies:
            return False
        self._load_cookies(session.cookies)
        self._user_id = session.user_id
        self._file_mtime = self.store.mtime()
        self._generation += 1
        return True

    async def _restore_and_probe(self) -> bool:
        if not self._restored:
            self._restored = True
            if not self._restore():
                return False
        elif not len(self._http.cookies.jar) and not self._restore():
            return False
        if await self._check_session():
            return True
        # Another process, such as `mcp-server-worldquantbrain auth`, may have logged in meanwhile.
        if self.store.mtime() != self._file_mtime and self._restore():
            return await self._check_session()
        return False

    async def _auth_call(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        """Send a login-related request, keeping failures inside the BrainError hierarchy."""
        idempotent = method == "GET"
        for attempt in range(RETRY_SAFE.network_attempts):
            last_attempt = attempt == RETRY_SAFE.network_attempts - 1
            try:
                response = await self._http.request(method, url, **kwargs)
            except (httpx2.TransportError, httpx2.DecodingError) as exc:
                retryable = idempotent or isinstance(
                    exc, httpx2.ConnectError | httpx2.ConnectTimeout
                )
                if last_attempt or not retryable:
                    raise ServerError(
                        f"{method} {urlsplit(url).path} failed: {type(exc).__name__}.",
                        hint="Retry later.",
                    ) from exc
                await _sleep(backoff(attempt))
                continue
            if idempotent and response.status_code in _TRANSIENT_STATUS and not last_attempt:
                await _sleep(clamp(parse_retry_after(response) or backoff(attempt), 1, 30))
                continue
            return response
        raise AssertionError("unreachable")

    async def _check_session(self) -> bool:
        response = await self._auth_call("GET", "/authentication")
        if response.status_code in (401, 204):
            return False
        if response.status_code == 429:
            raise RateLimited(
                "BRAIN is rate limiting session checks.",
                retry_after=parse_retry_after(response),
                status=429,
            )
        if response.status_code != 200:
            raise ServerError(f"GET /authentication returned {response.status_code}.")
        body = parse_json(response)
        if not isinstance(body, dict) or not (body.get("user") or body.get("token")):
            return False
        self._adopt_token(body, persist=False)
        return True

    def _adopt_token(self, body: dict[str, Any], *, persist: bool) -> None:
        user = body.get("user")
        if isinstance(user, dict) and user.get("id"):
            self._user_id = str(user["id"])
        token = body.get("token")
        expiry = token.get("expiry") if isinstance(token, dict) else None
        now = _monotonic()
        if isinstance(expiry, int | float):
            self._expires_at = now + float(expiry)
            self._valid_until = self._expires_at - EXPIRY_MARGIN
        else:
            self._expires_at = None
            self._valid_until = now + UNKNOWN_EXPIRY_RECHECK
        if persist:
            self._persist()

    def _adopt_login(self, body: dict[str, Any]) -> None:
        self._adopt_token(body, persist=True)
        self._mark_logged_in()

    def _mark_logged_in(self) -> None:
        self._generation += 1
        self._login_failure = None
        self._probe = None

    def _persist(self) -> None:
        self.store.save(
            SessionData(
                base_url=self.settings.base_url,
                cookies=self._dump_cookies(),
                user_id=self._user_id,
            )
        )
        self._file_mtime = self.store.mtime()

    async def _login(self) -> None:
        pending = self.store.load_pending()
        if pending:
            # Starting another login would only open a second inquiry for the same user.
            raise BiometricRequired(pending.inquiry_url)
        if self._login_failure and _monotonic() < self._login_failure[0]:
            raise self._login_failure[1]
        credentials = self.settings.require_credentials()
        self._http.cookies.clear()
        response = await self._auth_call(
            "POST",
            "/authentication",
            auth=httpx2.BasicAuth(credentials.email, credentials.password),
        )
        body = parse_json(response)
        if response.status_code == 401:
            if "persona" in response.headers.get("www-authenticate", "").lower():
                location = response.headers.get("location")
                if not location:
                    raise AuthError("BRAIN asked for a biometric check but sent no URL.")
                url = self._own_host_url(urljoin(str(response.url), location))
                self.store.save_pending(url, self._dump_cookies())
                raise BiometricRequired(url)
            error = AuthError(
                "BRAIN rejected the email or password.",
                status=401,
                hint="Check the configured credentials, then call authenticate.",
            )
            self._login_failure = (_monotonic() + LOGIN_FAILURE_COOLDOWN, error)
            raise error
        if response.status_code == 429:
            retry_after = parse_retry_after(response)
            throttled = RateLimited("BRAIN is rate limiting logins.", retry_after=retry_after)
            cooldown = retry_after or LOGIN_FAILURE_COOLDOWN
            self._login_failure = (_monotonic() + cooldown, throttled)
            raise throttled
        if 400 <= response.status_code < 500:
            refused = AuthError(
                f"BRAIN refused the login with status {response.status_code}.",
                status=response.status_code,
                hint="Log in on the BRAIN website to see why.",
            )
            self._login_failure = (_monotonic() + LOGIN_FAILURE_COOLDOWN, refused)
            raise refused
        if response.status_code != 201 or not isinstance(body, dict):
            raise ServerError(f"POST /authentication returned {response.status_code}.")
        if body.get("inquiry") and not body.get("user"):
            url = f"{self.settings.base_url}/authentication/persona?inquiry={body['inquiry']}"
            self.store.save_pending(url, self._dump_cookies())
            raise BiometricRequired(url)
        self._adopt_login(body)
        logger.info("Logged in to BRAIN")

    async def _ensure_session(self) -> None:
        if _monotonic() < self._valid_until:
            return
        async with self._lock:
            if _monotonic() < self._valid_until:
                return
            if not await self._restore_and_probe() or _monotonic() >= self._valid_until:
                await self._login()

    async def _reauthenticate(self, generation: int) -> None:
        async with self._lock:
            if self._generation != generation:
                return
            self._valid_until = 0.0
            self._probe = None
            mtime = self.store.mtime()
            if mtime != self._file_mtime and self._restore() and await self._check_session():
                return
            await self._login()

    # Requests

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Params = None,
        json_body: Any = None,
        long_poll: bool = False,
        deadline: float | None = None,
    ) -> httpx2.Response:
        method = method.upper()
        policy = RETRY_UNSAFE if method == "POST" else RETRY_SAFE
        await self._ensure_session()
        relogged = False
        rate_limited = server_failures = network_failures = 0

        async def pause(seconds: float) -> None:
            # A retry must not outlive the caller's wait, or the client cancels the whole call.
            if deadline is not None and _monotonic() + seconds > deadline:
                raise PollTimeout(url, elapsed=0.0)
            await _sleep(seconds)

        while True:
            generation = self._generation
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,  # type: ignore[arg-type]
                    json=json_body,
                )
            except (httpx2.TransportError, httpx2.DecodingError) as exc:
                reached_server = not isinstance(exc, httpx2.ConnectError | httpx2.ConnectTimeout)
                network_failures += 1
                if network_failures >= policy.network_attempts or (
                    reached_server and not policy.retry_ambiguous_timeouts
                ):
                    raise ServerError(
                        f"{method} {url} failed: {type(exc).__name__}.",
                        hint="Check the outcome before repeating this write."
                        if method == "POST"
                        else "Retry later.",
                    ) from exc
                await pause(backoff(network_failures))
                continue

            status = response.status_code
            retry_after = parse_retry_after(response)
            if status == 401:
                if relogged:
                    raise AuthError("BRAIN rejected the session right after a new login.")
                relogged = True
                await self._reauthenticate(generation)
                continue
            if status == 429:
                rate_limited += 1
                if rate_limited >= policy.rate_limit_attempts:
                    raise RateLimited(
                        f"BRAIN is throttling {method} {url}.",
                        retry_after=retry_after,
                        status=429,
                        detail=parse_json(response),
                    )
                wait = clamp(retry_after, 1, 120) if retry_after else backoff(rate_limited)
                await pause(wait)
                continue
            long_polling = status == 503 and long_poll and retry_after is not None
            if status in (500, 502, 503, 504) and not long_polling:
                server_failures += 1
                if server_failures >= policy.server_attempts:
                    raise ServerError(f"{method} {url} returned {status}.", status=status)
                wait = clamp(retry_after, 0.5, 30) if retry_after else backoff(server_failures)
                await pause(wait)
                continue
            if not long_polling:
                self._raise_for_status(method, url, response)
            return response

    @staticmethod
    def _raise_for_status(method: str, url: str, response: httpx2.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        body = parse_json(response)
        detail = body.get("detail") if isinstance(body, dict) else None
        text = response.text.lower()
        if status in (400, 409) and _CONCURRENCY_ERROR.search(text):
            raise RateLimited(
                f"BRAIN refused {method} {url}: {detail or response.text[:200]}",
                retry_after=30,
                status=status,
                detail=body,
            )
        if status == 403:
            raise PermissionDenied(
                f"BRAIN denied {method} {url}: {str(detail or 'forbidden').rstrip('.')}.",
                status=status,
                detail=detail,
                body=body,
                hint="This endpoint may not be available on the current account tier.",
            )
        if status == 404:
            raise NotFound(f"{url} does not exist on BRAIN.", status=status, detail=detail)
        if 400 <= status < 500:
            fields = body if isinstance(body, dict) else {}
            summary = "; ".join(f"{key}: {value}" for key, value in fields.items())
            raise BrainValidationError(
                f"BRAIN rejected {method} {url}: {summary or response.text[:300]}",
                status=status,
                detail=body,
                fields=fields,
            )
        raise ServerError(f"{method} {url} returned {status}.", status=status)

    @staticmethod
    def deadline_after(seconds: float) -> float:
        return _monotonic() + seconds

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        return parse_json(await self.request(method, url, **kwargs))

    async def poll(
        self,
        url: str,
        *,
        max_seconds: float,
        terminal: Terminal = default_terminal,
        min_interval: float = 1.0,
        max_interval: float | None = None,
        on_poll: OnPoll | None = None,
    ) -> httpx2.Response:
        """GET `url` until `terminal` accepts a response or `max_seconds` runs out."""
        ceiling = max_interval if max_interval is not None else self.settings.poll_max_interval
        started = _monotonic()
        deadline = started + max_seconds
        delay = min_interval
        last_body: Any = None
        while True:
            try:
                # Re-login sleeps and a slow GET must not outlive the wait either.
                async with asyncio.timeout(max(deadline - _monotonic(), 0) + POLL_GRACE_SECONDS):
                    response = await self.request("GET", url, long_poll=True, deadline=deadline)
            except (TimeoutError, PollTimeout):
                raise PollTimeout(
                    url, elapsed=_monotonic() - started, last_body=last_body
                ) from None
            except (ServerError, RateLimited):
                # The job is still running on BRAIN. A brief outage must not lose track of it.
                remaining = deadline - _monotonic()
                if remaining <= POLL_MIN_WAIT:
                    raise PollTimeout(
                        url, elapsed=_monotonic() - started, last_body=last_body
                    ) from None
                await _sleep(min(delay, remaining))
                delay = min(delay * 1.5, ceiling)
                continue
            if terminal(response):
                return response
            last_body = parse_json(response)
            elapsed = _monotonic() - started
            if on_poll:
                await on_poll(response, elapsed)
            remaining = max_seconds - elapsed
            if remaining <= POLL_MIN_WAIT:
                raise PollTimeout(url, elapsed=elapsed, last_body=last_body)
            retry_after = parse_retry_after(response)
            if retry_after is not None:
                wait = clamp(retry_after, min_interval, ceiling)
            else:
                wait = delay
                delay = min(delay * 1.5, ceiling)
            await _sleep(min(wait, remaining))
