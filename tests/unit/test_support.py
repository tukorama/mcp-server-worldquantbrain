import json
import logging
import tomllib
from pathlib import Path

import pytest

from mcp_server_worldquantbrain.client.alphas import summarize_checks
from mcp_server_worldquantbrain.client.cache import JsonCache
from mcp_server_worldquantbrain.client.filters import alpha_query
from mcp_server_worldquantbrain.client.ledger import Ledger, fingerprint
from mcp_server_worldquantbrain.client.paging import page_info
from mcp_server_worldquantbrain.client.simulations import parse_options
from mcp_server_worldquantbrain.config import Settings
from mcp_server_worldquantbrain.errors import BrainValidationError, ConfigError
from mcp_server_worldquantbrain.logging_setup import RedactingFormatter
from mcp_server_worldquantbrain.models.alpha import AlphaSummary
from mcp_server_worldquantbrain.models.simulation import simulation_payload
from mcp_server_worldquantbrain.redaction import html_to_text, register_secret, scrub, strip_base64
from tests.conftest import fixture


def test_env_credentials_win_over_the_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('["file@x.io", "filepw"]')
    settings = Settings(_env_file=None, email="env@x.io", password="envpw", credentials_file=path)
    assert settings.credentials().email == "env@x.io"


@pytest.mark.parametrize("content", ['["a@x.io", "pw"]', '{"email": "a@x.io", "password": "pw"}'])
def test_credentials_file_formats(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content)
    assert Settings(_env_file=None, credentials_file=path).credentials().password == "pw"


def test_malformed_credentials_file_is_a_config_error(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"user": 1}')
    with pytest.raises(ConfigError):
        Settings(_env_file=None, credentials_file=path).credentials()


def test_missing_explicit_credentials_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        Settings(_env_file=None, credentials_file=tmp_path / "none.json").credentials()


def test_password_is_hidden_in_repr():
    assert "s3cret" not in repr(Settings(_env_file=None, password="s3cret"))


def test_scrub_masks_headers_tokens_and_emails():
    text = scrub(
        "Authorization: Basic dXNlcjpwYXNz cookie: t=abc "
        'password="abc" jwt eyJhbGci.eyJzdWIi.c2ln mail john.doe@example.com'
    )
    for leaked in ("dXNlcjpwYXNz", "t=abc", '"abc"', "eyJhbGci", "john.doe"):
        assert leaked not in text


def test_registered_secret_is_masked_in_tracebacks():
    register_secret("topsecretvalue")
    try:
        raise ValueError("failed with topsecretvalue")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, "f", 1, "boom", None, sys.exc_info())
    assert "topsecretvalue" not in RedactingFormatter().format(record)


def test_html_and_base64_are_stripped():
    blob = "A" * 600
    assert html_to_text(f'<p>Hi <b>there</b></p><img src="data:image/png;base64,{blob}">') == (
        "Hi there"
    )
    assert blob not in strip_base64(f"x {blob} y")


def test_filters_put_the_operator_in_the_parameter_name():
    query = dict(alpha_query(limit=50, offset=0, min_sharpe=1.25, name="mom", hidden=False))
    assert query == {
        "limit": 50,
        "offset": 0,
        "is.sharpe>": 1.25,
        "name": "mom",
        "hidden": "false",
    }


def test_filter_encoding_on_the_wire():
    import httpx2

    url = httpx2.URL(
        "https://x/a",
        params=alpha_query(
            limit=1, offset=0, min_sharpe=1.25, created_after="2026-01-01T00:00:00+00:00"
        ),
    )
    query = url.query.decode()
    assert "is.sharpe%3E=1.25" in query
    assert "%2B00:00" in query or "%2B00%3A00" in query


@pytest.mark.parametrize(
    ("count", "offset", "received", "limit", "expected"),
    [
        (120, 0, 50, 50, (50, False)),
        (120, 100, 20, 50, (None, False)),
        (10000, 9950, 50, 50, (None, True)),
        (None, 0, 50, 50, (50, False)),
        (None, 50, 3, 50, (None, False)),
    ],
)
def test_page_info(count, offset, received, limit, expected):
    assert page_info(count, offset, received, limit) == expected


