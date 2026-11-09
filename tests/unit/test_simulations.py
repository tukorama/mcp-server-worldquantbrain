import asyncio
import json

import httpx2
import pytest

from mcp_server_worldquantbrain.client.ledger import fingerprint
from mcp_server_worldquantbrain.models.simulation import simulation_payload
from mcp_server_worldquantbrain.ops import Call, Runtime
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

LOCATION = {"location": "https://api.worldquantbrain.com/simulations/sim1"}


def scripted(brain, *polls):
    brain.add("POST", "/simulations", (201, None, LOCATION))
    brain.add("GET", "/simulations/sim1", *polls)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))


@pytest.fixture
async def runtime(settings, brain):
    instance = Runtime.create(settings, transport=brain.transport)
    yield instance
    await instance.aclose()


async def test_blocking_simulation_returns_the_alpha(runtime, settings, brain):
    scripted(
        brain,
        (200, {"status": "SIMULATING", "progress": 0.5}, {"retry-after": "3"}),
        (200, fixture("simulation_complete")),
    )
    ticks = []

    async def progress(fraction, message):
        ticks.append(fraction)

    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=600, progress=progress
    )
    assert result.status == "COMPLETE"
    assert result.alpha.in_sample.sharpe == 2.25
    assert ticks == [0.5]


