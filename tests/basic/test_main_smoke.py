import os
import platform

import pytest
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from aider.main import main, main_async


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, mocker):
    """Completely isolated test environment with no real API keys."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    clean_env = {
        "OPENAI_API_KEY": "test-key",
        "AIDER_CHECK_UPDATE": "false",
        "AIDER_ANALYTICS": "false",
    }

    if platform.system() == "Windows":
        clean_env["USERPROFILE"] = str(fake_home)
    else:
        clean_env["HOME"] = str(fake_home)

    mocker.patch.dict(os.environ, clean_env, clear=True)
    mocker.patch(
        "aider.io.webbrowser.open",
        side_effect=AssertionError("Browser should not open during tests"),
    )
    mocker.patch("builtins.input", return_value=None)
    monkeypatch.chdir(tmp_path)

    yield tmp_path


async def test_main_async_executes():
    await main_async(["--exit", "--yes-always"], input=DummyInput(), output=DummyOutput())


def test_main_executes():
    main(["--exit", "--yes-always"], input=DummyInput(), output=DummyOutput())
