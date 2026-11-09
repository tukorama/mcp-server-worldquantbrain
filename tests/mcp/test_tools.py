import json

import pytest
from mcp import Client
from mcp.types import Implementation

from mcp_server_worldquantbrain.server import create_server
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

ALPHA = fixture("alpha_detail")
EXPECTED_TOOLS = {
    "authenticate",
    "complete_authentication",
    "auth_status",
    "simulate_alpha",
    "simulate_alphas_batch",
    "get_simulation_status",
    "get_simulation_options",
    "get_alpha",
    "list_alphas",
    "get_alpha_pnl",
    "list_alpha_recordsets",
    "get_alpha_recordset",
    "update_alpha",
    "check_alpha_submission",
    "get_alpha_self_correlation",
    "get_alpha_prod_correlation",
    "list_datasets",
    "get_dataset",
    "list_datafields",
    "get_datafield",
    "list_data_categories",
    "list_operators",
    "get_user_profile",
    "get_user_activity",
    "list_messages",
}


@pytest.fixture
async def session(settings, brain):
    async with Client(create_server(settings, transport=brain.transport)) as client:
        yield client


async def call(session, tool, /, **arguments):
    result = await session.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_lists_every_tool_with_annotations_and_schemas(session):
    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["get_alpha"].annotations.read_only_hint is True
    assert tools["simulate_alpha"].annotations.read_only_hint is False
    assert tools["get_alpha"].title == "Get an alpha"
    for tool in tools.values():
        assert tool.description
        assert tool.output_schema, tool.name
        assert "call" not in tool.input_schema.get("properties", {})
        assert "ctx" not in tool.input_schema.get("properties", {})


async def test_output_schema_keeps_field_definitions(session):
    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    properties = tools["get_alpha"].output_schema["properties"]
    assert "in_sample" in properties
    assert "expression" in properties


async def test_auth_status(session):
    assert (await call(session, "auth_status"))["state"] == "unauthenticated"


async def test_authenticate(session):
    assert (await call(session, "authenticate"))["state"] == "authenticated"


async def test_complete_authentication_without_pending_is_an_actionable_error(session):
    result = await session.call_tool("complete_authentication", {})
    assert result.is_error
    assert "Call authenticate first" in result.content[0].text


async def test_simulate_alpha_with_progress(session, brain):
    brain.add("POST", "/simulations", (201, None, {"location": "https://x/simulations/s1"}))
    brain.add(
        "GET",
        "/simulations/s1",
        (200, {"status": "SIMULATING", "progress": 0.3}, {"retry-after": "1"}),
        (200, {"status": "SIMULATING", "progress": 0.3}, {"retry-after": "1"}),
        (200, fixture("simulation_complete")),
    )
    brain.add("GET", "/alphas/qMPjAxnO", ALPHA)
    ticks = []

    async def on_progress(progress, total, message):
        ticks.append(progress)

    result = await session.call_tool(
        "simulate_alpha", {"expression": "rank(close)"}, progress_callback=on_progress
    )
    assert result.structured_content["alpha"]["id"] == "qMPjAxnO"
    assert len(ticks) == 2
    assert ticks[0] < ticks[1]
    body = json.loads(brain.calls("POST", "/simulations")[0].content)
    assert body["regular"] == "rank(close)"
    assert body["settings"]["region"] == "USA"


async def test_invalid_arguments_are_reported(session):
    result = await session.call_tool("simulate_alpha", {"expression": "x", "delay": 7})
    assert result.is_error


async def test_batch_rejects_mixed_delays(session):
    result = await session.call_tool(
        "simulate_alphas_batch",
        {"alphas": [{"expression": "a", "delay": 0}, {"expression": "b", "delay": 1}]},
    )
    assert result.is_error
    assert "one delay" in result.content[0].text


async def test_get_simulation_status(session, brain):
    brain.add("GET", "/simulations/s1", (200, {"status": "SIMULATING"}, {"retry-after": "1"}))
    assert (await call(session, "get_simulation_status", simulation_id="s1"))["status"] == "PENDING"


