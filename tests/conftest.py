import json
from collections import deque
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mcp_server_worldquantbrain.client import base
from mcp_server_worldquantbrain.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"

LOGIN_BODY = {"user": {"id": "AB1234"}, "token": {"expiry": 14400.0}, "permissions": ["TUTORIAL"]}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeBrain:
    """Scripted BRAIN API. The last response of a route repeats."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], deque[tuple[int, Any, dict[str, str]]]] = {}
        self.requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self._handle)
        self.add("POST", "/authentication", (201, LOGIN_BODY, {"set-cookie": "t=jwt-1; Path=/"}))
        self.add("GET", "/authentication", (200, LOGIN_BODY))

    def add(self, method: str, path: str, *responses: Any) -> None:
        normalized = []
        for response in responses:
            status, body, *rest = response if isinstance(response, tuple) else (200, response)
            normalized.append((status, body, rest[0] if rest else {}))
        self.routes[(method, path)] = deque(normalized)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx2.Response(404, json={"detail": "Not found."})
        status, body, headers = queue.popleft() if len(queue) > 1 else queue[0]
        if isinstance(body, str | bytes):
            return httpx2.Response(status, content=body, headers=headers)
        if body is None:
            return httpx2.Response(status, headers=headers)
        return httpx2.Response(status, json=body, headers=headers)

    def calls(self, method: str, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(base, "_sleep", fake.sleep)
    monkeypatch.setattr(base, "_monotonic", fake.monotonic)
    return fake


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Keep a developer's real credentials file out of the tests.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    return Settings(
        _env_file=None,
        email="user@example.com",
        password="hunter2-secret",
        session_file=tmp_path / "config" / "session.json",
        cache_dir=tmp_path / "cache",
        credentials_file=None,
    )
