import asyncio
import contextlib
import logging
import secrets
import time
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

from mcp_server_worldquantbrain.client import alphas, simulations
from mcp_server_worldquantbrain.client.base import BrainClient, parse_json
from mcp_server_worldquantbrain.client.ledger import (
    Ledger,
    LedgerEntry,
    fingerprint,
    ledger_status,
    normalize_expression,
)
from mcp_server_worldquantbrain.client.paging import segment
from mcp_server_worldquantbrain.client.simulations import RUNNING, SUCCESS, simulation_terminal
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import (
    AuthError,
    BiometricRequired,
    BrainError,
    LedgerUnavailable,
    NotFound,
    PollTimeout,
    RateLimited,
    ServerError,
)
from mcp_server_worldquantbrain.models.base import as_dict, as_list
from mcp_server_worldquantbrain.models.simulation import BatchResult, Quota, SimulationResult

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], Awaitable[None]]
# A ledger row's fingerprint and the payload filed under it.
Owner = tuple[str, dict[str, Any]]
MAX_BATCH = 10
CHILD_WAIT_SECONDS = 60
ALPHA_WAIT_SECONDS = 60
# A reservation that is no longer renewed belongs to a process that died, and expires.
RESERVATION_SECONDS = 60.0
RESERVATION_RENEW_SECONDS = 20.0
RESERVATION_POLL_SECONDS = 0.25
_LOST_CONTACT = (ServerError, RateLimited, AuthError, BiometricRequired)


async def _no_progress(_fraction: float, _message: str) -> None:
    return None