async def test_get_simulation_options_is_cached(session, brain):
    brain.add("OPTIONS", "/simulations", fixture("simulation_options"))
    assert (await call(session, "get_simulation_options"))["parsed"] is True
    await call(session, "get_simulation_options")
    assert len(brain.calls("OPTIONS", "/simulations")) == 1


async def test_get_alpha(session, brain):
    brain.add("GET", "/alphas/qMPjAxnO", ALPHA)
    data = await call(session, "get_alpha", alpha_id="qMPjAxnO")
    assert data["expression"] == "close"
    assert "name" not in data


async def test_alpha_id_cannot_escape_the_path(session, brain):
    await session.call_tool("get_alpha", {"alpha_id": "../users/self"})
    assert any(b"..%2Fusers%2Fself" in r.url.raw_path for r in brain.requests)


async def test_list_alphas_sends_filters(session, brain):
    brain.add("GET", "/users/self/alphas", {"count": 75, "results": [ALPHA]})
    data = await call(session, "list_alphas", min_sharpe=1.25, region="USA", limit=1)
    assert data["next_offset"] == 1
    params = brain.calls("GET", "/users/self/alphas")[0].url.params
    assert params["is.sharpe>"] == "1.25"
    assert params["settings.region"] == "USA"


async def test_get_alpha_pnl_summarizes_cumulative_values_by_year(session, brain):
    brain.add("GET", "/alphas/a/recordsets/pnl", fixture("recordset_pnl"))
    data = await call(session, "get_alpha_pnl", alpha_id="a")
    assert [(y["year"], y["pnl"]) for y in data["yearly"]] == [
        (2019, 1000000.0),
        (2020, 500000.0),
        (2021, -100000.0),
    ]
    assert "daily" not in data
    assert data["total_pnl"] == 1400000.0


async def test_get_alpha_pnl_last_n(session, brain):
    brain.add("GET", "/alphas/a/recordsets/pnl", fixture("recordset_pnl"))
    data = await call(session, "get_alpha_pnl", alpha_id="a", last_n=2)
    assert [d["date"] for d in data["daily"]] == ["2020-12-31", "2021-01-04"]
    assert data["truncated"] is True


async def test_list_alpha_recordsets(session, brain):
    brain.add("GET", "/alphas/a/recordsets", {"results": [{"name": "pnl"}, {"name": "turnover"}]})
    assert (await call(session, "list_alpha_recordsets", alpha_id="a"))["names"] == [
        "pnl",
        "turnover",
    ]


async def test_get_alpha_recordset_names_columns_and_caps_rows(session, brain):
    brain.add("GET", "/alphas/a/recordsets/pnl", fixture("recordset_pnl"))
    data = await call(session, "get_alpha_recordset", alpha_id="a", name="pnl", max_rows=2)
    assert data["columns"] == ["date", "pnl"]
    assert data["rows"][-1] == {"date": "2021-01-04", "pnl": 1400000.0}
    assert data["truncated"] is True


async def test_unknown_recordset_lists_the_available_ones(session, brain):
    brain.add("GET", "/alphas/a/recordsets", {"results": [{"name": "pnl"}]})
    result = await session.call_tool("get_alpha_recordset", {"alpha_id": "a", "name": "nope"})
    assert result.is_error
    assert "Available recordsets: pnl" in result.content[0].text


async def test_update_alpha_nests_the_description(session, brain):
    brain.add("PATCH", "/alphas/a", ALPHA)
    await call(session, "update_alpha", alpha_id="a", name="n", description="d")
    assert json.loads(brain.calls("PATCH", "/alphas/a")[0].content) == {
        "name": "n",
        "regular": {"description": "d"},
    }


async def test_update_alpha_requires_a_change(session):
    result = await session.call_tool("update_alpha", {"alpha_id": "a"})
    assert result.is_error


