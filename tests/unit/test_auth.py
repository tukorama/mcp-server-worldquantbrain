import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.errors import (
    AuthError,
    BiometricRequired,
    ConfigError,
    RateLimited,
    ServerError,
)
from tests.conftest import LOGIN_BODY, FakeBrain

pytestmark = pytest.mark.anyio

PERSONA = {"www-authenticate": "persona", "location": "/authentication/persona?inquiry=inq_1"}
PERSONA_URL = "https://api.worldquantbrain.com/authentication/persona?inquiry=inq_1"


def persona_brain(brain: FakeBrain) -> FakeBrain:
    brain.add("POST", "/authentication", (401, None, PERSONA))
    brain.add("GET", "/authentication", (401, None))
    return brain


async def test_login_persists_a_private_session(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.authenticate()
    assert status.state == "authenticated"
    assert status.user_id == "AB1234"
    path = settings.resolved_session_file
    saved = json.loads(path.read_text())
    assert saved["cookies"][0]["name"] == "t"
    assert "hunter2" not in path.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_restart_reuses_the_session_without_sending_the_password(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
    fresh = FakeBrain()
    async with BrainClient(settings, transport=fresh.transport) as client:
        status = await client.authenticate()
    assert status.state == "authenticated"
    assert fresh.calls("POST", "/authentication") == []
    assert "t=jwt-1" in fresh.calls("GET", "/authentication")[0].headers["cookie"]


async def test_401_triggers_exactly_one_login_for_concurrent_requests(settings):
    logins = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        if request.url.path == "/authentication" and request.method == "POST":
            logins.append(request)
            cookie = f"t=jwt-{len(logins)}; Path=/"
            return httpx2.Response(201, json=LOGIN_BODY, headers={"set-cookie": cookie})
        if request.url.path == "/authentication":
            return httpx2.Response(200, json=LOGIN_BODY)
        if f"t=jwt-{len(logins)}" in request.headers.get("cookie", "") and len(logins) > 1:
            return httpx2.Response(200, json={"ok": 1})
        return httpx2.Response(401)

    transport = httpx2.MockTransport(handler)
    async with BrainClient(settings, transport=transport) as client:
        await client.authenticate()
        results = await asyncio.gather(*(client.request_json("GET", "/x") for _ in range(5)))
    assert results == [{"ok": 1}] * 5
    assert len(logins) == 2


async def test_session_refreshed_by_another_process_is_adopted(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        brain.add("GET", "/x", (401, None), (200, {"ok": 1}))
        saved = json.loads(settings.resolved_session_file.read_text())
        saved["cookies"][0]["value"] = "jwt-from-cli"
        settings.resolved_session_file.write_text(json.dumps(saved))
        os.utime(settings.resolved_session_file, (1, 1))
        before = len(brain.calls("POST", "/authentication"))
        assert await client.request_json("GET", "/x") == {"ok": 1}
    assert len(brain.calls("POST", "/authentication")) == before


async def test_session_close_to_expiry_logs_in_again(settings, brain):
    brain.add("GET", "/authentication", (200, {"user": {"id": "A"}, "token": {"expiry": 30}}))
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate(force=True)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
    assert len(brain.calls("POST", "/authentication")) == 2


async def test_bad_password_is_not_retried_during_the_cooldown(settings, brain):
    brain.add("POST", "/authentication", (401, None))
    async with BrainClient(settings, transport=brain.transport) as client:
        with pytest.raises(AuthError):
            await client.authenticate(force=True)
        with pytest.raises(AuthError):
            await client.request_json("GET", "/x")
    assert len(brain.calls("POST", "/authentication")) == 1


async def test_throttled_login_is_not_repeated_during_the_cooldown(settings, brain):
    brain.add("POST", "/authentication", (429, None, {"retry-after": "600"}))
    async with BrainClient(settings, transport=brain.transport) as client:
        for _ in range(3):
            with pytest.raises(RateLimited):
                await client.request_json("GET", "/x")
    assert len(brain.calls("POST", "/authentication")) == 1


async def test_locked_account_surfaces_as_an_auth_error(settings, brain):
    brain.add("POST", "/authentication", (403, None))
    async with BrainClient(settings, transport=brain.transport) as client:
        with pytest.raises(AuthError):
            await client.request_json("GET", "/x")


async def test_finished_biometric_check_is_saved_before_a_failing_probe(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        brain.add("POST", "/authentication/persona", (201, None, {"set-cookie": "t=bio; Path=/"}))
        brain.add("GET", "/authentication", (503, None))
        with pytest.raises(ServerError):
            await client.complete_authentication()
    assert "bio" in settings.resolved_session_file.read_text()


@pytest.mark.parametrize("status", [403, 404])
async def test_dead_inquiry_is_cleared(settings, brain, status):
    persona_brain(brain)
    brain.add("POST", "/authentication/persona", (status, None))
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        with pytest.raises(AuthError):
            await client.complete_authentication()
        assert client.store.load_pending() is None


@pytest.mark.parametrize(
    "content",
    [
        '{"version": 1}',
        '{"version": 1, "base_url": "x", "cookies": "no"}',
        '{"version": 1, "base_url": "x", "cookies": [{"name": 1}]}',
    ],
)
async def test_malformed_session_file_counts_as_no_session(settings, brain, content):
    settings.resolved_session_file.parent.mkdir(parents=True)
    settings.resolved_session_file.write_text(content)
    async with BrainClient(settings, transport=brain.transport) as client:
        assert (await client.auth_status()).state == "unauthenticated"


async def test_missing_credentials_say_how_to_fix_it(settings, brain):
    settings.email = None
    settings.password = None
    async with BrainClient(settings, transport=brain.transport) as client:
        with pytest.raises(ConfigError) as caught:
            await client.authenticate(force=True)
    assert "mcp-server-worldquantbrain auth" in caught.value.for_agent()


async def test_auth_status_never_posts_credentials(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.auth_status()
    assert status.state == "unauthenticated"
    assert status.credentials_configured is True
    assert brain.calls("POST", "/authentication") == []


async def test_auth_status_is_cached_for_a_minute(settings, brain, clock):
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        await client.auth_status()
        first = len(brain.calls("GET", "/authentication"))
        await client.auth_status()
        assert len(brain.calls("GET", "/authentication")) == first
        clock.now += 61
        await client.auth_status()
        assert len(brain.calls("GET", "/authentication")) == first + 1


async def test_persona_401_returns_url_and_writes_pending(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.authenticate()
    assert status.state == "biometric_required"
    assert status.biometric_url == PERSONA_URL
    assert client.store.load_pending().inquiry_url == status.biometric_url


async def test_pending_inquiry_blocks_a_second_login_attempt(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        with pytest.raises(BiometricRequired):
            await client.request_json("GET", "/x")
    assert len(brain.calls("POST", "/authentication")) == 1


async def test_force_discards_the_pending_inquiry(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        await client.authenticate(force=True)
    assert len(brain.calls("POST", "/authentication")) == 2


async def test_complete_authentication_survives_a_restart(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
    brain.add("POST", "/authentication/persona", (201, LOGIN_BODY, {"set-cookie": "t=jwt-2"}))
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.complete_authentication()
    assert status.state == "authenticated"
    assert client.store.load_pending() is None
    assert settings.resolved_session_file.exists()
    posted = brain.calls("POST", "/authentication/persona")[0]
    assert posted.headers["authorization"].startswith("Basic ")


async def test_incomplete_check_stays_pending(settings, brain):
    persona_brain(brain)
    brain.add("POST", "/authentication/persona", (401, None))
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        status = await client.complete_authentication()
    assert status.state == "biometric_required"
    assert client.store.load_pending() is not None


async def test_completion_with_empty_body_probes_and_saves_the_session(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        brain.add("POST", "/authentication/persona", (201, None, {"set-cookie": "t=bio; Path=/"}))
        brain.add("GET", "/authentication", (200, LOGIN_BODY))
        status = await client.complete_authentication()
    assert status.state == "authenticated"
    assert "t" in {
        c["name"] for c in json.loads(settings.resolved_session_file.read_text())["cookies"]
    }
    fresh = FakeBrain()
    async with BrainClient(settings, transport=fresh.transport) as client:
        assert (await client.authenticate()).state == "authenticated"
    assert fresh.calls("POST", "/authentication") == []


async def test_only_201_completes_a_biometric_check(settings, brain):
    persona_brain(brain)
    brain.add("POST", "/authentication/persona", (200, None))
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        status = await client.complete_authentication()
    assert status.state == "biometric_required"
    assert client.store.load_pending() is not None


async def test_lapsed_token_adopts_a_newer_session_file_without_logging_in(settings, clock):
    logins = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            logins.append(request)
            return httpx2.Response(201, json=LOGIN_BODY, headers={"set-cookie": "t=old; Path=/"})
        if "t=fresh" in request.headers.get("cookie", "") or len(logins) == 1 and clock.now < 5000:
            return httpx2.Response(200, json=LOGIN_BODY)
        return httpx2.Response(401)

    async with BrainClient(settings, transport=httpx2.MockTransport(handler)) as client:
        await client.authenticate()
        saved = json.loads(settings.resolved_session_file.read_text())
        saved["cookies"][0]["value"] = "fresh"
        settings.resolved_session_file.write_text(json.dumps(saved))
        os.utime(settings.resolved_session_file, (1, 1))
        clock.now += 20000
        status = await client.auth_status()
    assert status.state == "authenticated"
    assert len(logins) == 1


async def test_empty_session_probe_means_logged_out(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
    brain.add("GET", "/authentication", (200, ""))
    async with BrainClient(settings, transport=brain.transport) as client:
        assert (await client.auth_status()).state == "unauthenticated"


async def test_session_close_to_expiry_still_reports_authenticated(settings, brain):
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
    brain.add("GET", "/authentication", (200, {"user": {"id": "A"}, "token": {"expiry": 30}}))
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.auth_status()
    assert (status.state, status.expires_in_seconds) == ("authenticated", 30)


async def test_network_failure_during_probe_stays_a_brain_error(settings):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    async with BrainClient(settings, transport=httpx2.MockTransport(handler)) as client:
        client._http.cookies.set("t", "x", domain="api.worldquantbrain.com")
        client._restored = True
        with pytest.raises(ServerError):
            await client.auth_status()


async def test_url_shown_for_an_http_location_opens_in_a_browser(settings, brain):
    location = "http://api.worldquantbrain.com:443/authentication/persona?inquiry=x"
    brain.add("POST", "/authentication", (401, None, PERSONA | {"location": location}))
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.authenticate()
    assert status.biometric_url == PERSONA_URL.replace("inq_1", "x")


async def test_http_inquiry_location_is_upgraded_before_sending_credentials(settings, brain):
    brain.add(
        "POST",
        "/authentication",
        (
            401,
            None,
            PERSONA
            | {"location": "http://api.worldquantbrain.com:443/authentication/persona?inquiry=x"},
        ),
    )
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        brain.add("POST", "/authentication/persona", (201, LOGIN_BODY))
        await client.complete_authentication()
    posted = brain.calls("POST", "/authentication/persona")[0]
    assert posted.url.scheme == "https"
    assert posted.url.query == b"inquiry=x"


async def test_inquiry_201_variant(settings, brain):
    brain.add("POST", "/authentication", (201, {"inquiry": "inq_9"}))
    async with BrainClient(settings, transport=brain.transport) as client:
        status = await client.authenticate(force=True)
    assert status.state == "biometric_required"
    assert status.biometric_url.endswith("/authentication/persona?inquiry=inq_9")
    assert client.store.load_pending().inquiry_url == status.biometric_url


async def test_expired_pending_is_ignored(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        path = client.store.pending_path
        data = json.loads(path.read_text())
        data["created_at"] = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
        path.write_text(json.dumps(data))
        with pytest.raises(AuthError):
            await client.complete_authentication()


async def test_tampered_pending_file_cannot_redirect_credentials(settings, brain):
    persona_brain(brain)
    async with BrainClient(settings, transport=brain.transport) as client:
        await client.authenticate()
        path = client.store.pending_path
        data = json.loads(path.read_text())
        data["inquiry_url"] = "https://evil.test/steal"
        path.write_text(json.dumps(data))
        with pytest.raises(AuthError):
            await client.complete_authentication()
    assert not any(r.url.host == "evil.test" for r in brain.requests)


async def test_credentials_are_never_sent_to_a_foreign_inquiry_host(settings, brain):
    brain.add(
        "POST",
        "/authentication",
        (401, None, {"www-authenticate": "persona", "location": "https://evil.test/steal"}),
    )
    async with BrainClient(settings, transport=brain.transport) as client:
        with pytest.raises(AuthError):
            await client.authenticate()
        assert client.store.load_pending() is None
    assert not any(r.url.host == "evil.test" for r in brain.requests)
