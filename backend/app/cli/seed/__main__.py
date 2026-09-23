"""Shim for ``python -m app.cli.seed`` (direct module execution).

Formerly the ``if __name__ == "__main__"`` guard at the bottom of the
single-file ``seed.py``; kept as a package ``__main__`` so the pre-split
invocation keeps working after the R2 package split.
"""

from app.cli.seed import cli_main

if __name__ == "__main__":  # pragma: no cover
    cli_main()
