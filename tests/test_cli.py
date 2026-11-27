"""``argus-iris serve`` takes the tokens its config names from the suite file, and nothing else."""

from __future__ import annotations

import os

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


def _check(tmp_path, admission: str, rules: str, name: str) -> tuple:
    config, policy = tmp_path / f"{name}.yaml", tmp_path / f"{name}-policy.yaml"
    config.write_text(f"admission: {admission}\nnodes:\n  rtx3090: {{agent_url: 'http://192.0.2.10:9110'}}\n")
    policy.write_text(f"residents:\n{rules}")
    return str(config), str(policy)


def test_check_flags_admission_and_newly_enforced_rules(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.delenv("IRIS_REPORT_ONLY", raising=False)
    quiet = "  - match: {node: rtx3090}\n    idle_kill: 5m\n"
    loud = quiet + "    enforce: true\n"
    old = _check(tmp_path, "observe", quiet, "old")
    run = CliRunner().invoke

    same = run(
        app, ["check", "-c", old[0], "--policy", old[1], "--previous-config", old[0], "--previous-policy", old[1]]
    )
    assert same.exit_code == 0 and json.loads(same.stdout)["approval"] == []

    new = _check(tmp_path, "enforce", loud, "new")
    out = run(
        app, ["check", "-c", new[0], "--policy", new[1], "--previous-config", old[0], "--previous-policy", old[1]]
    )
    approval = json.loads(out.stdout)["approval"]
    assert approval[0] == "admission observe -> enforce" and "enforced rule" in approval[1]

    # Already enforced and unchanged: nothing to approve.
    again = run(
        app, ["check", "-c", new[0], "--policy", new[1], "--previous-config", new[0], "--previous-policy", new[1]]
    )
    assert json.loads(again.stdout)["approval"] == []

    first = run(app, ["check", "-c", new[0], "--policy", new[1], "--previous-config", str(tmp_path / "none.yaml")])
    assert json.loads(first.stdout)["approval"] == ["no previous config to compare against"]


def test_check_fails_on_a_bad_file(tmp_path) -> None:
    bad = tmp_path / "iris.yaml"
    bad.write_text("admission: sometimes\n")
    out = CliRunner().invoke(app, ["check", "-c", str(bad)])
    assert out.exit_code == 1 and "invalid" in out.stderr
