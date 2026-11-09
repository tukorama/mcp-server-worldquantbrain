import logging

import httpx2

from mcp_server_worldquantbrain.client import alphas as api
from mcp_server_worldquantbrain.client.base import parse_json
from mcp_server_worldquantbrain.client.paging import segment
from mcp_server_worldquantbrain.errors import PermissionDenied, PollTimeout, ServerError
from mcp_server_worldquantbrain.models.alpha import SubmitResult
from mcp_server_worldquantbrain.ops.registry import AlphaId, Call, operation

logger = logging.getLogger(__name__)

CHECK_WAIT = 120
CORRELATION_WAIT = 300
SUBMIT_WAIT = 300
MIN_SECONDS_TO_SUBMIT = 10
_NO_CORRELATION = "Self-correlation is unavailable, so the correlation gate could not run."
_DISABLE_GATE = "Set WQB_SUBMIT_CORRELATION_GATE=0 to rely on BRAIN's own check."


def _finished(response: httpx2.Response) -> bool:
    return 200 <= response.status_code < 300 and "retry-after" not in response.headers


def _refused(alpha_id: str, message: str, **kwargs) -> SubmitResult:
    return SubmitResult(alpha_id=alpha_id, outcome="refused", message=message, **kwargs)


def _unknown_outcome(alpha_id: str) -> SubmitResult:
    return SubmitResult(
        alpha_id=alpha_id,
        outcome="pending",
        message="BRAIN accepted the request and has not given a verdict yet.",
        next_step="Do not submit again. Check get_alpha: status ACTIVE means it went through.",
    )


def _rejected(alpha_id: str, exc: PermissionDenied) -> SubmitResult:
    result = api.summarize_checks(alpha_id, exc.body)
    return SubmitResult(
        alpha_id=alpha_id,
        outcome="rejected",
        message=f"BRAIN rejected the submission: {', '.join(result.failed) or exc.message}",
        checks=result.checks or None,
    )


@operation("Submit an alpha", destructive=True, requires_submit_flag=True)
async def submit_alpha(call: Call, alpha_id: AlphaId) -> SubmitResult:
    """Submit an alpha to BRAIN. This cannot be undone and uses the daily submission allowance.

    Runs the pre-submission checks and the self-correlation test first and refuses without
    contacting the submit endpoint if any check fails. Only call this when the user asked for it.
    """
    client = call.client
    current = await api.get_alpha(client, alpha_id, max_seconds=call.limit_wait(60))
    if current.status != "UNSUBMITTED":
        return _refused(alpha_id, f"Alpha is {current.status or 'in an unknown state'}.")

    await call.progress(0.1, "Running submission checks")
    check = await api.check_submission(client, alpha_id, wait_seconds=call.limit_wait(CHECK_WAIT))
    if check.failed:
        return _refused(alpha_id, f"Checks failed: {', '.join(check.failed)}.", checks=check.checks)
    if check.verdict == "PENDING":
        return _refused(
            alpha_id, "Checks are still pending.", next_step="Call submit_alpha again in a minute."
        )

    gate = call.settings.submit_correlation_gate
    if gate > 0:
        await call.progress(0.4, "Checking self-correlation")
        try:
            correlation = await api.get_correlation(
                client,
                alpha_id,
                "self",
                wait_seconds=call.limit_wait(CORRELATION_WAIT),
                threshold=gate,
            )
        except PermissionDenied:
            return _refused(alpha_id, _NO_CORRELATION, next_step=_DISABLE_GATE)
        if correlation.pending:
            return _refused(
                alpha_id,
                "Self-correlation is still being computed.",
                next_step="Call submit_alpha again in a few minutes.",
            )
        if correlation.passes is None:
            return _refused(alpha_id, _NO_CORRELATION, next_step=_DISABLE_GATE)
        if not correlation.passes:
            return _refused(
                alpha_id,
                f"Self-correlation {correlation.max_correlation:.3f} is at or above {gate}.",
            )

    if call.wait_cap is not None and call.limit_wait(SUBMIT_WAIT) < MIN_SECONDS_TO_SUBMIT:
        return _refused(
            alpha_id,
            "The checks used up this client's time limit, so nothing was submitted.",
            next_step="Call submit_alpha again; the checks are cached and it will be quicker.",
        )

    await call.progress(0.7, "Submitting")
    url = f"/alphas/{segment(alpha_id)}/submit"
    deadline = None
    if call.wait_cap is not None:
        deadline = client.deadline_after(call.limit_wait(SUBMIT_WAIT))
    try:
        await client.request("POST", url, long_poll=True, deadline=deadline)
    except PermissionDenied as exc:
        return _rejected(alpha_id, exc)
    except PollTimeout:
        # Only a 429 or a refused connection pauses a POST, so BRAIN did not process this one.
        return _refused(
            alpha_id,
            "BRAIN is throttling submissions, so nothing was submitted.",
            next_step="Call submit_alpha again later.",
        )
    except ServerError as exc:
        exc.hint = "The outcome is unknown. Check get_alpha before submitting again."
        raise

    # From here the POST is out, so anything unexpected must say "do not submit again".
    try:
        response = await client.poll(
            url, max_seconds=call.limit_wait(SUBMIT_WAIT), terminal=_finished, min_interval=2
        )
    except PermissionDenied as exc:
        return _rejected(alpha_id, exc)
    except Exception as exc:
        logger.warning("Lost track of submission for %s: %s", alpha_id, exc)
        return _unknown_outcome(alpha_id)

    try:
        final = await api.get_alpha(client, alpha_id, max_seconds=call.limit_wait(60))
        if final.status == "ACTIVE":
            return SubmitResult(alpha_id=alpha_id, outcome="submitted")
        result = api.summarize_checks(alpha_id, parse_json(response))
    except Exception as exc:
        logger.warning("Could not confirm submission of %s: %s", alpha_id, exc)
        return _unknown_outcome(alpha_id)
    failed = [check.name or "" for check in result.checks if check.result == "FAIL"]
    if failed:
        return SubmitResult(
            alpha_id=alpha_id,
            outcome="rejected",
            message=f"BRAIN rejected the submission: {', '.join(failed)}",
            checks=result.checks,
        )
    return SubmitResult(
        alpha_id=alpha_id,
        outcome="pending",
        message=f"The submit call finished but the alpha is still {final.status}.",
        next_step="Do not submit again. Check get_alpha in a few minutes.",
    )
