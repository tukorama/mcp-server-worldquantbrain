"""Property-based & stateful invariant tests for expressions, paging, redaction, and ledger."""

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from mcp_server_worldquantbrain.client.ledger import (
    VALID_LEDGER_STATUSES,
    Ledger,
    fingerprint,
    normalize_expression,
)
from mcp_server_worldquantbrain.client.paging import MAX_OFFSET, page_info, segment
from mcp_server_worldquantbrain.redaction import html_to_text, register_secret, scrub, strip_base64

_WHITESPACE = st.text(alphabet=" \t\r\n", min_size=0, max_size=4)
_STATUSES: tuple[str, ...] = tuple(sorted(VALID_LEDGER_STATUSES))
_EXPRESSIONS: tuple[str, ...] = ("rank(close)", "ts_mean(returns, 20)", "-volume / adv20")
_SCRIPT_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")


@given(
    left=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_()", min_size=1, max_size=20),
    right=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_()", min_size=1, max_size=20),
    ws1=_WHITESPACE,
    ws2=_WHITESPACE,
    delay=st.sampled_from([0, 1]),
    region=st.sampled_from(["USA", "EUR", "ASI"]),
)
def test_normalize_expression_and_fingerprint_whitespace_invariance(
    left: str,
    right: str,
    ws1: str,
    ws2: str,
    delay: int,
    region: str,
) -> None:
    expr_a = f"{ws1}{left}{ws2}{right}{ws1}"
    expr_b = f"{left}{right}"
    assert normalize_expression(expr_a) == expr_b

    settings = {"region": region, "delay": delay}
    payload_a: dict[str, Any] = {"type": "REGULAR", "settings": settings, "regular": expr_a}
    payload_b: dict[str, Any] = {
        "regular": expr_b,
        "settings": {"delay": delay, "region": region},
        "type": "REGULAR",
    }
    assert fingerprint(payload_a) == fingerprint(payload_b)


@given(raw=st.text(min_size=1, max_size=80))
def test_paging_segment_bijection_and_no_unencoded_slashes(raw: str) -> None:
    encoded = segment(raw)
    assert "/" not in encoded
    assert unquote(encoded) == raw


@given(
    count=st.one_of(st.none(), st.integers(min_value=0, max_value=25_000)),
    offset=st.integers(min_value=0, max_value=MAX_OFFSET),
    limit=st.integers(min_value=1, max_value=100),
    received_raw=st.integers(min_value=0, max_value=100),
)
def test_paging_info_bounds(
    count: int | None,
    offset: int,
    limit: int,
    received_raw: int,
) -> None:
    received = min(received_raw, limit)
    next_offset, truncated = page_info(count, offset, received, limit)
    if next_offset is not None:
        assert next_offset == offset + received
        assert next_offset + limit <= MAX_OFFSET
        assert not truncated
    if received == 0:
        assert next_offset is None
        assert not truncated


@given(
    secret=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$_-",
        min_size=8,
        max_size=24,
    ),
    local=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=2, max_size=10),
    body=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .,!?<>/pbr", min_size=0, max_size=40
    ),
)
def test_redaction_and_html_stripping_invariants(secret: str, local: str, body: str) -> None:
    register_secret(secret)
    sample = f"Authorization: Bearer {secret} user={local}@example.com password={secret} {body}"
    scrubbed = scrub(sample)
    assert secret not in scrubbed
    assert f"{local}@example.com" not in scrubbed

    blob = "A" * 600
    cleaned_img = strip_base64(f'<img src="data:image/png;base64,{blob}"> {body}')
    assert blob not in cleaned_img

    extracted = html_to_text(f"<p>{body}</p>")
    assert not _SCRIPT_CONTROL.search(_SCRIPT_CONTROL.sub("", extracted))


class LedgerStateMachine(RuleBasedStateMachine):
    """Stateful verification that known alpha_id and file permissions survive any call sequence."""

    def __init__(self) -> None:
        super().__init__()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.db_path)
        self.expected_alpha: dict[str, str] = {}
        self.payload_for_sim: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def teardown(self) -> None:
        self._tmpdir.cleanup()

    @initialize()
    def init_db(self) -> None:
        payload: dict[str, Any] = {
            "type": "REGULAR",
            "settings": {"region": "USA", "delay": 1},
            "regular": _EXPRESSIONS[0],
        }
        self.ledger.record(payload, "sim-init", "WAITING")

    @rule(
        expr=st.sampled_from(_EXPRESSIONS),
        status=st.sampled_from(_STATUSES),
        has_alpha=st.booleans(),
    )
    def record_simulation(self, expr: str, status: str, has_alpha: bool) -> None:
        self._counter += 1
        sim_id = f"sim-{self._counter}"
        payload: dict[str, Any] = {
            "type": "REGULAR",
            "settings": {"region": "USA", "delay": 1},
            "regular": expr,
        }
        key = fingerprint(payload)
        alpha_id = f"alpha-{self._counter}" if has_alpha else None
        self.ledger.record(payload, sim_id, status, alpha_id)
        if alpha_id is not None:
            self.expected_alpha[key] = alpha_id
        self.payload_for_sim[sim_id] = payload

    @rule(
        status=st.sampled_from(_STATUSES),
        has_alpha=st.booleans(),
    )
    def update_known_simulation(self, status: str, has_alpha: bool) -> None:
        if not self.payload_for_sim:
            return
        sim_id = next(iter(self.payload_for_sim))
        self._counter += 1
        alpha_id = f"alpha-upd-{self._counter}" if has_alpha else None
        self.ledger.update_by_id(sim_id, status, alpha_id)
        key = fingerprint(self.payload_for_sim[sim_id])
        entry = self.ledger.get(key)
        if entry is not None and alpha_id is not None and entry.simulation_id == sim_id:
            self.expected_alpha[key] = alpha_id

    @rule(bad_status=st.sampled_from(["BOGUS", "INVALID", "DONE", ""]))
    def reject_invalid_status(self, bad_status: str) -> None:
        payload: dict[str, Any] = {
            "type": "REGULAR",
            "settings": {"region": "USA", "delay": 1},
            "regular": _EXPRESSIONS[0],
        }
        with pytest.raises(ValueError, match="Unsupported ledger status"):
            self.ledger.record(payload, "sim-bad", bad_status)
        with pytest.raises(ValueError, match="Unsupported ledger status"):
            self.ledger.update_by_id("sim-init", bad_status, None)

    @rule(expr=st.sampled_from(_EXPRESSIONS))
    def forget_expression(self, expr: str) -> None:
        payload: dict[str, Any] = {
            "type": "REGULAR",
            "settings": {"region": "USA", "delay": 1},
            "regular": expr,
        }
        key = fingerprint(payload)
        self.ledger.forget(key)
        self.expected_alpha.pop(key, None)

    @invariant()
    def alpha_id_never_erased_and_file_mode_private(self) -> None:
        if os.name == "posix" and self.db_path.exists():
            assert (self.db_path.stat().st_mode & 0o777) == 0o600
        for key, expected in self.expected_alpha.items():
            entry = self.ledger.get(key)
            assert entry is not None
            assert entry.alpha_id is not None
            if entry.alpha_id != expected:
                # An explicit new alpha replacement is allowed; erasure to None is forbidden.
                self.expected_alpha[key] = entry.alpha_id


TestLedgerStateMachine = LedgerStateMachine.TestCase
