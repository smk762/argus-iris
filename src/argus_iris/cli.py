"""``argus-iris``: run the broker or a node agent."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="VRAM-aware GPU lease broker for the Argus suite.")


@app.command()
def serve(
    config: Path = typer.Option(None, "--config", "-c", envvar="IRIS_CONFIG", help="Broker YAML config."),
    host: str = typer.Option("0.0.0.0", envvar="IRIS_HOST"),
    port: int = typer.Option(8120, envvar="IRIS_PORT"),
) -> None:
    """Run the broker (observe-only in this release)."""
    import uvicorn  # noqa: PLC0415

    from argus_iris.broker.config import load_config  # noqa: PLC0415
    from argus_iris.server.app import create_app  # noqa: PLC0415

    uvicorn.run(create_app(load_config(config)), host=host, port=port)


@app.command()
def agent(
    host: str = typer.Option("0.0.0.0", envvar="IRIS_AGENT_HOST"),
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