async def test_check_alpha_submission(session, brain):
    brain.add(
        "GET", "/alphas/a/check", (200, "", {"retry-after": "2"}), (200, fixture("check_failing"))
    )
    data = await call(session, "check_alpha_submission", alpha_id="a")
    assert data["verdict"] == "FAIL"
    assert data["failed"] == ["LOW_SHARPE"]


async def test_check_alpha_submission_pending(session, brain):
    brain.add("GET", "/alphas/a/check", (200, "", {"retry-after": "2"}))
    data = await call(session, "check_alpha_submission", alpha_id="a", wait_seconds=1)
    assert data["verdict"] == "PENDING"


async def test_self_correlation(session, brain):
    brain.add("GET", "/alphas/a/correlations/self", fixture("correlation_self"))
    data = await call(session, "get_alpha_self_correlation", alpha_id="a")
    assert data["max_correlation"] == 0.52
    assert data["passes"] is True
    assert data["top"][0]["id"] == "def"


async def test_prod_correlation_on_a_restricted_tier(session, brain):
    brain.add("GET", "/alphas/a/correlations/prod", (403, {"detail": "Not allowed"}))
    result = await session.call_tool("get_alpha_prod_correlation", {"alpha_id": "a"})
    assert result.is_error
    assert "tier" in result.content[0].text


async def test_list_datasets_defaults_market_and_caches(session, brain):
    brain.add("GET", "/data-sets", {"count": 1, "results": [{"id": "pv1", "name": "Price"}]})
    data = await call(session, "list_datasets")
    assert data["items"][0]["id"] == "pv1"
    await call(session, "list_datasets")
    assert len(brain.calls("GET", "/data-sets")) == 1
    assert brain.calls("GET", "/data-sets")[0].url.params["universe"] == "TOP3000"


async def test_get_dataset(session, brain):
    brain.add("GET", "/data-sets/pv1", {"id": "pv1", "description": "x" * 500})
    assert len((await call(session, "get_dataset", dataset_id="pv1"))["description"]) == 500


async def test_list_datafields_trims_descriptions_and_passes_dataset(session, brain):
    brain.add(
        "GET",
        "/data-fields",
        {
            "count": 10000,
            "results": [
                {"id": "f", "description": "y" * 500, "dataset": {"id": "pv1", "name": "P"}}
            ],
        },
    )
    data = await call(session, "list_datafields", dataset_id="pv1", offset=9950)
    assert len(data["items"][0]["description"]) < 310
    assert data["items"][0]["dataset_id"] == "pv1"
    assert data["truncated_by_api"] is True
    assert brain.calls("GET", "/data-fields")[0].url.params["dataset.id"] == "pv1"


async def test_get_datafield(session, brain):
    brain.add("GET", "/data-fields/close", {"id": "close", "type": "MATRIX"})
    assert (await call(session, "get_datafield", field_id="close"))["type"] == "MATRIX"


async def test_list_data_categories(session, brain):
    brain.add("GET", "/data-categories", [{"id": "pv", "name": "PV", "children": [{"id": "c"}]}])
    assert (await call(session, "list_data_categories"))["items"][0]["children"][0]["id"] == "c"


async def test_list_operators_is_compact_unless_verbose(session, brain):
    brain.add(
        "GET",
        "/operators",
        [
            {
                "name": "rank",
                "category": "Cross Sectional",
                "definition": "rank(x)",
                "description": "long text",
                "documentation": "/doc",
            },
            {"name": "ts_mean", "category": "Time Series", "definition": "ts_mean(x, d)"},
        ],
    )
    data = await call(session, "list_operators", search="rank")
    assert data["count"] == 1
    assert "description" not in data["items"][0]
    verbose = await call(session, "list_operators", search="rank", verbose=True)
    assert verbose["items"][0]["description"] == "long text"
    assert len(brain.calls("GET", "/operators")) == 1


async def test_profile_withholds_personal_data(session, brain):
    brain.add(
        "GET",
        "/users/self",
        {"id": "AB1", "email": "me@x.io", "telephone": "123", "firstName": "M", "verified": True},
    )
    data = await call(session, "get_user_profile")
    assert "me@x.io" not in json.dumps(data)
    assert data["redacted_fields"] == ["email", "firstName", "telephone"]
    included = await call(session, "get_user_profile", include_pii=True)
    assert included["personal"]["email"] == "me@x.io"