def test_ledger_round_trip_and_whitespace_insensitive_key(tmp_path, settings):
    ledger = Ledger(tmp_path / "l.sqlite3")
    a = simulation_payload(settings, "rank( close )")
    b = simulation_payload(settings, "rank(close)")
    ledger.record(a, "sim1", "COMPLETE", "alpha1")
    assert fingerprint(a) == fingerprint(b)
    entry = ledger.get(fingerprint(b))
    assert (entry.simulation_id, entry.alpha_id) == ("sim1", "alpha1")
    ledger.forget(fingerprint(a))
    assert ledger.get(fingerprint(a)) is None


def test_running_ledger_entry_does_not_erase_a_known_alpha(tmp_path, settings):
    ledger = Ledger(tmp_path / "l.sqlite3")
    payload = simulation_payload(settings, "close")
    ledger.record(payload, "sim1", "COMPLETE", "alpha1")
    ledger.record(payload, "sim2", "WAITING")
    ledger.record(payload, "sim2", "ERROR")
    assert ledger.get(fingerprint(payload)).alpha_id == "alpha1"
    ledger.record(payload, "sim3", "COMPLETE", "alpha3")
    assert ledger.get(fingerprint(payload)).alpha_id == "alpha3"


def test_disabled_ledger_is_inert(tmp_path, settings):
    ledger = Ledger(tmp_path / "l.sqlite3", enabled=False)
    payload = simulation_payload(settings, "close")
    ledger.record(payload, "s", "COMPLETE")
    assert ledger.get(fingerprint(payload)) is None


def test_cache_expires(tmp_path, monkeypatch):
    cache = JsonCache(tmp_path)
    cache.set("k", {"a": 1})
    assert cache.get("k", 100) == {"a": 1}
    import mcp_server_worldquantbrain.client.cache as module

    real = module.time.time
    monkeypatch.setattr(module.time, "time", lambda: real() + 101)
    assert cache.get("k", 100) is None


def test_cache_can_be_disabled(tmp_path):
    cache = JsonCache(tmp_path, ttl_override=0)
    cache.set("k", 1)
    assert cache.get("k", 100) is None
    assert not list(tmp_path.iterdir())


def test_alpha_summary_from_api():
    alpha = AlphaSummary.from_api(fixture("alpha_detail"))
    assert alpha.expression == "close"
    assert alpha.in_sample.sharpe == 2.25
    assert alpha.out_of_sample is None
    assert alpha.warning_checks == ["REVERSION_COMPONENT"]
    dumped = json.loads(alpha.model_dump_json())
    assert "name" not in dumped
    assert "out_of_sample" not in dumped


def test_zero_stats_are_still_stats():
    alpha = AlphaSummary.from_api({"id": "x", "is": {"sharpe": 0.0, "pnl": 0, "turnover": 0.0}})
    assert alpha.in_sample.sharpe == 0.0


def test_empty_stats_block_is_absent():
    assert AlphaSummary.from_api({"id": "x", "os": {}}).out_of_sample is None


def test_unusual_check_values_do_not_break_parsing():
    data = {"id": "x", "is": {"checks": [{"name": "A", "result": "PASS", "value": [1, 2]}]}}
    assert AlphaSummary.from_api(data).checks[0].value == [1, 2]


def test_alpha_summary_tolerates_a_compact_list_item():
    alpha = AlphaSummary.from_api({"id": "x", "is": {"sharpe": 1.0, "fitness": 0.5}})
    assert alpha.in_sample.fitness == 0.5


def test_check_verdicts():
    assert summarize_checks("a", fixture("check_failing")).verdict == "FAIL"
    assert summarize_checks("a", fixture("check_passing")).verdict == "PASS"
    assert summarize_checks("a", {"is": {"checks": []}}).verdict == "PENDING"
    pending = {"is": {"checks": [{"name": "SOME_CHECK", "result": "PENDING"}]}}
    assert summarize_checks("a", pending).pending is True


