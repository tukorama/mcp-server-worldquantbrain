import json
from typing import Any

import pytest

from mcp_server_worldquantbrain import cli
from mcp_server_worldquantbrain.client.base import BrainClient
from tests.conftest import FakeBrain


@pytest.fixture
def typed(monkeypatch: Any, tmp_path: Any) -> Any:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt="": "typed-password")
    return tmp_path / "config" / "mcp-server-worldquantbrain"


def use(monkeypatch: Any, brain: FakeBrain) -> Any:
    def make_client(settings: Any) -> BrainClient:
        return BrainClient(settings, transport=brain.transport)

    monkeypatch.setattr(cli, "BrainClient", make_client)


def test_auth_saves_typed_credentials_after_a_real_login(typed: Any, monkeypatch: Any) -> None:
    brain = FakeBrain()
    use(monkeypatch, brain)
    assert cli.main(["auth"]) == 0
    assert json.loads((typed / "credentials.json").read_text()) == [
        "user@example.com",
        "typed-password",
    ]
    assert len(brain.calls("POST", "/authentication")) == 1


def test_auth_does_not_save_rejected_credentials(typed: Any, monkeypatch: Any) -> None:
    brain = FakeBrain()
    brain.add("POST", "/authentication", (401, None))
    use(monkeypatch, brain)
    assert cli.main(["auth"]) == 1
    assert not (typed / "credentials.json").exists()


@pytest.mark.parametrize(
    ("variable", "value"),
    [("WQB_POLL_MAX_INTERVAL", "0"), ("WQB_BASE_URL", "http://x.io"), ("WQB_LOG_LEVEL", "loud")],
)
def test_bad_configuration_names_the_variable(
    typed: Any, monkeypatch: Any, capsys: Any, variable: Any, value: Any
) -> None:
    monkeypatch.setenv(variable, value)
    assert cli.main(["tools"]) == 2
    assert variable in capsys.readouterr().err


def test_log_level_is_case_insensitive(typed: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("WQB_LOG_LEVEL", "debug")
    assert cli.main(["tools"]) == 0


def test_tools_lists_submit_only_when_enabled(typed: Any, monkeypatch: Any, capsys: Any) -> None:
    assert cli.main(["tools"]) == 0
    assert "submit_alpha  " not in capsys.readouterr().out
    monkeypatch.setenv("WQB_ALLOW_SUBMIT", "true")
    assert cli.main(["tools"]) == 0
    assert "Submit an alpha" in capsys.readouterr().out
