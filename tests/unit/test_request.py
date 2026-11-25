from typing import Any, cast

import httpx2
import pytest

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.errors import (
    AuthError,
    BrainValidationError,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServerError,
)
from tests.conftest import FakeBrain

pytestmark = pytest.mark.anyio


def client_for(settings: Any, brain: FakeBrain) -> BrainClient:
    return BrainClient(settings, transport=brain.transport)


async def test_429_honours_retry_after_then_succeeds(settings: Any, brain: Any, clock: Any) -> None:
    brain.add("GET", "/x", (429, {"detail": "THROTTLED"}, {"retry-after": "7"}), (200, {"ok": 1}))
    async with client_for(settings, brain) as client:
        assert await client.request_json("GET", "/x") == {"ok": 1}
    assert 7 in clock.sleeps


async def test_retry_after_on_429_is_clamped(settings: Any, brain: Any, clock: Any) -> None:
    brain.add("GET", "/x", (429, None, {"retry-after": "9000"}), (200, {"ok": 1}))
    async with client_for(settings, brain) as client:
        await client.request_json("GET", "/x")
    assert clock.sleeps == [120]


async def test_429_gives_up_after_five_attempts(settings: Any, brain: Any) -> None:
    brain.add("GET", "/x", (429, {"detail": "THROTTLED"}, {"retry-after": "3"}))
    async with client_for(settings, brain) as client:
        with pytest.raises(RateLimited) as caught:
            await client.request_json("GET", "/x")
    assert caught.value.retry_after == 3
    assert len(brain.calls("GET", "/x")) == 5


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_server_errors_retry_three_times(settings: Any, brain: Any, status: Any) -> None:
    brain.add("GET", "/x", (status, None))
    async with client_for(settings, brain) as client:
        with pytest.raises(ServerError):
            await client.request_json("GET", "/x")
    assert len(brain.calls("GET", "/x")) == 3


async def test_503_without_retry_after_is_a_server_error(settings: Any, brain: Any) -> None:
    brain.add("GET", "/x", (503, None), (503, None), (200, {"ok": 1}))
    async with client_for(settings, brain) as client:
        assert await client.request_json("GET", "/x") == {"ok": 1}


async def test_post_is_not_repeated_after_a_server_error(settings: Any, brain: Any) -> None:
    brain.add("POST", "/simulations", (500, None))
    async with client_for(settings, brain) as client:
        with pytest.raises(ServerError):
            await client.request("POST", "/simulations", json_body={})
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_post_is_not_repeated_after_a_read_timeout(settings: Any, brain: Any) -> None:
    attempts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        raise httpx2.ReadTimeout("slow", request=request)

    settings_client = BrainClient(settings, transport=httpx2.MockTransport(handler))
    cast("Any", settings_client)._valid_until = 1e12
    with pytest.raises(ServerError):
        await settings_client.request("POST", "/simulations", json_body={})
    assert len(attempts) == 1
    await settings_client.aclose()


async def test_get_retries_network_errors(settings: Any) -> None:
    attempts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx2.ConnectError("down", request=request)
        return httpx2.Response(200, json={"ok": 1})

    client = BrainClient(settings, transport=httpx2.MockTransport(handler))
    cast("Any", client)._valid_until = 1e12
    assert await client.request_json("GET", "/x") == {"ok": 1}
    assert len(attempts) == 3
    await client.aclose()


async def test_400_lists_rejected_fields_without_retry(settings: Any, brain: Any) -> None:
    brain.add("GET", "/x", (400, {"region": ["Invalid choice."]}))
    async with client_for(settings, brain) as client:
        with pytest.raises(BrainValidationError) as caught:
            await client.request_json("GET", "/x")
    assert "region" in caught.value.fields
    assert len(brain.calls("GET", "/x")) == 1


async def test_403_keeps_the_body(settings: Any, brain: Any) -> None:
    brain.add("GET", "/x", (403, {"detail": "nope", "is": {}}))
    async with client_for(settings, brain) as client:
        with pytest.raises(PermissionDenied) as caught:
            await client.request_json("GET", "/x")
    assert caught.value.body == {"detail": "nope", "is": {}}


async def test_404_is_not_found(settings: Any, brain: Any) -> None:
    async with client_for(settings, brain) as client:
        with pytest.raises(NotFound):
            await client.request_json("GET", "/missing")


async def test_concurrency_error_on_post_is_rate_limited(settings: Any, brain: Any) -> None:
    brain.add("POST", "/simulations", (400, {"detail": "CONCURRENT_SIMULATION_LIMIT_EXCEEDED"}))
    async with client_for(settings, brain) as client:
        with pytest.raises(RateLimited):
            await client.request("POST", "/simulations", json_body={})


async def test_validation_error_mentioning_limit_stays_a_validation_error(
    settings: Any, brain: Any
) -> None:
    brain.add("GET", "/x", (400, {"limit": ["Ensure this value is at most 50."]}))
    async with client_for(settings, brain) as client:
        with pytest.raises(BrainValidationError):
            await client.request_json("GET", "/x")


async def test_redirects_are_not_followed(settings: Any, brain: Any) -> None:
    brain.add("POST", "/alphas/a/submit", (303, None, {"location": "http://elsewhere:443/x"}))
    async with client_for(settings, brain) as client:
        response = await client.request("POST", "/alphas/a/submit")
    assert response.status_code == 303
    assert len(brain.requests) == 2


async def test_second_401_after_relogin_is_an_auth_error(settings: Any, brain: Any) -> None:
    brain.add("GET", "/x", (401, None))
    async with client_for(settings, brain) as client:
        with pytest.raises(AuthError):
            await client.request_json("GET", "/x")
    assert len(brain.calls("POST", "/authentication")) == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(302, None), (200, b""), (200, b"<html>maintenance</html>"), (200, b"null"), (204, None)],
)
async def test_json_endpoint_refuses_a_redirect_or_a_body_that_is_not_json(
    settings: Any, brain: Any, status: Any, body: Any
) -> None:
    brain.add("GET", "/x", (status, body, {"location": "https://elsewhere.example/x"}))
    async with client_for(settings, brain) as client:
        with pytest.raises(ServerError, match="without a JSON body"):
            await client.request_json("GET", "/x")
    assert len(brain.calls("GET", "/x")) == 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
@pytest.mark.parametrize(
    ("status", "body"), [(200, b""), (200, b"<html>saved</html>"), (204, None)]
)
async def test_write_that_succeeded_may_answer_without_json(
    settings: Any, brain: Any, method: str, status: Any, body: Any
) -> None:
    brain.add(method, "/x", (status, body))
    async with client_for(settings, brain) as client:
        assert await client.request_json(method, "/x") is None


async def test_redirect_after_a_write_is_an_error(settings: Any, brain: Any) -> None:
    brain.add("PATCH", "/x", (302, None, {"location": "https://elsewhere.example/x"}))
    async with client_for(settings, brain) as client:
        with pytest.raises(ServerError, match="302"):
            await client.request_json("PATCH", "/x")