def test_pending_correlation_does_not_hold_the_verdict_open():
    payload = {
        "is": {
            "checks": [
                {"name": "LOW_SHARPE", "result": "PASS"},
                {"name": "SELF_CORRELATION", "result": "PENDING"},
            ]
        }
    }
    result = summarize_checks("a", payload)
    assert (result.verdict, result.pending) == ("PASS", False)
    assert result.correlation_pending == ["SELF_CORRELATION"]
    assert "get_alpha_self_correlation" in result.next_step


def test_message_that_is_only_a_link_keeps_the_link():
    assert html_to_text('<p><a href="https://x.io/video"></a></p>') == "https://x.io/video"
    assert html_to_text('<p>Text <a href="https://x.io">here</a></p>') == "Text here"


def test_payload_uses_defaults_and_overrides(settings):
    payload = simulation_payload(settings, "close", region="JPN", universe="TOP1600", decay=0)
    assert payload["type"] == "REGULAR"
    assert payload["settings"]["region"] == "JPN"
    assert payload["settings"]["maxTrade"] == "ON"
    assert payload["settings"]["universe"] == "TOP1600"
    assert simulation_payload(settings, "close")["settings"]["universe"] == "TOP3000"
    assert "testPeriod" not in payload["settings"]


def test_options_parser_handles_flat_and_dependent_choices():
    options = parse_options(fixture("simulation_options"))
    assert options.parsed
    assert options.fields["instrumentType"].values == ["EQUITY"]
    assert options.fields["universe"].by_context == {"EQUITY:USA": ["TOP3000", "TOP500"]}


def test_options_parser_refuses_to_report_empty_choices():
    payload = {
        "actions": {"POST": {"settings": {"children": {"u": {"choices": {"a": {"b": {}}}}}}}}
    }
    assert not parse_options(payload).parsed


def test_dotenv_cannot_redirect_credentials_or_enable_submit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "WQB_BASE_URL=https://evil.example\nWQB_ALLOW_SUBMIT=true\nWQB_DEFAULT_REGION=EUR\n"
    )
    settings = Settings()
    assert settings.base_url == "https://api.worldquantbrain.com"
    assert settings.allow_submit is False
    assert settings.default_region == "EUR"


def test_scrub_masks_header_mappings():
    text = scrub("""{'Authorization': 'Basic dXNlcjpwYXNz', 'Cookie': 't=abc'}""")
    assert "dXNlcjpwYXNz" not in text
    assert "t=abc" not in text


def test_unknown_check_results_are_not_a_pass():
    assert (
        summarize_checks("a", {"is": {"checks": [{"name": "X", "result": "NOT_RUN"}]}}).verdict
        == "PENDING"
    )


def test_empty_region_means_the_default(settings):
    assert simulation_payload(settings, "close", region="")["settings"]["universe"] == "TOP3000"


def test_other_region_needs_an_explicit_universe(settings):
    with pytest.raises(BrainValidationError):
        simulation_payload(settings, "close", region="ASI")
    assert simulation_payload(settings, "close", region="ASI", universe="MINVOL1M")


def test_blank_variables_mean_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WQB_ALLOW_SUBMIT", "")
    monkeypatch.setenv("WQB_CREDENTIALS_FILE", "")
    settings = Settings()
    assert settings.allow_submit is False
    assert settings.credentials_file is None


def test_checks_are_collected_from_every_block():
    payload = {
        "is": {"checks": [{"name": "A", "result": "PASS"}]},
        "os": {"checks": [{"name": "B", "result": "FAIL"}]},
    }
    assert summarize_checks("a", payload).failed == ["B"]


def test_options_parser_keeps_flat_value_maps():
    children = {"u": {"choices": {"USA": [{"value": "TOP3000"}], "EUR": [{"value": "TOP2500"}]}}}
    payload = {"actions": {"POST": {"settings": {"children": children}}}}
    assert parse_options(payload).fields["u"].by_context == {
        "USA": ["TOP3000"],
        "EUR": ["TOP2500"],
    }


def test_manifest_version_matches_the_package():
    root = Path(__file__).parent.parent.parent
    package = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((root / "server.json").read_text())
    assert {manifest["version"], manifest["packages"][0]["version"]} == {package}


def test_options_parser_falls_back_to_raw():
    options = parse_options({"unexpected": True})
    assert not options.parsed
    assert options.raw == {"unexpected": True}
