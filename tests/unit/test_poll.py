import pytest

from mcp_server_worldquantbrain.client.base import BrainClient
from mcp_server_worldquantbrain.client.simulations import simulation_terminal
from mcp_server_worldquantbrain.errors import PollTimeout
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

DONE = {"status": "COMPLETE", "alpha": "a1"}


async def run(settings, brain, **kwargs):
    async with BrainClient(settings, transport=brain.transport) as client:
        return await client.poll("/job", **kwargs)


async def test_waits_for_retry_after_then_returns(settings, brain, clock):
    brain.add("GET", "/job", (200, None, {"retry-after": "5.0"}), (200, DONE))
    response = await run(settings, brain, max_seconds=60)
    assert response.json() == DONE
    assert clock.sleeps == [5.0]


async def test_503_with_retry_after_keeps_polling(settings, brain, clock):
    brain.add("GET", "/job", (503, None, {"retry-after": "2"}), (200, DONE))
    await run(settings, brain, max_seconds=60)
    assert clock.sleeps == [2.0]


async def test_empty_200_keeps_polling(settings, brain):
    brain.add("GET", "/job", (200, ""), (200, DONE))
    assert (await run(settings, brain, max_seconds=60)).json() == DONE


async def test_202_keeps_polling_with_growing_delay(settings, brain, clock):
    brain.add("GET", "/job", (202, None), (202, None), (200, DONE))
    await run(settings, brain, max_seconds=60)
    assert clock.sleeps == [1.0, 1.5]


async def test_303_keeps_polling(settings, brain):
    brain.add("GET", "/job", (303, None, {"location": "http://x:443/y"}), (200, DONE))
    assert (await run(settings, brain, max_seconds=60)).json() == DONE


async def test_rounding_residue_of_the_deadline_ends_the_wait(settings, brain, clock):
    # 1000 + this value rounds down, so the fake clock stops a few 1e-14 s short of the deadline
    # and can never reach it. A fourth GET would mean the residue was treated as time left.
    pending = (200, "", {"retry-after": "30"})
    brain.add("GET", "/job", pending, pending, pending, (200, DONE))
    with pytest.raises(PollTimeout):
        await run(settings, brain, max_seconds=44.123456789012)
    assert len(brain.calls("GET", "/job")) == 3


async def test_deadline_raises_with_last_body(settings, brain):
    brain.add("GET", "/job", (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    with pytest.raises(PollTimeout) as caught:
        await run(settings, brain, max_seconds=12)
    assert caught.value.last_body == {"status": "SIMULATING"}
    assert caught.value.location == "/job"


async def test_zero_wait_checks_exactly_once(settings, brain):
    brain.add("GET", "/job", (200, {"x": 1}, {"retry-after": "5"}))
    with pytest.raises(PollTimeout):
        await run(settings, brain, max_seconds=0)
    assert len(brain.calls("GET", "/job")) == 1


async def test_503_with_zero_retry_after_keeps_polling(settings, brain):
    brain.add("GET", "/job", (503, None, {"retry-after": "0"}), (200, DONE))
    assert (await run(settings, brain, max_seconds=60)).json() == DONE


async def test_transient_errors_do_not_abort_the_wait(settings, brain):
    brain.add("GET", "/job", (500, None), (500, None), (500, None), (500, None), (200, DONE))
    assert (await run(settings, brain, max_seconds=600)).json() == DONE


async def test_persistent_errors_end_in_a_timeout(settings, brain):
    brain.add("GET", "/job", (500, None))
    with pytest.raises(PollTimeout):
        await run(settings, brain, max_seconds=30)


async def test_rate_limit_sleeps_do_not_outlive_the_wait(settings, brain, clock):
    brain.add("GET", "/job", (429, None, {"retry-after": "100"}))
    with pytest.raises(PollTimeout):
        await run(settings, brain, max_seconds=45)
    assert sum(clock.sleeps) <= 45


async def test_non_json_success_keeps_polling(settings, brain):
    brain.add("GET", "/job", (200, "<html>gateway</html>"), (200, DONE))
    assert (await run(settings, brain, max_seconds=60)).json() == DONE


async def test_list_body_is_not_a_finished_simulation(settings, brain):
    brain.add("GET", "/job", (200, [1, 2]), (200, DONE))
    response = await run(settings, brain, max_seconds=60, terminal=simulation_terminal)
    assert response.json() == DONE


async def test_retry_after_is_capped_by_poll_ceiling(settings, brain, clock):
    brain.add("GET", "/job", (200, None, {"retry-after": "500"}), (200, DONE))
    await run(settings, brain, max_seconds=600, max_interval=30)
    assert clock.sleeps == [30]


async def test_simulation_terminal_treats_running_without_header_as_running(settings, brain):
    brain.add("GET", "/job", (200, {"status": "SIMULATING", "progress": 0.4}), (200, DONE))
    response = await run(settings, brain, max_seconds=60, terminal=simulation_terminal)
    assert response.json() == DONE


async def test_simulation_terminal_accepts_warning_and_error(settings, brain):
    body = fixture("simulation_complete") | {"status": "ERROR"}
    brain.add("GET", "/job", (200, body))
    response = await run(settings, brain, max_seconds=60, terminal=simulation_terminal)
    assert response.json()["status"] == "ERROR"
