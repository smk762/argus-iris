"""``argus-iris``: run the broker or a node agent."""

from __future__ import annotations

from pathlib import Path

try:
    import typer
except ModuleNotFoundError as exc:
    if exc.name != "typer":
        raise
    raise SystemExit(
        "The argus-iris command needs the cli extra: "
        "pip install 'argus-iris[server,cli]' (broker) or 'argus-iris[agent,cli]' (node agent)"
    ) from None

app = typer.Typer(no_args_is_help=True, add_completion=False, help="VRAM-aware GPU lease broker for the Argus suite.")


@app.command()
def serve(
    config: Path = typer.Option(None, "--config", "-c", envvar="IRIS_CONFIG", help="Broker YAML config."),
    policy: Path = typer.Option(None, "--policy", envvar="IRIS_POLICY", help="Residency policy YAML (optional)."),
    host: str = typer.Option("127.0.0.1", envvar="IRIS_HOST"),
    port: int = typer.Option(8120, envvar="IRIS_PORT"),
) -> None:
    """Run the broker."""
    import uvicorn  # noqa: PLC0415
    from argus_cortex import settings  # noqa: PLC0415

    from argus_iris.broker.config import load_config  # noqa: PLC0415
    from argus_iris.broker.policy import load_policy  # noqa: PLC0415
    from argus_iris.server.app import create_app  # noqa: PLC0415

    # The broker reads its tokens from the environment, by the names its config gives (caller_token_env,
    # agent_token_env, each service's token_env). Take those, and only those, from the suite file where the
    # environment sets none. The names are logged, never a value.
    cfg = load_config(config)
    names = {cfg.caller_token_env, cfg.agent_token_env, *(s.token_env for s in cfg.services if s.token_env)}
    applied = settings.apply(names)
    if applied:
        typer.echo(f"suite settings: {', '.join(applied)} read from the suite file", err=True)
    uvicorn.run(create_app(cfg, policy=load_policy(policy)), host=host, port=port)


@app.command()
def agent(
    host: str = typer.Option("127.0.0.1", envvar="IRIS_AGENT_HOST"),
    port: int = typer.Option(9110, envvar="IRIS_AGENT_PORT"),
) -> None:
    """Run iris-agent on a GPU host. Configured by IRIS_AGENT_* environment variables."""
    import uvicorn  # noqa: PLC0415

    from argus_iris.agent.app import create_app  # noqa: PLC0415

    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def version() -> None:
    """Print the version."""
    import argus_iris  # noqa: PLC0415

    typer.echo(argus_iris.__version__)


if __name__ == "__main__":  # pragma: no cover
    app()
