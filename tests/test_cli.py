"""``argus-iris serve`` takes the tokens its config names from the suite file, and nothing else."""

from __future__ import annotations

import os

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
  morpheus: {{agent_url: "http://192.168.1.39:9110"}}
caller_token_env: {CALLER}
agent_token_env: {AGENT}
services:
  - id: lens
    node: morpheus
    adapter: argus-http
    base_url: http://192.168.1.39:8100
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
