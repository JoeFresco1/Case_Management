"""Local conftest for the integration tests.

Suppresses two pre-existing deprecations that fire when importing
``app.main``:

1. ``starlette.exceptions.StarletteDeprecationWarning`` -- raised by
   ``app.interfaces.http.routers.capture`` referencing the deprecated
   ``status.HTTP_422_UNPROCESSABLE_ENTITY`` (Starlette renamed it to
   ``HTTP_422_UNPROCESSABLE_CONTENT``).
2. FastAPI ``on_event`` deprecation -- raised by the
   ``@app.on_event("startup")`` / ``@app.on_event("shutdown")``
   decorators used in ``app.main``. Migrating to the lifespan API is
   out of scope for the seams inventory work and tracked elsewhere.

``pyproject.toml`` sets ``filterwarnings = ["error"]`` which re-promotes
DeprecationWarnings to errors at collection time. We use
``pytest_configure`` (which runs *after* the ini filterwarnings is
applied but before tests are collected) to re-register ignore filters.

M-3 / ADR-TRACK: See ``tests/conftest.py`` for the full rationale.
These filters duplicate the top-level ones to ensure the integration
sub-suite works even if run in isolation.
"""


def pytest_configure(config):  # noqa: ARG001 -- pytest signature
    import warnings  # noqa: PLC0415

    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*HTTP_422_UNPROCESSABLE_ENTITY.*",
    )
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*on_event is deprecated.*",
    )
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*on_event is deprecated.*",
    )
