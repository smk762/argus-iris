"""``argus-iris``: run the broker or a node agent."""

from __future__ import annotations

from pathlib import Path

import typer

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
def check(
    config: Path = typer.Option(..., "--config", "-c", help="Broker YAML config to validate."),
    policy: Path = typer.Option(None, "--policy", help="Residency policy YAML to validate."),
    previous_config: Path = typer.Option(None, help="The config deployed now, to compare against."),
    previous_policy: Path = typer.Option(None, help="The policy deployed now, to compare against."),
) -> None:
    """Validate a config and policy with this release's code, without starting anything; print a JSON summary.

    With the files deployed now, ``approval`` lists what makes the change need a person to look at it (#65): another
    difference at all between the old and the new config or policy, as this release reads them (so a comment or a
    default spelled out is not one). Exits 1 when a new file does not load; a deployed file this release no longer
    reads cannot be compared, so it needs approval instead.
    """
    import json  # noqa: PLC0415

    from argus_iris.broker.config import load_config  # noqa: PLC0415
    from argus_iris.broker.policy import Policy, load_policy  # noqa: PLC0415

    def summary(cfg_path: Path, pol_path: Path | None) -> dict:
        cfg = load_config(cfg_path)
        pol = load_policy(pol_path) if pol_path else Policy()
        enforced = [r.model_dump(mode="json", exclude_none=True) for r in pol.residents if r.enforce]
        return {
            "admission": cfg.admission,
            "nodes": sorted(cfg.nodes),
            "enforced": enforced,
            "_config": cfg.model_dump(mode="json"),
            "_policy": pol.model_dump(mode="json"),
        }

    def changed(old: dict, new: dict) -> list[str]:
        return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))

    try:
        new = summary(config, policy)
    except Exception as exc:  # noqa: BLE001 - any load failure is the answer, printed for the deploy log
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(1) from exc
    approval: list[str] = []
    old = None
    unreadable = False
    if previous_config and previous_config.exists():
        # The deployed files were written for the release running now. One this release no longer reads (a removed
        # key, a policy file that was never written) must not block the deployment that replaces it.
        try:
            old = summary(previous_config, previous_policy if previous_policy and previous_policy.exists() else None)
        except Exception as exc:  # noqa: BLE001
            unreadable = True
            typer.echo(f"previous files do not load: {exc}", err=True)
    if unreadable:
        approval.append("the deployed config or policy does not load with this release: nothing to compare against")
    elif old is None:
        approval.append("no previous config to compare against")
    else:
        # Any difference needs a person (#65): rules are first-match, so even a reorder changes what governs a
        # resident, and config fields such as evict_by or lease_stall_reclaim let admission act. The first two lines
        # name the changes that matter most; the last two catch everything else.
        if new["admission"] != old["admission"]:
            approval.append(f"admission {old['admission']} -> {new['admission']}")
        approval += [
            f"enforced rule new or changed: {json.dumps(r['match'])}"
            for r in new["enforced"]
            if r not in old["enforced"]
        ]
        config_keys = [k for k in changed(old["_config"], new["_config"]) if k != "admission"]
        if config_keys:
            approval.append(f"config changed: {', '.join(config_keys)}")
        policy_keys = changed(old["_policy"], new["_policy"])
        if policy_keys:
            approval.append(f"policy changed: {', '.join(policy_keys)}")
    shown = {k: v for k, v in new.items() if not k.startswith("_")}
    typer.echo(json.dumps({**shown, "approval": approval}, indent=2))


@app.command()
def version() -> None:
    """Print the version."""
    import argus_iris  # noqa: PLC0415

    typer.echo(argus_iris.__version__)


if __name__ == "__main__":  # pragma: no cover
    app()