async def test_timeout_returns_a_resumable_id(runtime, settings, brain):
    scripted(brain, (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=12
    )
    assert result.status == "PENDING"
    assert result.simulation_id == "sim1"
    assert "get_simulation_status" in result.next_step


async def test_non_blocking_returns_immediately(runtime, settings, brain):
    scripted(brain, (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=False, timeout=600
    )
    assert result.status == "PENDING"
    assert brain.calls("GET", "/simulations/sim1") == []


async def test_failure_carries_brains_message(runtime, settings, brain):
    scripted(brain, (200, {"status": "ERROR", "message": "Unknown variable foo"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "foo"), wait=True, timeout=60
    )
    assert result.status == "ERROR"
    assert result.message == "Unknown variable foo"
    assert result.alpha is None


async def test_warning_with_alpha_counts_as_success(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete") | {"status": "WARNING"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=60
    )
    assert result.alpha is not None


async def test_repeat_is_served_from_the_ledger_without_a_post(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    again = await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert again.from_ledger
    assert again.alpha is not None
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_force_runs_again(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    await runtime.simulations.simulate(payload, wait=True, timeout=60, force=True)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_failed_simulation_is_not_replayed_from_the_ledger(runtime, settings, brain):
    scripted(brain, (200, {"status": "ERROR", "message": "transient"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_running_simulation_is_resumed_not_restarted(runtime, settings, brain):
    scripted(
        brain,
        (200, {"status": "SIMULATING"}, {"retry-after": "5"}),
        (200, {"status": "SIMULATING"}, {"retry-after": "5"}),
        (200, fixture("simulation_complete")),
    )
    payload = simulation_payload(settings, "close")
    first = await runtime.simulations.simulate(payload, wait=True, timeout=1)
    assert first.status == "PENDING"
    second = await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert second.status == "COMPLETE"
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_stale_ledger_entry_is_discarded(runtime, settings, brain):
    payload = simulation_payload(settings, "close")
    runtime.ledger.record(payload, "gone", "WAITING")
    scripted(brain, (200, fixture("simulation_complete")))
    result = await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert result.simulation_id == "sim1"


async def batch_routes(brain, count):
    children = [f"c{i}" for i in range(count)]
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": children})
    for index, child in enumerate(children):
        brain.add(
            "GET",
            f"/simulations/{child}",
            {"status": "ERROR", "message": "bad", "regular": f"e{index}"},
        )


async def test_batch_posts_an_array_and_expands_children(runtime, settings, brain):
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    assert result.batch_ids == ["parent"]
    assert [r.simulation_id for r in result.results] == ["c0", "c1"]
    assert isinstance(json.loads(brain.calls("POST", "/simulations")[0].content), list)


async def test_large_batches_are_split_into_tens(runtime, settings, brain):
    await batch_routes(brain, 10)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(25)]
    await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    sizes = [len(json.loads(r.content)) for r in brain.calls("POST", "/simulations")]
    assert sizes == [10, 10, 5]


async def test_batch_skips_expressions_already_in_the_ledger(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete")))
    first = simulation_payload(settings, "close")
    await runtime.simulations.simulate(first, wait=True, timeout=60)
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/sim2"}))
    brain.add("GET", "/simulations/sim2", {"status": "ERROR", "message": "x"})
    second = simulation_payload(settings, "open")
    result = await runtime.simulations.simulate_batch([first, second], wait=True, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 2
    assert result.results[0].from_ledger


async def test_batch_rate_limit_reports_what_did_not_start(runtime, settings, brain):
    brain.add("POST", "/simulations", (429, {"detail": "THROTTLED"}, {"retry-after": "1"}))
    payloads = [simulation_payload(settings, f"e{i}") for i in range(12)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    assert result.status == "PARTIAL"
    assert len(result.not_started) == 12


async def test_poll_failure_after_post_is_not_reported_as_not_started(runtime, settings, brain):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", (403, {"detail": "gone"}))
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    assert result.not_started == []
    assert result.results[0].simulation_id == "parent"
    assert result.status == "PENDING"


async def test_identical_concurrent_calls_post_once(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    results = await asyncio.gather(
        *(runtime.simulations.simulate(payload, wait=True, timeout=60) for _ in range(4))
    )
    assert len(brain.calls("POST", "/simulations")) == 1
    assert all(r.alpha is not None for r in results)


async def test_duplicates_inside_a_batch_post_once(runtime, settings, brain):
    await batch_routes(brain, 1)
    brain.add("GET", "/simulations/parent", {"status": "ERROR", "message": "x"})
    same = [simulation_payload(settings, "close"), simulation_payload(settings, " close ")]
    await runtime.simulations.simulate_batch(same, wait=True, timeout=60)
    assert isinstance(json.loads(brain.calls("POST", "/simulations")[0].content), dict)


async def test_same_expression_with_different_settings_is_matched_by_settings(
    runtime, settings, brain
):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c5"]})
    for child, decay, alpha in (("c0", 0, "a0"), ("c5", 5, "a5")):
        brain.add(
            "GET",
            f"/simulations/{child}",
            {
                "status": "COMPLETE",
                "alpha": alpha,
                "regular": "close",
                "settings": {"decay": decay},
            },
        )
        brain.add("GET", f"/alphas/{alpha}", {"id": alpha})
    first = simulation_payload(settings, "close", decay=0)
    second = simulation_payload(settings, "close", decay=5)
    await runtime.simulations.simulate_batch([first, second], wait=True, timeout=60)
    assert runtime.ledger.get(fingerprint(first)).alpha_id == "a0"
    assert runtime.ledger.get(fingerprint(second)).alpha_id == "a5"


async def test_failed_children_are_not_replayed_forever(runtime, settings, brain):
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, timeout=60)
    await runtime.simulations.simulate_batch(payloads, wait=False, timeout=60)
    await runtime.simulations.simulate_batch(payloads, wait=False, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_batch_of_only_failures_is_failed(runtime, settings, brain):
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    assert result.status == "FAILED"


async def test_regions_are_sent_in_separate_groups(runtime, settings, brain):
    await batch_routes(brain, 2)
    payloads = [
        simulation_payload(settings, "a"),
        simulation_payload(settings, "b", region="EUR", universe="TOP2500"),
    ]
    await runtime.simulations.simulate_batch(payloads, wait=True, timeout=60)
    bodies = [json.loads(r.content) for r in brain.calls("POST", "/simulations")]
    assert all(isinstance(body, dict) for body in bodies)
    assert len(bodies) == 2


async def test_known_alpha_survives_a_purged_simulation(runtime, settings, brain):
    payload = simulation_payload(settings, "close")
    runtime.ledger.record(payload, "gone", "COMPLETE", "qMPjAxnO")
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    result = await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert result.from_ledger
    assert result.alpha is not None
    assert brain.calls("POST", "/simulations") == []


async def test_unusable_cache_directory_does_not_lose_the_simulation(settings, brain, tmp_path):
    (tmp_path / "file").write_text("")
    settings.cache_dir = tmp_path / "file" / "sub"
    runtime = Runtime.create(settings, transport=brain.transport)
    scripted(brain, (200, fixture("simulation_complete")))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=60, force=True
    )
    assert result.status == "COMPLETE"
    await runtime.aclose()


async def test_outage_during_a_wait_returns_a_resumable_id(runtime, settings, brain):
    scripted(brain, (500, None))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=30
    )
    assert (result.status, result.simulation_id) == ("PENDING", "sim1")


async def test_status_check_updates_the_ledger(runtime, settings, brain):
    scripted(brain, (200, {"status": "ERROR", "message": "bad"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=False, timeout=60)
    await runtime.simulations.status("sim1", wait_seconds=0)
    await runtime.simulations.simulate(payload, wait=False, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_cancelled_post_is_recorded_so_a_retry_joins_it(runtime, settings):
    release = asyncio.Event()
    posts = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/authentication":
            body = {"user": {"id": "A"}, "token": {"expiry": 9000}}
            return httpx2.Response(201 if request.method == "POST" else 200, json=body)
        if request.method == "POST":
            posts.append(request)
            await release.wait()
            return httpx2.Response(201, headers={"location": "https://x/simulations/sim1"})
        return httpx2.Response(200, json={"status": "SIMULATING"}, headers={"retry-after": "1"})

    instance = Runtime.create(settings, transport=httpx2.MockTransport(handler))
    payload = simulation_payload(settings, "close")
    first = asyncio.create_task(instance.simulations.simulate(payload, wait=False, timeout=60))
    while not posts:
        await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    retry = asyncio.create_task(instance.simulations.simulate(payload, wait=False, timeout=60))
    await asyncio.sleep(0.01)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await retry).simulation_id == "sim1"
    assert len(posts) == 1
    await instance.aclose()


async def test_replay_through_a_parent_does_not_guess_between_ambiguous_children(
    runtime, settings, brain
):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    for child, alpha in (("c0", "a0"), ("c1", "a1")):
        body = {"status": "COMPLETE", "alpha": alpha, "regular": "close", "settings": {}}
        brain.add("GET", f"/simulations/{child}", body)
        brain.add("GET", f"/alphas/{alpha}", {"id": alpha})
    on = simulation_payload(settings, "close", max_trade="ON")
    off = simulation_payload(settings, "close", max_trade="OFF")
    await runtime.simulations.simulate_batch([on, off], wait=True, timeout=60)
    await runtime.simulations.simulate(on, wait=True, timeout=60)
    assert runtime.ledger.get(fingerprint(on)).alpha_id is None


async def test_missing_sibling_does_not_discard_the_parent(runtime, settings, brain):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "gone"]})
    brain.add("GET", "/simulations/c0", {"status": "ERROR", "message": "x", "regular": "e0"})
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, timeout=60)
    await runtime.simulations.simulate(payloads[0], wait=False, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 1
    assert runtime.ledger.get(fingerprint(payloads[0])).simulation_id == "c0"


async def test_joining_a_running_batch_returns_the_own_child(runtime, settings, brain):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    for index, alpha in enumerate(("a0", "a1")):
        body = {"status": "COMPLETE", "alpha": alpha, "regular": f"e{index}", "settings": {}}
        brain.add("GET", f"/simulations/c{index}", body)
        brain.add("GET", f"/alphas/{alpha}", {"id": alpha})
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, timeout=60)
    result = await runtime.simulations.simulate(payloads[1], wait=True, timeout=60)
    assert (result.simulation_id, result.alpha_id) == ("c1", "a1")


async def test_success_without_an_alpha_can_run_again(runtime, settings, brain):
    scripted(brain, (200, {"status": "WARNING", "message": "no alpha"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    await runtime.simulations.simulate(payload, wait=True, timeout=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_unexpected_alpha_body_keeps_the_finished_simulation(runtime, settings, brain):
    scripted(brain, (200, fixture("simulation_complete")))
    brain.add("GET", "/alphas/qMPjAxnO", (200, [1, 2]))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=60
    )
    assert (result.status, result.alpha_id) == ("COMPLETE", "qMPjAxnO")
    assert "get_alpha" in result.next_step


async def test_gateway_page_during_a_wait_stays_resumable(runtime, settings, brain):
    scripted(brain, (200, "<html>gateway</html>"))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, timeout=5
    )
    assert (result.status, result.simulation_id) == ("PENDING", "sim1")


def test_ledger_and_cache_are_separate_per_account(settings):
    first = Runtime.create(settings)
    other = Runtime.create(settings.model_copy(update={"email": "other@example.com"}))
    assert first.ledger.path != other.ledger.path
    assert first.cache.directory != other.cache.directory


async def test_progress_keeps_increasing_at_the_top(runtime):
    seen = []

    async def report(value, _message):
        seen.append(value)

    call = Call(runtime=runtime, report=report)
    for _ in range(5):
        await call.progress(1.0, "finishing")
    assert seen == sorted(set(seen))
    assert len(seen) == 5
    assert seen[-1] < 1


async def test_status_of_a_parent_lists_children(runtime, settings, brain):
    await batch_routes(brain, 2)
    result = await runtime.simulations.status("parent", wait_seconds=0)
    assert [c.simulation_id for c in result.children] == ["c0", "c1"]
