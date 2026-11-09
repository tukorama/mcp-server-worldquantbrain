"""Runs against the real API. Needs WQB_LIVE=1 and credentials; never runs in CI."""

import os

import pytest

from mcp_server_worldquantbrain.client import alphas, data
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.models.simulation import simulation_payload
from mcp_server_worldquantbrain.ops import Runtime

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("WQB_LIVE") != "1", reason="set WQB_LIVE=1"),
]


@pytest.fixture(autouse=True)
def clock():
    """Real time here, not the fake clock used by the other tests."""


@pytest.fixture
async def runtime():
    instance = Runtime.create(Settings())
    yield instance
    await instance.aclose()


async def test_session(runtime):
    status = await runtime.client.authenticate()
    assert status.state == "authenticated", status


async def test_catalogues(runtime):
    operators = await data.list_operators(runtime.client, runtime.cache)
    assert operators.count > 10
    fields = await data.list_datafields(
        runtime.client,
        {
            "instrumentType": "EQUITY",
            "region": "USA",
            "delay": 1,
            "universe": "TOP3000",
            "limit": 5,
            "offset": 0,
        },
    )
    assert fields.items


@pytest.mark.skipif(os.environ.get("WQB_LIVE_SIMULATE") != "1", reason="spends simulation quota")
async def test_one_simulation(runtime):
    payload = simulation_payload(runtime.settings, "rank(-returns)")
    result = await runtime.simulations.simulate(payload, wait=True, timeout=900, force=True)
    assert result.status in ("COMPLETE", "WARNING"), result
    pnl = await alphas.get_pnl(runtime.client, result.alpha_id, include_daily=False, last_n=None)
    assert pnl.yearly
