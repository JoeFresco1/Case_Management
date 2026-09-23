"""Typer CLI surface for the synthetic data seed.

Owns the ``app.cli`` Typer instance and the ``seed`` command wrapper —
it prints the synthetic-environment marker, runs the async seed runner,
and echoes the summary when ``--verbose`` is passed. The command is
registered on the Typer app here so ``python -m app.cli.seed`` (via
``cli_main``) and ``app.cli.__main__`` (which imports ``seed``) share
one registration point.
"""

from __future__ import annotations

import asyncio

import typer

from app.config import get_settings

from .runner import _seed

__all__ = ["app", "cli_main", "seed"]

app = typer.Typer(
    name="app.cli",
    help="Seed the local database with synthetic tenants/users/case types.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def seed(
    verbose: bool = typer.Option(
        False,
        "--verbose/--quiet",
        help="Echo the seeded row identifiers after the run.",
    ),
) -> None:
    """Seed synthetic ACME tenant + users + ServiceRequest case type.

    Also persists the demo vertical: published workflow
    (``intake → in_review → decided → closed``), one demo case in
    ``intake`` with its initial review task, and a 48h review SLA clock.
    """
    settings = get_settings()
    typer.echo(
        f"[seed] APP_ENV={settings.app_env} SYNTHETIC_ENVIRONMENT={settings.synthetic_environment}"
    )
    summary = asyncio.run(_seed())
    if verbose:
        typer.echo("[seed] done")
        typer.echo(f"  tenant: {summary['tenant']['key']} ({summary['tenant']['id']})")
        for u in summary["users"]:
            typer.echo(f"  user:   {u['key']} -> {u['user_id']} ({u['email']})")
        typer.echo(
            f"  case_type: {summary['case_type']['key']} v{summary['case_type']['version']} "
            f"[{summary['case_type']['status']}]"
        )
        if summary["workflow"] is not None:
            wf = summary["workflow"]
            guarded = ",".join(wf["guarded_transitions"]) or "<none>"
            typer.echo(f"  workflow: {wf['key']} v{wf['version']} (guarded: {guarded})")


def cli_main() -> None:  # pragma: no cover — exercised via ``python -m app.cli``
    app()
