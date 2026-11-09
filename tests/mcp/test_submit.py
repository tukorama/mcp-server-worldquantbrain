import pytest
from mcp import Client
from mcp.types import ElicitResult

from mcp_server_worldquantbrain.server import create_server
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

SUBMIT = "/alphas/a/submit"
UNSUBMITTED = fixture("alpha_detail") | {"id": "a"}
ACTIVE = UNSUBMITTED | {"status": "ACTIVE"}


@pytest.fixture
def enabled(settings):
    settings.allow_submit = True
    settings.submit_confirm = "none"
    return settings


def healthy(brain):
    brain.add("GET", "/alphas/a", UNSUBMITTED)
    brain.add("GET", "/alphas/a/check", fixture("check_passing"))
    brain.add("GET", "/alphas/a/correlations/self", fixture("correlation_self"))


async def submit(settings, brain, **client_kwargs):
    server = create_server(settings, transport=brain.transport)
    async with Client(server, **client_kwargs) as session:
        result = await session.call_tool("submit_alpha", {"alpha_id": "a"})
    assert not result.is_error, result.content
    return result.structured_content


async def test_tool_is_marked_destructive_when_enabled(enabled, brain):
    async with Client(create_server(enabled, transport=brain.transport)) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert tools["submit_alpha"].annotations.destructive_hint is True


async def test_failed_check_refuses_without_posting(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/check", fixture("check_failing"))
    data = await submit(enabled, brain)
    assert data["outcome"] == "refused"
    assert "LOW_SHARPE" in data["message"]
    assert brain.calls("POST", SUBMIT) == []


async def test_pending_check_refuses_without_posting(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/check", (200, "", {"retry-after": "30"}))
    assert (await submit(enabled, brain))["outcome"] == "refused"
    assert brain.calls("POST", SUBMIT) == []


async def test_high_self_correlation_refuses_without_posting(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/correlations/self", fixture("correlation_self") | {"max": 0.91})
    data = await submit(enabled, brain)
    assert data["outcome"] == "refused"
    assert "0.910" in data["message"]
    assert brain.calls("POST", SUBMIT) == []


async def test_correlation_gate_can_be_disabled(enabled, brain):
    enabled.submit_correlation_gate = 0
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(enabled, brain))["outcome"] == "submitted"
    assert brain.calls("GET", "/alphas/a/correlations/self") == []


async def test_already_submitted_alpha_is_refused(enabled, brain):
    brain.add("GET", "/alphas/a", ACTIVE)
    assert (await submit(enabled, brain))["outcome"] == "refused"
    assert brain.calls("POST", SUBMIT) == []


async def test_303_then_active_is_submitted(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    redirect = {"location": "http://api.worldquantbrain.com:443/alphas/a/submit"}
    brain.add("POST", SUBMIT, (303, None, redirect))
    brain.add("GET", SUBMIT, (303, None, redirect), (200, "", {"retry-after": "1"}), (200, ""))
    assert (await submit(enabled, brain))["outcome"] == "submitted"
    assert len(brain.calls("POST", SUBMIT)) == 1


async def test_403_with_checks_is_rejected(enabled, brain):
    healthy(brain)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (403, fixture("submit_403_corr_fail")))
    data = await submit(enabled, brain)
    assert data["outcome"] == "rejected"
    assert "SELF_CORRELATION" in data["message"]


async def test_no_verdict_is_pending_and_warns_against_resubmitting(enabled, brain):
    healthy(brain)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, "", {"retry-after": "30"}))
    data = await submit(enabled, brain)
    assert data["outcome"] == "pending"
    assert "Do not submit again" in data["next_step"]
    assert len(brain.calls("POST", SUBMIT)) == 1


async def test_outage_after_the_post_is_pending_not_an_error(enabled, brain):
    healthy(brain)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (500, None))
    data = await submit(enabled, brain)
    assert data["outcome"] == "pending"
    assert len(brain.calls("POST", SUBMIT)) == 1


