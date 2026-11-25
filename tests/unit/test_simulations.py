import asyncio
import contextlib
import json
from typing import Any, cast

import httpx2
import pytest

from mcp_server_worldquantbrain.client.ledger import fingerprint
from mcp_server_worldquantbrain.errors import BrainError, RateLimited, ServerError
from mcp_server_worldquantbrain.models.simulation import simulation_payload
from mcp_server_worldquantbrain.ops import Call, Runtime
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

LOCATION = {"location": "https://api.worldquantbrain.com/simulations/sim1"}
SESSION = {"user": {"id": "A"}, "token": {"expiry": 9000}}


def scripted(brain: Any, *polls: Any) -> Any:
    brain.add("POST", "/simulations", (201, None, LOCATION))
    brain.add("GET", "/simulations/sim1", *polls)
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))


class HeldPosts:
    """BRAIN that holds every POST /simulations until `release` is set."""

    def __init__(self) -> None:
        self.posts: list[httpx2.Request] = []
        self.first_post = asyncio.Event()
        self.second_post = asyncio.Event()
        self.release = asyncio.Event()
        self.transport = httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/authentication":
            return httpx2.Response(201 if request.method == "POST" else 200, json=SESSION)
        if request.method == "POST":
            self.posts.append(request)
            number = len(self.posts)
            if number == 1:
                self.first_post.set()
            else:
                self.second_post.set()
            await self.release.wait()
            return httpx2.Response(201, headers={"location": f"https://x/simulations/sim{number}"})
        return httpx2.Response(200, json={"status": "SIMULATING"}, headers={"retry-after": "1"})


@pytest.fixture
async def runtime(settings: Any, brain: Any) -> Any:
    instance = Runtime.create(settings, transport=brain.transport)
    yield instance
    await instance.aclose()


async def test_blocking_simulation_returns_the_alpha(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(
        brain,
        (200, {"status": "SIMULATING", "progress": 0.5}, {"retry-after": "3"}),
        (200, fixture("simulation_complete")),
    )
    ticks: list[float] = []

    async def progress(fraction: Any, message: Any) -> Any:
        ticks.append(fraction)

    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=600, progress=progress
    )
    assert result.status == "COMPLETE"
    assert result.alpha.in_sample.sharpe == 2.25
    assert ticks == [0.5]