@dataclass
class _KeyLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SimulationManager:
    def __init__(self, client: BrainClient, settings: Settings, ledger: Ledger) -> None:
        self.client = client
        self.ledger = ledger
        self._slots = asyncio.Semaphore(settings.max_concurrent_simulations)
        self._locks: dict[str, _KeyLock] = {}
        self._owner = secrets.token_hex(8)

    async def _acquire(
        self, primitive: asyncio.Lock | asyncio.Semaphore, wait_seconds: float
    ) -> None:
        try:
            async with asyncio.timeout(max(wait_seconds, 0.001)):
                await primitive.acquire()
        except TimeoutError:
            raise RateLimited(
                "All local simulation slots stayed busy for the whole wait.",
                hint=(
                    "Other simulations are running. Retry when they finish, or raise "
                    + "WQB_MAX_CONCURRENT_SIMULATIONS if the account allows more."
                ),
            ) from None

    @contextlib.asynccontextmanager
    async def _locked(self, keys: list[str], deadline: float) -> AsyncGenerator[None, None]:
        """Lock fingerprints in this process. A lock is dropped once no caller holds or wants it."""
        wanted: list[str] = []
        taken: list[asyncio.Lock] = []
        try:
            for key in keys:
                entry = self._locks.setdefault(key, _KeyLock())
                entry.users += 1
                wanted.append(key)
                await self._acquire(entry.lock, deadline - time.monotonic())
                taken.append(entry.lock)
            yield
        finally:
            for lock in taken:
                lock.release()
            for key in wanted:
                entry = self._locks[key]
                entry.users -= 1
                if not entry.users:
                    del self._locks[key]

    async def _reserve(
        self, keys: list[str], deadline: float, *, force: bool
    ) -> dict[str, LedgerEntry] | None:
        """Reserve fingerprints in the ledger, waiting while another process holds one of them.

        Returns what the ledger holds for them, or None when nothing was reserved.
        """
        if not self.ledger.enabled:
            return None
        while True:
            try:
                entries = self.ledger.claim(keys, self._owner, RESERVATION_SECONDS)
            except LedgerUnavailable as exc:
                if not force:
                    raise
                # force=True already accepts running it again, which is all the ledger prevents.
                logger.warning("Simulating without the ledger: %s", exc.message)
                return None
            if entries is not None:
                return entries
            left = deadline - time.monotonic()
            if left <= 0:
                raise RateLimited(
                    "Another server process sharing the ledger is starting the same simulation.",
                    hint="Retry in a few seconds. The retry joins that simulation.",
                )
            await asyncio.sleep(min(RESERVATION_POLL_SECONDS, left))

    async def _keep_reserved(self, keys: list[str]) -> None:
        while True:
            await asyncio.sleep(RESERVATION_RENEW_SECONDS)
            self.ledger.renew(keys, self._owner, RESERVATION_SECONDS)

    @contextlib.asynccontextmanager
    async def _exclusive(
        self, keys: list[str], wait_seconds: float, *, force: bool
    ) -> AsyncGenerator[dict[str, LedgerEntry], None]:
        """Hold fingerprints against identical calls here and in processes sharing the ledger.

        Yields the ledger rows for them, read in the transaction that reserved them.
        """
        deadline = time.monotonic() + wait_seconds
        # Sorted, so two overlapping batches cannot deadlock on each other.
        ordered = sorted(set(keys))
        async with self._locked(ordered, deadline):
            entries = await self._reserve(ordered, deadline, force=force)
            if entries is None:
                yield {}
                return
            keeper = asyncio.create_task(self._keep_reserved(ordered))
            try:
                yield entries
            finally:
                keeper.cancel()
                self.ledger.release(ordered, self._owner)

    async def simulate(
        self,
        payload: dict[str, Any],
        *,
        wait: bool,
        wait_seconds: float,
        force: bool = False,
        progress: Progress = _no_progress,
    ) -> SimulationResult:
        started = time.monotonic()

        def remaining() -> float:
            return max(0.0, wait_seconds - (time.monotonic() - started))

        key = fingerprint(payload)
        slot_held = False
        try:
            # Holding this until the POST is recorded makes identical calls, here or in another
            # process sharing the ledger, join one simulation. It is released before waiting so
            # the second caller needs no slot.
            async with self._exclusive([key], wait_seconds, force=force) as entries:
                known = None if force else await self._from_ledger(payload, entries.get(key))
                if known is not None:
                    if not wait or known.status not in RUNNING:
                        return known
                    simulation_id, quota = known.simulation_id, None
                elif not wait:
                    simulation_id, quota = await self._start(payload, [payload])
                    return self._pending(simulation_id, quota=quota)
                else:
                    await self._acquire(self._slots, remaining())
                    slot_held = True
                    simulation_id, quota = await self._start(payload, [payload])
        except BaseException:
            if slot_held:
                self._slots.release()
            raise

        try:
            result = await self.wait_for(simulation_id, remaining(), progress, payload=payload)
        finally:
            if slot_held:
                self._slots.release()
        result = self._own_child(payload, result)
        result.quota = quota
        result.from_ledger = known is not None
        return result

    async def _start(
        self, body: dict[str, Any] | list[dict[str, Any]], payloads: list[dict[str, Any]]
    ) -> tuple[str, Quota | None]:
        async def post_and_record() -> tuple[str, Quota | None]:
            simulation_id, quota = await simulations.start_simulation(self.client, body)
            for payload in payloads:
                self.ledger.record(payload, simulation_id, "WAITING")
            logger.info("Started simulation %s", simulation_id)
            return simulation_id, quota

        # If the client cancels mid-POST, BRAIN may already have accepted it. Finish recording the
        # id before the callers release their locks, so a retry joins that simulation.
        task = asyncio.ensure_future(post_and_record())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({task})
            raise

    async def _from_ledger(
        self, payload: dict[str, Any], entry: LedgerEntry | None
    ) -> SimulationResult | None:
        if entry is None or not self._reusable(entry):
            return None
        try:
            result = await self.wait_for(entry.simulation_id, 0, payload=payload)
        except NotFound:
            if entry.status in SUCCESS and entry.alpha_id:
                try:
                    alpha = await alphas.get_alpha(self.client, entry.alpha_id)
                except NotFound:
                    pass
                else:
                    return SimulationResult(
                        simulation_id=entry.simulation_id,
                        status=entry.status,
                        alpha_id=entry.alpha_id,
                        alpha=alpha,
                        from_ledger=True,
                    )
            # Only while the row still points there: another process may have filed a new run.
            self.ledger.forget(fingerprint(payload), entry.simulation_id)
            return None
        result = self._own_child(payload, result)
        result.from_ledger = True
        return result

    @staticmethod
    def _reusable(entry: LedgerEntry) -> bool:
        return entry.status in RUNNING or (entry.status in SUCCESS and entry.alpha_id is not None)

    def _own_child(self, payload: dict[str, Any], result: SimulationResult) -> SimulationResult:
        """When the ledger pointed at a batch parent, answer with this payload's own child."""
        if not result.children:
            return result
        current = self.ledger.get(fingerprint(payload))
        own = (c for c in result.children if current and c.simulation_id == current.simulation_id)
        return next(own, result)

    @staticmethod
    def _owner_of(payload: dict[str, Any] | None) -> Owner | None:
        return None if payload is None else (fingerprint(payload), payload)

    def _members(self, simulation_id: str, owner: Owner | None) -> dict[str, dict[str, Any]]:
        """The rows a batch parent's children may be filed under.

        A status check files every row the batch was recorded under, since nothing else would.
        A call that joined through one payload files only that one, so each sibling's outcome is
        still reported when it is looked up, before a failed one can run again.
        """
        if owner is not None:
            return {owner[0]: owner[1]}
        return self.ledger.members(simulation_id)

    def _pending(
        self,
        simulation_id: str,
        *,
        progress: float | None = None,
        elapsed: float | None = None,
        quota: Quota | None = None,
        message: str | None = None,
    ) -> SimulationResult:
        return SimulationResult(
            simulation_id=simulation_id,
            status="PENDING",
            progress=progress,
            elapsed_seconds=elapsed,
            quota=quota,
            message=message,
            next_step=(
                f"Still running. Call get_simulation_status('{simulation_id}', "
                + "wait_seconds=30) to resume; nothing is resubmitted."
            ),
        )

    @staticmethod
    def _failed_check(timeout: PollTimeout) -> str | None:
        """Say why BRAIN gave no answer, so an outage does not pass for a running simulation."""
        if timeout.last_error is None:
            return None
        return f"The last status check failed: {timeout.last_error.for_agent()}"

    async def wait_for(
        self,
        simulation_id: str,
        wait_seconds: float,
        progress: Progress = _no_progress,
        *,
        payload: dict[str, Any] | None = None,
    ) -> SimulationResult:
        started = time.monotonic()

        async def tick(response: httpx2.Response, elapsed: float) -> None:
            body = as_dict(parse_json(response))
            reported = body.get("progress")
            fraction = (
                float(reported)
                if isinstance(reported, int | float)
                else min(elapsed / max(wait_seconds, 1), 0.95)
            )
            await progress(fraction, f"{body.get('status', 'RUNNING')} after {int(elapsed)}s")

        def elapsed() -> float:
            return round(time.monotonic() - started, 1)

        try:
            response = await self.client.poll(
                f"/simulations/{segment(simulation_id)}",
                max_seconds=wait_seconds,
                terminal=simulation_terminal,
                on_poll=tick,
            )
        except PollTimeout as exc:
            body = as_dict(exc.last_body)
            reported = body.get("progress")
            return self._pending(
                simulation_id,
                progress=float(reported) if isinstance(reported, int | float) else None,
                elapsed=round(exc.elapsed, 1),
                message=self._failed_check(exc),
            )
        except _LOST_CONTACT as exc:
            return self._pending(simulation_id, elapsed=elapsed(), message=exc.for_agent())
        budget = max(0.0, wait_seconds - (time.monotonic() - started))
        result = await self._finish(simulation_id, response.json(), self._owner_of(payload), budget)
        result.elapsed_seconds = elapsed()
        return result

    async def _finish(
        self,
        simulation_id: str,
        body: dict[str, Any],
        owner: Owner | None,
        budget: float,
    ) -> SimulationResult:
        alpha_id = body.get("alpha")
        status = str(body.get("status") or ("COMPLETE" if alpha_id else "UNKNOWN"))
        children = as_list(body.get("children"))
        if children:
            return SimulationResult(
                simulation_id=simulation_id,
                status=status,
                message=body.get("message"),
                children=await self._children(
                    children, self._members(simulation_id, owner), budget
                ),
            )
        if owner is not None:
            self.ledger.record(owner[1], simulation_id, status, alpha_id, key=owner[0])
        else:
            self.ledger.update_by_id(simulation_id, status, alpha_id)
        result = SimulationResult(
            simulation_id=simulation_id,
            status=status,
            alpha_id=alpha_id,
            message=body.get("message"),
        )
        if alpha_id and ledger_status(status) in SUCCESS:
            try:
                result.alpha = await alphas.get_alpha(
                    self.client, alpha_id, max_seconds=min(budget, ALPHA_WAIT_SECONDS)
                )
            except Exception as exc:
                # The simulation is done and paid for, so its id must reach the caller regardless.
                logger.warning("Could not read alpha %s: %s", alpha_id, exc)
                result.next_step = f"Stats not ready yet. Call get_alpha('{alpha_id}')."
        return result

    @staticmethod
    def _matching_member(body: dict[str, Any], members: dict[str, dict[str, Any]]) -> str | None:
        expression = normalize_expression(str(body.get("regular")))
        returned = as_dict(body.get("settings"))
        candidates = [
            key
            for key, payload in members.items()
            if normalize_expression(str(payload.get("regular"))) == expression
            and all(
                returned.get(name, value) == value for name, value in payload["settings"].items()
            )
        ]
        # BRAIN may echo fewer settings than were sent. If that leaves it ambiguous, record
        # nothing rather than file an alpha under settings it did not run with.
        return candidates[0] if len(candidates) == 1 else None

    async def _children(
        self, children: list[Any], members: dict[str, dict[str, Any]], budget: float
    ) -> list[SimulationResult]:
        async def fetch(child_id: str) -> dict[str, Any] | None:
            try:
                response = await self.client.poll(
                    f"/simulations/{segment(child_id)}",
                    max_seconds=min(budget, CHILD_WAIT_SECONDS),
                    terminal=simulation_terminal,
                )
            except (PollTimeout, NotFound, *_LOST_CONTACT):
                return None
            return response.json()

        ids = [str(child) for child in children]
        bodies = await asyncio.gather(*(fetch(child) for child in ids))
        matches = [self._matching_member(b, members) if b else None for b in bodies]
        # A row matched by two children is ambiguous, so neither may be filed under it.
        claims = Counter(match for match in matches if match)

        async def finish(child: str, body: dict[str, Any] | None, match: Any) -> SimulationResult:
            if body is None:
                return self._pending(child)
            owner = (match, members[match]) if match and claims[match] == 1 else None
            return await self._finish(child, body, owner, budget)

        return list(await asyncio.gather(*map(finish, ids, bodies, matches)))

    async def status(
        self, simulation_id: str, *, wait_seconds: float, progress: Progress = _no_progress
    ) -> SimulationResult:
        return await self.wait_for(simulation_id, wait_seconds, progress)

    @staticmethod
    def _chunks(payloads: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        # BRAIN rejects a multi-simulation that mixes regions.
        groups: dict[str, list[dict[str, Any]]] = {}
        for payload in payloads:
            groups.setdefault(str(payload["settings"]["region"]), []).append(payload)
        return [
            group[i : i + MAX_BATCH]
            for group in groups.values()
            for i in range(0, len(group), MAX_BATCH)
        ]

    async def simulate_batch(
        self,
        payloads: list[dict[str, Any]],
        *,
        wait: bool,
        wait_seconds: float,
        force: bool = False,
        progress: Progress = _no_progress,
    ) -> BatchResult:
        batch = BatchResult(status="COMPLETE")
        todo: list[dict[str, Any]] = []
        queued: set[str] = set()
        seen: set[str] = set()
        for payload in payloads:
            key = fingerprint(payload)
            # Even with force, one call never pays twice for the same payload.
            if key in queued:
                continue
            queued.add(key)
            known = None if force else await self._from_ledger(payload, self.ledger.get(key))
            if known is None:
                todo.append(payload)
            elif known.simulation_id not in seen:
                seen.add(known.simulation_id)
                batch.results.append(known)
        chunks = self._chunks(todo)
        deadline = time.monotonic() + wait_seconds
        for index, chunk in enumerate(chunks):
            remaining = deadline - time.monotonic()
            if wait and remaining <= 0:
                batch.not_started.extend(str(p["regular"]) for p in chunk)
                continue
            before = len(batch.batch_ids)
            try:
                await self._run_chunk(batch, chunk, wait, remaining, force)
            except BrainError as exc:
                # A chunk whose POST went through is running, whatever failed afterwards.
                started = len(batch.batch_ids) > before
                if started:
                    batch.results.append(self._pending(batch.batch_ids[-1]))
                batch.not_started.extend(
                    str(p["regular"]) for later in chunks[index + started :] for p in later
                )
                batch.message = exc.for_agent()
                break
            await progress((index + 1) / len(chunks), f"Group {index + 1}/{len(chunks)} done")
        self._summarize(batch, wait)
        return batch

    @staticmethod
    def _summarize(batch: BatchResult, wait: bool) -> None:
        running = [r.simulation_id for r in batch.results if r.status in RUNNING]
        if batch.not_started:
            batch.status = "PARTIAL"
            batch.next_step = "Resubmit not_started later. Finished ones come from the ledger."
        elif running or (batch.batch_ids and not wait):
            ids = list(dict.fromkeys([*batch.batch_ids, *running]))
            batch.status = "PENDING"
            batch.next_step = f"Call get_simulation_status for each of: {', '.join(ids)}."
        elif batch.results and not any(ledger_status(r.status) in SUCCESS for r in batch.results):
            batch.status = "FAILED"

    def _collect_fresh_chunk(
        self,
        batch: BatchResult,
        chunk: list[dict[str, Any]],
        entries: dict[str, LedgerEntry],
        force: bool,
    ) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for payload in chunk:
            entry = None if force else entries.get(fingerprint(payload))
            if entry is not None and self._reusable(entry):
                batch.results.append(self._pending(entry.simulation_id))
            else:
                fresh.append(payload)
        return fresh

    async def _poll_chunk_outcome(
        self,
        batch: BatchResult,
        parent: str,
        fresh: list[dict[str, Any]],
        time_left: Callable[[], float],
    ) -> None:
        try:
            response = await self.client.poll(
                f"/simulations/{segment(parent)}",
                max_seconds=max(0.0, time_left()),
                terminal=simulation_terminal,
            )
        except PollTimeout as exc:
            batch.results.append(self._pending(parent, message=self._failed_check(exc)))
            return
        except _LOST_CONTACT as exc:
            batch.results.append(self._pending(parent, message=exc.for_agent()))
            return
        data = response.json()
        budget = max(0.0, time_left())
        children = as_list(data.get("children"))
        if children:
            members = {fingerprint(payload): payload for payload in fresh}
            batch.results.extend(await self._children(children, members, budget))
        else:
            single = fresh[0] if len(fresh) == 1 else None
            batch.results.append(await self._finish(parent, data, self._owner_of(single), budget))

    async def _run_chunk(
        self,
        batch: BatchResult,
        chunk: list[dict[str, Any]],
        wait: bool,
        remaining: float,
        force: bool,
    ) -> None:
        started = time.monotonic()
        slot_held = False

        def time_left() -> float:
            return remaining - (time.monotonic() - started)

        keys = [fingerprint(payload) for payload in chunk]
        try:
            async with self._exclusive(keys, time_left(), force=force) as entries:
                fresh = self._collect_fresh_chunk(batch, chunk, entries, force)
                if not fresh:
                    return
                if wait:
                    await self._acquire(self._slots, time_left())
                    slot_held = True
                parent, _ = await self._start(fresh[0] if len(fresh) == 1 else fresh, fresh)
                batch.batch_ids.append(parent)
        except BaseException:
            if slot_held:
                self._slots.release()
            raise
        if not wait:
            return
        try:
            await self._poll_chunk_outcome(batch, parent, fresh, time_left)
        finally:
            self._slots.release()