async def test_failing_final_status_read_is_pending(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, (404, {"detail": "x"}))
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(enabled, brain))["outcome"] == "pending"


async def test_error_check_in_a_2xx_body_is_not_a_rejection(enabled, brain):
    healthy(brain)
    brain.add("POST", SUBMIT, (200, None))
    body = {"is": {"checks": [{"name": "SELF_CORRELATION", "result": "ERROR"}]}}
    brain.add("GET", SUBMIT, (200, body))
    assert (await submit(enabled, brain))["outcome"] == "pending"


async def test_forbidden_final_read_is_not_called_a_rejection(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, (403, {"detail": "x"}))
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(enabled, brain))["outcome"] == "pending"


async def test_garbage_after_the_post_still_warns_against_resubmitting(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, (200, "<html>gateway</html>"))
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    data = await submit(enabled, brain)
    assert data["outcome"] == "pending"
    assert "Do not submit again" in data["next_step"]


async def test_null_correlation_records_fail_closed(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/correlations/self", {"records": None})
    assert (await submit(enabled, brain))["outcome"] == "refused"


async def test_pending_self_correlation_check_goes_to_the_correlation_gate(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    checks = fixture("check_passing")
    checks["is"]["checks"].append({"name": "SELF_CORRELATION", "result": "PENDING"})
    brain.add("GET", "/alphas/a/check", checks)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(enabled, brain))["outcome"] == "submitted"
    assert len(brain.calls("GET", "/alphas/a/correlations/self")) == 1


async def test_correlation_403_fails_closed(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/correlations/self", (403, {"detail": "no"}))
    data = await submit(enabled, brain)
    assert data["outcome"] == "refused"
    assert brain.calls("POST", SUBMIT) == []


async def test_correlation_without_a_number_fails_closed(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a/correlations/self", {"unexpected": True})
    assert (await submit(enabled, brain))["outcome"] == "refused"
    assert brain.calls("POST", SUBMIT) == []


async def test_empty_correlation_list_passes(enabled, brain):
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    brain.add("GET", "/alphas/a/correlations/self", {"schema": {}, "records": [], "max": None})
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(enabled, brain))["outcome"] == "submitted"


async def test_alpha_with_unknown_status_is_refused(enabled, brain):
    brain.add("GET", "/alphas/a", {"id": "a"})
    assert (await submit(enabled, brain))["outcome"] == "refused"
    assert brain.calls("POST", SUBMIT) == []


async def test_submit_post_is_never_retried_on_server_error(enabled, brain):
    healthy(brain)
    brain.add("POST", SUBMIT, (500, None))
    server = create_server(enabled, transport=brain.transport)
    async with Client(server) as session:
        result = await session.call_tool("submit_alpha", {"alpha_id": "a"})
    assert result.is_error
    assert len(brain.calls("POST", SUBMIT)) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_declined_confirmation_refuses_without_contacting_brain(settings, brain, mode):
    settings.allow_submit = True

    async def decline(_context, _params):
        return ElicitResult(action="decline")

    data = await submit(settings, brain, elicitation_callback=decline, mode=mode)
    assert data["outcome"] == "refused"
    assert brain.calls("GET", "/alphas/a") == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accepted_confirmation_proceeds(settings, brain, mode):
    settings.allow_submit = True
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    asked = []

    async def accept(_context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": True})

    data = await submit(settings, brain, elicitation_callback=accept, mode=mode)
    assert data["outcome"] == "submitted"
    assert "cannot be undone" in asked[0]


async def test_client_without_elicitation_relies_on_the_flag(settings, brain):
    settings.allow_submit = True
    healthy(brain)
    brain.add("GET", "/alphas/a", UNSUBMITTED, ACTIVE)
    brain.add("POST", SUBMIT, (200, None))
    brain.add("GET", SUBMIT, (200, {}))
    assert (await submit(settings, brain))["outcome"] == "submitted"