async def test_timeout_returns_a_resumable_id(runtime: Any, settings: Any, brain: Any) -> None:
    scripted(brain, (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=12
    )
    assert result.status == "PENDING"
    assert result.simulation_id == "sim1"
    assert "get_simulation_status" in result.next_step


async def test_non_blocking_returns_immediately(runtime: Any, settings: Any, brain: Any) -> None:
    scripted(brain, (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=False, wait_seconds=600
    )
    assert result.status == "PENDING"
    assert brain.calls("GET", "/simulations/sim1") == []


async def test_failure_carries_brains_message(runtime: Any, settings: Any, brain: Any) -> None:
    scripted(brain, (200, {"status": "ERROR", "message": "Unknown variable foo"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "foo"), wait=True, wait_seconds=60
    )
    assert result.status == "ERROR"
    assert result.message == "Unknown variable foo"
    assert result.alpha is None


async def test_warning_with_alpha_counts_as_success(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, fixture("simulation_complete") | {"status": "WARNING"}))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=60
    )
    assert result.alpha is not None


async def test_repeat_is_served_from_the_ledger_without_a_post(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    again = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert again.from_ledger
    assert again.alpha is not None
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_force_runs_again(runtime: Any, settings: Any, brain: Any) -> None:
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60, force=True)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_failed_simulation_is_not_replayed_from_the_ledger(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, {"status": "ERROR", "message": "transient"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_running_simulation_is_resumed_not_restarted(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(
        brain,
        (200, {"status": "SIMULATING"}, {"retry-after": "5"}),
        (200, {"status": "SIMULATING"}, {"retry-after": "5"}),
        (200, fixture("simulation_complete")),
    )
    payload = simulation_payload(settings, "close")
    first = await runtime.simulations.simulate(payload, wait=True, wait_seconds=1)
    assert first.status == "PENDING"
    second = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert second.status == "COMPLETE"
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_stale_ledger_entry_is_discarded(runtime: Any, settings: Any, brain: Any) -> None:
    payload = simulation_payload(settings, "close")
    runtime.ledger.record(payload, "gone", "WAITING")
    scripted(brain, (200, fixture("simulation_complete")))
    result = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert result.simulation_id == "sim1"


async def batch_routes(brain: Any, count: Any) -> Any:
    children = [f"c{i}" for i in range(count)]
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": children})
    for index, child in enumerate(children):
        brain.add(
            "GET",
            f"/simulations/{child}",
            {"status": "ERROR", "message": "bad", "regular": f"e{index}"},
        )


async def test_batch_posts_an_array_and_expands_children(
    runtime: Any, settings: Any, brain: Any
) -> None:
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    assert result.batch_ids == ["parent"]
    assert [r.simulation_id for r in result.results] == ["c0", "c1"]
    assert isinstance(json.loads(brain.calls("POST", "/simulations")[0].content), list)


async def test_large_batches_are_split_into_tens(runtime: Any, settings: Any, brain: Any) -> None:
    await batch_routes(brain, 10)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(25)]
    await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    sizes = [len(json.loads(r.content)) for r in brain.calls("POST", "/simulations")]
    assert sizes == [10, 10, 5]


async def test_batch_skips_expressions_already_in_the_ledger(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, fixture("simulation_complete")))
    first = simulation_payload(settings, "close")
    await runtime.simulations.simulate(first, wait=True, wait_seconds=60)
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/sim2"}))
    brain.add("GET", "/simulations/sim2", {"status": "ERROR", "message": "x"})
    second = simulation_payload(settings, "open")
    result = await runtime.simulations.simulate_batch([first, second], wait=True, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 2
    assert result.results[0].from_ledger


async def test_batch_rate_limit_reports_what_did_not_start(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (429, {"detail": "THROTTLED"}, {"retry-after": "1"}))
    payloads = [simulation_payload(settings, f"e{i}") for i in range(12)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    assert result.status == "PARTIAL"
    assert len(result.not_started) == 12


async def test_poll_failure_after_post_is_not_reported_as_not_started(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", (403, {"detail": "gone"}))
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    assert result.not_started == []
    assert result.results[0].simulation_id == "parent"
    assert result.status == "PENDING"


async def test_identical_concurrent_calls_post_once(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    results = await asyncio.gather(
        *(runtime.simulations.simulate(payload, wait=True, wait_seconds=60) for _ in range(4))
    )
    assert len(brain.calls("POST", "/simulations")) == 1
    assert all(r.alpha is not None for r in results)


@pytest.mark.parametrize("force", [False, True])
async def test_duplicates_inside_a_batch_post_once(
    runtime: Any, settings: Any, brain: Any, force: bool
) -> None:
    await batch_routes(brain, 1)
    brain.add("GET", "/simulations/parent", {"status": "ERROR", "message": "x"})
    same = [simulation_payload(settings, "close"), simulation_payload(settings, " close ")]
    await runtime.simulations.simulate_batch(same, wait=True, wait_seconds=60, force=force)
    assert isinstance(json.loads(brain.calls("POST", "/simulations")[0].content), dict)


async def test_same_expression_with_different_settings_is_matched_by_settings(
    runtime: Any, settings: Any, brain: Any
) -> None:
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
    await runtime.simulations.simulate_batch([first, second], wait=True, wait_seconds=60)
    assert runtime.ledger.get(fingerprint(first)).alpha_id == "a0"
    assert runtime.ledger.get(fingerprint(second)).alpha_id == "a5"


async def test_failed_children_are_not_replayed_forever(
    runtime: Any, settings: Any, brain: Any
) -> None:
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, wait_seconds=60)
    await runtime.simulations.simulate_batch(payloads, wait=False, wait_seconds=60)
    await runtime.simulations.simulate_batch(payloads, wait=False, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_batch_of_only_failures_is_failed(runtime: Any, settings: Any, brain: Any) -> None:
    await batch_routes(brain, 2)
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    assert result.status == "FAILED"


async def test_regions_are_sent_in_separate_groups(runtime: Any, settings: Any, brain: Any) -> None:
    await batch_routes(brain, 2)
    payloads = [
        simulation_payload(settings, "a"),
        simulation_payload(settings, "b", region="EUR", universe="TOP2500"),
    ]
    await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    bodies = [json.loads(r.content) for r in brain.calls("POST", "/simulations")]
    assert all(isinstance(body, dict) for body in bodies)
    assert len(bodies) == 2


async def test_known_alpha_survives_a_purged_simulation(
    runtime: Any, settings: Any, brain: Any
) -> None:
    payload = simulation_payload(settings, "close")
    runtime.ledger.record(payload, "gone", "COMPLETE", "qMPjAxnO")
    brain.add("GET", "/alphas/qMPjAxnO", fixture("alpha_detail"))
    result = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert result.from_ledger
    assert result.alpha is not None
    assert brain.calls("POST", "/simulations") == []


async def test_unusable_cache_directory_does_not_lose_the_simulation(
    settings: Any, brain: Any, tmp_path: Any
) -> None:
    (tmp_path / "file").write_text("")
    settings.cache_dir = tmp_path / "file" / "sub"
    runtime = Runtime.create(settings, transport=brain.transport)
    scripted(brain, (200, fixture("simulation_complete")))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=60, force=True
    )
    assert result.status == "COMPLETE"
    await runtime.aclose()


async def test_outage_during_a_wait_returns_a_resumable_id(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (500, None))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=30
    )
    assert (result.status, result.simulation_id) == ("PENDING", "sim1")


async def test_status_check_updates_the_ledger(runtime: Any, settings: Any, brain: Any) -> None:
    scripted(brain, (200, {"status": "ERROR", "message": "bad"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=False, wait_seconds=60)
    await runtime.simulations.status("sim1", wait_seconds=0)
    await runtime.simulations.simulate(payload, wait=False, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_cancelled_post_is_recorded_so_a_retry_joins_it(runtime: Any, settings: Any) -> None:
    release = asyncio.Event()
    posted = asyncio.Event()
    posts: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/authentication":
            body = {"user": {"id": "A"}, "token": {"expiry": 9000}}
            return httpx2.Response(201 if request.method == "POST" else 200, json=body)
        if request.method == "POST":
            posts.append(request)
            posted.set()
            await release.wait()
            return httpx2.Response(201, headers={"location": "https://x/simulations/sim1"})
        return httpx2.Response(200, json={"status": "SIMULATING"}, headers={"retry-after": "1"})

    instance = Runtime.create(settings, transport=httpx2.MockTransport(handler))
    payload = simulation_payload(settings, "close")
    first = asyncio.create_task(instance.simulations.simulate(payload, wait=False, wait_seconds=60))
    await posted.wait()
    first.cancel()
    await asyncio.sleep(0)
    retry = asyncio.create_task(instance.simulations.simulate(payload, wait=False, wait_seconds=60))
    await asyncio.sleep(0.01)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await retry).simulation_id == "sim1"
    assert len(posts) == 1
    await instance.aclose()


async def test_replay_through_a_parent_does_not_guess_between_ambiguous_children(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    for child, alpha in (("c0", "a0"), ("c1", "a1")):
        body: dict[str, Any] = {
            "status": "COMPLETE",
            "alpha": alpha,
            "regular": "close",
            "settings": {},
        }
        brain.add("GET", f"/simulations/{child}", body)
        brain.add("GET", f"/alphas/{alpha}", {"id": alpha})
    on = simulation_payload(settings, "close", max_trade="ON")
    off = simulation_payload(settings, "close", max_trade="OFF")
    await runtime.simulations.simulate_batch([on, off], wait=True, wait_seconds=60)
    await runtime.simulations.simulate(on, wait=True, wait_seconds=60)
    entry = runtime.ledger.get(fingerprint(on))
    assert entry is not None
    assert entry.alpha_id is None


async def test_missing_sibling_does_not_discard_the_parent(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "gone"]})
    brain.add("GET", "/simulations/c0", {"status": "ERROR", "message": "x", "regular": "e0"})
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, wait_seconds=60)
    await runtime.simulations.simulate(payloads[0], wait=False, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 1
    entry = runtime.ledger.get(fingerprint(payloads[0]))
    assert entry is not None
    assert entry.simulation_id == "c0"


async def test_joining_a_running_batch_returns_the_own_child(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    for index, alpha in enumerate(("a0", "a1")):
        body: dict[str, Any] = {
            "status": "COMPLETE",
            "alpha": alpha,
            "regular": f"e{index}",
            "settings": {},
        }
        brain.add("GET", f"/simulations/c{index}", body)
        brain.add("GET", f"/alphas/{alpha}", {"id": alpha})
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    await runtime.simulations.simulate_batch(payloads, wait=False, wait_seconds=60)
    result = await runtime.simulations.simulate(payloads[1], wait=True, wait_seconds=60)
    assert (result.simulation_id, result.alpha_id) == ("c1", "a1")


async def test_success_without_an_alpha_can_run_again(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, {"status": "WARNING", "message": "no alpha"}))
    payload = simulation_payload(settings, "close")
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert len(brain.calls("POST", "/simulations")) == 2


async def test_unexpected_alpha_body_keeps_the_finished_simulation(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, fixture("simulation_complete")))
    brain.add("GET", "/alphas/qMPjAxnO", (200, [1, 2]))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=60
    )
    assert (result.status, result.alpha_id) == ("COMPLETE", "qMPjAxnO")
    assert "get_alpha" in result.next_step


async def test_gateway_page_during_a_wait_stays_resumable(
    runtime: Any, settings: Any, brain: Any
) -> None:
    scripted(brain, (200, "<html>gateway</html>"))
    result = await runtime.simulations.simulate(
        simulation_payload(settings, "close"), wait=True, wait_seconds=5
    )
    assert (result.status, result.simulation_id) == ("PENDING", "sim1")


def test_ledger_and_cache_are_separate_per_account(settings: Any) -> None:
    first = Runtime.create(settings)
    other = Runtime.create(settings.model_copy(update={"email": "other@example.com"}))
    assert first.ledger.path != other.ledger.path
    assert first.cache.directory != other.cache.directory


async def test_progress_keeps_increasing_at_the_top(runtime: Any) -> None:
    seen: list[float] = []

    async def report(value: Any, _message: Any) -> Any:
        seen.append(value)

    call = Call(runtime=runtime, report=report)
    for _ in range(5):
        await call.progress(1.0, "finishing")
    assert seen == sorted(set(seen))
    assert len(seen) == 5
    assert seen[-1] < 1


async def test_status_of_a_parent_lists_children(runtime: Any, settings: Any, brain: Any) -> None:
    await batch_routes(brain, 2)
    result = await runtime.simulations.status("parent", wait_seconds=0)
    assert [c.simulation_id for c in result.children] == ["c0", "c1"]


@pytest.mark.parametrize(
    ("status", "filed"),
    [
        ("FAIL", "FAILED"),
        ("CANCELED", "CANCELLED"),
        ("COMPLETED", "COMPLETE"),
        ("NEVER_SEEN", "FAILED"),
    ],
)
async def test_final_status_outside_the_ledger_alphabet_is_filed_by_meaning(
    runtime: Any, settings: Any, brain: Any, status: str, filed: str
) -> None:
    succeeded = filed == "COMPLETE"
    body = {"status": status, "message": "boom"} | ({"alpha": "qMPjAxnO"} if succeeded else {})
    scripted(brain, (200, body))
    payload = simulation_payload(settings, "close")
    result = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert (result.status, result.message) == (status, "boom")
    assert (result.alpha is not None) == succeeded
    entry = runtime.ledger.get(fingerprint(payload))
    assert entry is not None
    assert entry.status == filed
    again = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert (again.from_ledger, again.alpha is not None) == (succeeded, succeeded)
    assert len(brain.calls("POST", "/simulations")) == (1 if succeeded else 2)
    checked = await runtime.simulations.status("sim1", wait_seconds=0)
    assert (checked.status, checked.message) == (status, "boom")


@pytest.mark.parametrize("success", ["COMPLETE", "COMPLETED"])
async def test_batch_child_outside_the_ledger_alphabet_keeps_the_batch(
    runtime: Any, settings: Any, brain: Any, success: str
) -> None:
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    brain.add("GET", "/simulations/c0", {**payloads[0], "status": success, "alpha": "a0"})
    brain.add("GET", "/simulations/c1", {**payloads[1], "status": "FAIL", "message": "x"})
    brain.add("GET", "/alphas/a0", {"id": "a0"})
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=60)
    assert (result.status, result.batch_ids) == ("COMPLETE", ["parent"])
    assert [(r.simulation_id, r.status) for r in result.results] == [
        ("c0", success),
        ("c1", "FAIL"),
    ]
    assert result.results[0].alpha is not None
    again = await runtime.simulations.simulate(payloads[0], wait=True, wait_seconds=60)
    assert (again.alpha_id, again.from_ledger) == ("a0", True)
    assert len(brain.calls("POST", "/simulations")) == 1


@pytest.mark.parametrize("wait", [False, True])
async def test_resumed_batch_files_each_child_under_its_own_row(
    runtime: Any, settings: Any, brain: Any, wait: bool
) -> None:
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", (200, {"status": "SIMULATING"}, {"retry-after": "5"}))
    started = await runtime.simulations.simulate_batch(payloads, wait=wait, wait_seconds=1)
    assert started.status == "PENDING"
    brain.add("GET", "/simulations/parent", {"status": "COMPLETE", "children": ["c0", "c1"]})
    for index, payload in enumerate(payloads):
        child = {**payload, "status": "COMPLETE", "alpha": f"a{index}"}
        brain.add("GET", f"/simulations/c{index}", child)
        brain.add("GET", f"/alphas/a{index}", {"id": f"a{index}"})
    await runtime.simulations.status("parent", wait_seconds=30)
    for index, payload in enumerate(payloads):
        entry = runtime.ledger.get(fingerprint(payload))
        assert entry is not None
        assert (entry.simulation_id, entry.status, entry.alpha_id) == (
            f"c{index}",
            "COMPLETE",
            f"a{index}",
        )
    brain.add("GET", "/simulations/parent", (404, {"detail": "Not found."}))
    again = await runtime.simulations.simulate(payloads[0], wait=True, wait_seconds=60)
    assert (again.simulation_id, again.alpha_id, again.from_ledger) == ("c0", "a0", True)
    assert len(brain.calls("POST", "/simulations")) == 1


@pytest.mark.parametrize(
    ("failure", "reported"),
    [((500, {"detail": "Internal"}), "returned 500"), ((429, None), "throttling")],
)
async def test_lasting_failure_of_status_checks_is_reported(
    runtime: Any, settings: Any, brain: Any, failure: Any, reported: str
) -> None:
    scripted(brain, failure)
    payload = simulation_payload(settings, "close")
    first = await runtime.simulations.simulate(payload, wait=True, wait_seconds=120)
    assert first.status == "PENDING"
    assert first.message is not None
    assert reported in first.message
    again = await runtime.simulations.simulate(payload, wait=False, wait_seconds=120)
    assert again.from_ledger
    assert again.message is not None
    assert reported in again.message
    brain.add("GET", "/simulations/sim1", (200, fixture("simulation_complete")))
    done = await runtime.simulations.status("sim1", wait_seconds=0)
    assert done.status == "COMPLETE"
    entry = runtime.ledger.get(fingerprint(payload))
    assert entry is not None
    assert entry.status == "COMPLETE"
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_batch_reports_why_its_status_check_failed(
    runtime: Any, settings: Any, brain: Any
) -> None:
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/parent"}))
    brain.add("GET", "/simulations/parent", (500, None))
    payloads = [simulation_payload(settings, f"e{i}") for i in range(2)]
    result = await runtime.simulations.simulate_batch(payloads, wait=True, wait_seconds=30)
    assert result.status == "PENDING"
    assert result.results[0].message is not None
    assert "returned 500" in result.results[0].message


@pytest.mark.parametrize("batch", [False, True])
async def test_identical_calls_from_two_processes_post_once(settings: Any, batch: bool) -> None:
    held = HeldPosts()
    first = Runtime.create(settings, transport=held.transport)
    second = Runtime.create(settings, transport=held.transport)
    payload = simulation_payload(settings, "close")

    async def start(instance: Runtime) -> str:
        if batch:
            result = await instance.simulations.simulate_batch(
                [payload], wait=False, wait_seconds=60
            )
            return [*result.batch_ids, *(r.simulation_id for r in result.results)][0]
        single = await instance.simulations.simulate(payload, wait=False, wait_seconds=60)
        return single.simulation_id

    one = asyncio.create_task(start(first))
    await held.first_post.wait()
    other = asyncio.create_task(start(second))
    # Without a shared guard the second process posts too; give it the time to.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(held.second_post.wait(), timeout=0.3)
    held.release.set()
    assert await asyncio.gather(one, other) == ["sim1", "sim1"]
    assert len(held.posts) == 1
    await first.aclose()
    await second.aclose()


async def test_unusable_ledger_refuses_to_post(runtime: Any, settings: Any, brain: Any) -> None:
    runtime.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    runtime.ledger.path.write_text("not a database " * 100)
    scripted(brain, (200, fixture("simulation_complete")))
    payload = simulation_payload(settings, "close")
    with pytest.raises(BrainError, match="ledger"):
        await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    batch = await runtime.simulations.simulate_batch([payload], wait=True, wait_seconds=60)
    assert (batch.status, batch.not_started) == ("PARTIAL", ["close"])
    assert batch.message is not None
    assert "ledger" in batch.message
    assert brain.calls("POST", "/simulations") == []


async def test_reservation_of_another_live_process_is_waited_for(
    runtime: Any, settings: Any, brain: Any
) -> None:
    payload = simulation_payload(settings, "close")
    assert runtime.ledger.claim([fingerprint(payload)], "other-process", 60) == {}
    scripted(brain, (200, fixture("simulation_complete")))
    with pytest.raises(RateLimited, match="Another server process"):
        await runtime.simulations.simulate(payload, wait=False, wait_seconds=0.3)
    assert brain.calls("POST", "/simulations") == []


async def test_reservation_left_by_a_dead_process_expires(
    runtime: Any, settings: Any, brain: Any
) -> None:
    payload = simulation_payload(settings, "close")
    assert runtime.ledger.claim([fingerprint(payload)], "dead-process", -1) == {}
    scripted(brain, (200, fixture("simulation_complete")))
    result = await runtime.simulations.simulate(payload, wait=True, wait_seconds=60)
    assert result.status == "COMPLETE"
    assert len(brain.calls("POST", "/simulations")) == 1


async def test_redirect_from_post_simulations_is_not_a_simulation_id(
    runtime: Any, settings: Any, brain: Any
) -> None:
    moved = {"location": "https://api.worldquantbrain.com/maintenance"}
    brain.add("POST", "/simulations", (302, None, moved))
    payload = simulation_payload(settings, "close")
    with pytest.raises(ServerError, match="302"):
        await runtime.simulations.simulate(payload, wait=False, wait_seconds=60)
    assert runtime.ledger.get(fingerprint(payload)) is None


async def test_fingerprint_locks_are_dropped_once_unused(settings: Any) -> None:
    held = HeldPosts()
    instance = Runtime.create(settings, transport=held.transport)
    manager = cast("Any", instance.simulations)
    payload = simulation_payload(settings, "close")
    first = asyncio.create_task(manager.simulate(payload, wait=False, wait_seconds=60))
    await held.first_post.wait()
    with pytest.raises(RateLimited):
        await manager.simulate(payload, wait=False, wait_seconds=0.01)
    held.release.set()
    await first
    others = [simulation_payload(settings, f"e{i}") for i in range(3)]
    await manager.simulate_batch(others, wait=False, wait_seconds=60)
    assert manager._locks == {}
    await instance.aclose()
