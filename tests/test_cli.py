"""``argus-iris serve`` takes the tokens its config names from the suite file, and nothing else."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from argus_iris.cli import app

# Made-up names: a real one may be exported in the shell, and a failed assert would print it.
CALLER, AGENT, SERVICE, OTHER = (
    "IRIS_TEST_CALLER_TOKEN",
    "IRIS_TEST_AGENT_TOKEN",
    "IRIS_TEST_SVC_TOKEN",
    "IRIS_TEST_OTHER",
)


def test_serve_reads_only_its_token_names_from_the_suite_file(tmp_path, monkeypatch) -> None:
    suite = tmp_path / "suite.env"
    suite.write_text(f"{CALLER}=from-file\n{AGENT}=from-file\n{SERVICE}=from-file\n{OTHER}=from-file\n")
    suite.chmod(0o600)
    config = tmp_path / "iris.yaml"
    config.write_text(
        f"""
nodes:
  rtx3090: {{agent_url: "http://192.0.2.10:9110"}}
caller_token_env: {CALLER}
agent_token_env: {AGENT}
services:
  - id: lens
    node: rtx3090
    adapter: argus-http
    base_url: http://192.0.2.10:8100
    token_env: {SERVICE}
"""
    )
    monkeypatch.setenv("ARGUS_ENV_FILE", str(suite))
    monkeypatch.setenv(AGENT, "from-environment")  # the environment wins
    monkeypatch.setenv(CALLER, "")  # blank (compose's ${NAME:-}) falls through to the file
    for name in (SERVICE, OTHER):
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, str | None] = {}

    def fake_run(_app, **_kwargs) -> None:
        seen.update({name: os.environ.get(name) for name in (CALLER, AGENT, SERVICE, OTHER)})

    monkeypatch.setattr("uvicorn.run", fake_run)
    try:
        result = CliRunner().invoke(app, ["serve", "--config", str(config)])
    finally:
        for name in (CALLER, SERVICE, OTHER):  # apply() wrote to os.environ behind monkeypatch's back
            os.environ.pop(name, None)
    assert result.exit_code == 0, result.output
    assert seen == {CALLER: "from-file", AGENT: "from-environment", SERVICE: "from-file", OTHER: None}
    assert "from-file" not in result.output and CALLER in result.output  # names, never a value


@pytest.mark.parametrize("command", ["serve", "agent"])
@pytest.mark.parametrize("override", [None, "192.0.2.10"])
def test_binds_to_localhost_unless_overridden(tmp_path, monkeypatch, command, override):
    names = {"serve": "IRIS_HOST", "agent": "IRIS_AGENT_HOST"}
    monkeypatch.delenv(names[command], raising=False)
    if override:
        monkeypatch.setenv(names[command], override)
    config = tmp_path / "iris.yaml"
    config.write_text("nodes: {gpu: {agent_url: 'http://192.0.2.10:9110'}}\n")
    monkeypatch.setenv("IRIS_CONFIG", str(config))
    bound = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: bound.update(kw))
    monkeypatch.setattr("argus_iris.server.app.create_app", lambda *a, **kw: None)
    monkeypatch.setattr("argus_iris.agent.app.create_app", lambda *a, **kw: None)
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 0, result.output
    assert bound["host"] == (override or "127.0.0.1")


def test_without_the_cli_extra_the_command_says_what_to_install() -> None:
    # A None entry in sys.modules makes the import fail as if typer were not installed.
    code = "import sys; sys.modules['typer'] = None; import argus_iris.cli"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 1
    assert "argus-iris[server,cli]" in done.stderr and "Traceback" not in done.stderr
