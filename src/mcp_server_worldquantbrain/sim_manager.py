import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from mcp_server_worldquantbrain.client import alphas, simulations
from mcp_server_worldquantbrain.client.base import BrainClient, parse_json
from mcp_server_worldquantbrain.client.ledger import (
    Ledger,
    LedgerEntry,
    fingerprint,
    normalize_expression,
)
from mcp_server_worldquantbrain.client.paging import segment
from mcp_server_worldquantbrain.client.simulations import RUNNING, SUCCESS, simulation_terminal
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import (
    AuthError,
    BiometricRequired,
    BrainError,
    NotFound,
    PollTimeout,
    RateLimited,
    ServerError,
)
from mcp_server_worldquantbrain.models.simulation import BatchResult, Quota, SimulationResult

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], Awaitable[None]]
MAX_BATCH = 10
CHILD_WAIT_SECONDS = 60
ALPHA_WAIT_SECONDS = 60
_LOST_CONTACT = (ServerError, RateLimited, AuthError, BiometricRequired)


async def _no_progress(_fraction: float, _message: str) -> None:
    return None


class SimulationManager:
    def __init__(self, client: BrainClient, settings: Settings, ledger: Ledger) -> None:
        self.client = client
        self.ledger = ledger
        self._slots = asyncio.Semaphore(settings.max_concurrent_simulations)
        self._locks: dict[str, asyncio.Lock] = {}

    async def _acquire(self, primitive: asyncio.Lock | asyncio.Semaphore, timeout: float) -> None:
        try:
            await asyncio.wait_for(primitive.acquire(), max(timeout, 0.001))
        except TimeoutError:
            raise RateLimited(
                "All local simulation slots stayed busy for the whole wait.",
                hint="Other simulations are running. Retry when they finish, or raise "
                "WQB_MAX_CONCURRENT_SIMULATIONS if the account allows more.",
            ) from None

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def simulate(
        self,
        payload: dict[str, Any],
        *,
        wait: bool,
        timeout: float,
        force: bool = False,
        progress: Progress = _no_progress,
    ) -> SimulationResult:
        started = time.monotonic()

        def remaining() -> float:
            return max(0.0, timeout - (time.monotonic() - started))

        # Holding this until the POST is recorded makes identical concurrent calls join one
        # simulation. It is released before waiting so the second caller needs no slot.
        lock = self._lock(fingerprint(payload))
        await self._acquire(lock, timeout)
        slot_held = False
        try:
            known = None if force else await self._from_ledger(payload)
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
        finally:
            lock.release()

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

    async def _from_ledger(self, payload: dict[str, Any]) -> SimulationResult | None:
        key = fingerprint(payload)
        entry = self.ledger.get(key)
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
            self.ledger.forget(key)
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
            next_step=f"Still running. Call get_simulation_status('{simulation_id}', "
            "wait_seconds=30) to resume; nothing is resubmitted.",
        )

    async def wait_for(
        self,
        simulation_id: str,
        timeout: float,
        progress: Progress = _no_progress,
        *,
        payload: dict[str, Any] | None = None,
    ) -> SimulationResult:
        started = time.monotonic()

        async def tick(response: httpx2.Response, elapsed: float) -> None:
            body = parse_json(response)
            body = body if isinstance(body, dict) else {}
            reported = body.get("progress")
            fraction = (
                float(reported)
                if isinstance(reported, int | float)
                else min(elapsed / max(timeout, 1), 0.95)
            )
            await progress(fraction, f"{body.get('status', 'RUNNING')} after {int(elapsed)}s")

        def elapsed() -> float:
            return round(time.monotonic() - started, 1)

        try:
            response = await self.client.poll(
                f"/simulations/{segment(simulation_id)}",
                max_seconds=timeout,
                terminal=simulation_terminal,
                on_poll=tick,
            )
        except PollTimeout as exc:
            body = exc.last_body if isinstance(exc.last_body, dict) else {}
            reported = body.get("progress")
            return self._pending(
                simulation_id,
                progress=float(reported) if isinstance(reported, int | float) else None,
                elapsed=round(exc.elapsed, 1),
            )
        except _LOST_CONTACT as exc:
            return self._pending(simulation_id, elapsed=elapsed(), message=exc.for_agent())
        budget = max(0.0, timeout - (time.monotonic() - started))
        result = await self._finish(simulation_id, response.json(), payload, budget)
        result.elapsed_seconds = elapsed()
        return result

    async def _finish(
        self,
        simulation_id: str,
        body: dict[str, Any],
        payload: dict[str, Any] | None,
        budget: float,
    ) -> SimulationResult:
        alpha_id = body.get("alpha")
        status = body.get("status") or ("COMPLETE" if alpha_id else "UNKNOWN")
        children = body.get("children")
        if isinstance(children, list) and children:
            return SimulationResult(
                simulation_id=simulation_id,
                status=status,
                message=body.get("message"),
                children=await self._children(
                    children, [payload] if payload is not None else [], budget
                ),
            )
        if payload is not None:
            self.ledger.record(payload, simulation_id, status, alpha_id)
        else:
            self.ledger.update_by_id(simulation_id, status, alpha_id)
        result = SimulationResult(
            simulation_id=simulation_id,
            status=status,
            alpha_id=alpha_id,
            message=body.get("message"),
        )
        if alpha_id and status in SUCCESS:
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
    def _matching_payload(
        body: dict[str, Any], payloads: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        expression = normalize_expression(str(body.get("regular")))
        settings = body.get("settings")
        returned: dict[str, Any] = settings if isinstance(settings, dict) else {}
        candidates = {
            fingerprint(payload): payload
            for payload in payloads
            if normalize_expression(str(payload.get("regular"))) == expression
            and all(returned.get(key, value) == value for key, value in payload["settings"].items())
        }
        # BRAIN may echo fewer settings than were sent. If that leaves it ambiguous, record
        # nothing rather than file an alpha under settings it did not run with.
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    async def _children(
        self, children: list[Any], payloads: list[dict[str, Any]], budget: float
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
        matches = [self._matching_payload(b, payloads) if b else None for b in bodies]
        # A payload matched by two children is ambiguous, so neither may be filed under it.
        claims = Counter(fingerprint(match) for match in matches if match)

        async def finish(child: str, body: dict[str, Any] | None, match: Any) -> SimulationResult:
            if body is None:
                return self._pending(child)
            owner = match if match and claims[fingerprint(match)] == 1 else None
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
        timeout: float,
        force: bool = False,
        progress: Progress = _no_progress,
    ) -> BatchResult:
        batch = BatchResult(status="COMPLETE")
        todo: list[dict[str, Any]] = []
        queued: set[str] = set()
        seen: set[str] = set()
        for payload in payloads:
            key = fingerprint(payload)
            if key in queued and not force:
                continue
            known = None if force else await self._from_ledger(payload)
            if known is None:
                queued.add(key)
                todo.append(payload)
            elif known.simulation_id not in seen:
                seen.add(known.simulation_id)
                batch.results.append(known)
        chunks = self._chunks(todo)
        deadline = time.monotonic() + timeout
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
        elif batch.results and not any(r.status in SUCCESS for r in batch.results):
            batch.status = "FAILED"

    async def _run_chunk(
        self,
        batch: BatchResult,
        chunk: list[dict[str, Any]],
        wait: bool,
        remaining: float,
        force: bool,
    ) -> None:
        started = time.monotonic()
        # Sorted, so two overlapping batches cannot deadlock on each other.
        locks = [self._lock(key) for key in sorted({fingerprint(p) for p in chunk})]
        taken: list[asyncio.Lock] = []
        slot_held = False

        def time_left() -> float:
            return remaining - (time.monotonic() - started)

        try:
            for lock in locks:
                await self._acquire(lock, time_left())
                taken.append(lock)
            fresh = []
            for payload in chunk:
                entry = None if force else self.ledger.get(fingerprint(payload))
                if entry is not None and self._reusable(entry):
                    batch.results.append(self._pending(entry.simulation_id))
                else:
                    fresh.append(payload)
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
        finally:
            for lock in taken:
                lock.release()
        if not wait:
            return
        try:
            try:
                response = await self.client.poll(
                    f"/simulations/{segment(parent)}",
                    max_seconds=max(0.0, time_left()),
                    terminal=simulation_terminal,
                )
            except (PollTimeout, *_LOST_CONTACT):
                batch.results.append(self._pending(parent))
                return
            data = response.json()
            budget = max(0.0, time_left())
            children = data.get("children")
            if isinstance(children, list) and children:
                batch.results.extend(await self._children(children, fresh, budget))
            else:
                single = fresh[0] if len(fresh) == 1 else None
                batch.results.append(await self._finish(parent, data, single, budget))
        finally:
            self._slots.release()
