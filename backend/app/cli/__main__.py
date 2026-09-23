"""Entry point for ``python -m app.cli``.

Loads the seed use case and dispatches ``typer``'s CLI. The seed
subcommand is invoked as ``python -m app.cli seed``; running with
no subcommand prints help.
"""

from __future__ import annotations

from app.cli.seed import seed as _seed

if __name__ == "__main__":
    _seed()
