"""The repository's integration settings point at a runnable, parallel test gate."""

import os
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_integration_settings_name_an_executable_shard_gate():
    settings = tomllib.loads((ROOT / ".agents/integration.toml").read_text())
    assert set(settings) == {"tests", "canonical"}
    assert settings["tests"]["project"] == "."
    command = shlex.split(settings["tests"]["command"])
    assert command[0] == "bash"
    script = ROOT / command[1]
    assert script.is_file() and os.access(script, os.X_OK)
    text = script.read_text()
    # Isolation matches scripts/check-project: per-shard HOME and no inherited host state.
    assert 'env -i PATH="$PATH"' in text and 'HOME="$out/$i/home"' in text and "ruff check" in text
    assert settings["canonical"]["refresh_after_publish"] is True