async def test_user_activity_names_columns(session, brain):
    brain.add(
        "GET",
        "/users/self/activities/simulations",
        {
            "yesterday": 3,
            "total": 40,
            "records": {
                "schema": {"properties": [{"name": "date"}, {"name": "count"}]},
                "records": [["2026-11-01", 3]],
            },
        },
    )
    data = await call(session, "get_user_activity", kind="simulations")
    assert data["recent"] == [{"date": "2026-11-01", "count": 3}]


async def test_messages_are_plain_text(session, brain):
    blob = "A" * 3000
    brain.add(
        "GET",
        "/users/self/messages",
        {
            "count": 1,
            "results": [
                {
                    "id": "m",
                    "title": "T",
                    "description": f'<p>Hello</p><img src="data:image/png;base64,{blob}">',
                }
            ],
        },
    )
    data = await call(session, "list_messages")
    assert data["items"][0]["text"] == "Hello"


async def test_errors_never_contain_the_password(settings, brain):
    brain.add("POST", "/authentication", (401, None))
    async with Client(create_server(settings, transport=brain.transport)) as session:
        result = await session.call_tool("get_alpha", {"alpha_id": "a"})
    assert result.is_error
    assert "hunter2" not in result.content[0].text


async def test_desktop_clients_get_short_waits(settings, brain):
    brain.add("GET", "/alphas/a/check", (200, "", {"retry-after": "30"}))
    server = create_server(settings, transport=brain.transport)
    info = Implementation(name="claude-ai", version="1")
    async with Client(server, client_info=info, mode="legacy") as session:
        result = await session.call_tool(
            "check_alpha_submission", {"alpha_id": "a", "wait_seconds": 900}
        )
    assert result.structured_content["verdict"] == "PENDING"
    assert len(brain.calls("GET", "/alphas/a/check")) <= 3


async def test_alpha_reads_respect_the_short_client_cap(settings, brain, clock):
    brain.add("GET", "/alphas/a", (200, "", {"retry-after": "30"}))
    server = create_server(settings, transport=brain.transport)
    info = Implementation(name="claude-ai", version="1")
    async with Client(server, client_info=info, mode="legacy") as session:
        result = await session.call_tool("get_alpha", {"alpha_id": "a"})
    assert result.is_error
    assert sum(clock.sleeps) <= 45


async def test_static_resources_and_template_are_listed(session):
    uris = {str(r.uri) for r in (await session.list_resources()).resources}
    assert {
        "brain://operators",
        "brain://simulation-options",
        "brain://docs/fastexpr-cheatsheet",
        "brain://docs/settings-guide",
        "brain://docs/submission-criteria",
    } <= uris
    templates = (await session.list_resource_templates()).resource_templates
    assert [t.uri_template for t in templates] == ["brain://alphas/{alpha_id}"]


async def test_read_doc_resource(session):
    result = await session.read_resource("brain://docs/settings-guide")
    assert "Simulation settings" in result.contents[0].text
    assert result.contents[0].mime_type == "text/markdown"


async def test_read_operators_resource(session, brain):
    brain.add("GET", "/operators", [{"name": "rank"}])
    result = await session.read_resource("brain://operators")
    assert json.loads(result.contents[0].text)["count"] == 1


async def test_read_alpha_resource(session, brain):
    brain.add("GET", "/alphas/qMPjAxnO", ALPHA)
    result = await session.read_resource("brain://alphas/qMPjAxnO")
    assert json.loads(result.contents[0].text)["id"] == "qMPjAxnO"


async def test_prompts(session):
    names = {prompt.name for prompt in (await session.list_prompts()).prompts}
    assert names == {"design_alpha", "review_alpha"}
    result = await session.get_prompt("review_alpha", {"alpha_id": "a1"})
    assert "a1" in result.messages[0].content.text
