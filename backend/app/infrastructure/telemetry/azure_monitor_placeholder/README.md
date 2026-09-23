# INTEGRATION_SEAM: Azure Monitor / Application Insights — placeholder for the production telemetry adapter.

This directory reserves the location for the OTLP-based Azure Monitor
implementation of the `Telemetry` Protocol declared in `app/domain/ports.py`.

## Status

- **Local adapter implemented**:
  `app/infrastructure/telemetry/structured_logging.py` (JSON via
  `structlog.PrintLoggerFactory`) + `correlation.py` (structlog
  contextvars for `correlation_id`, `tenant_id`, `actor_id`, request
  metadata).
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Azure Monitor adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | yes (prod posture) | `https://<region>.in.applicationinsights.azure.com/v1/traces` | OTLP ingestion endpoint (Application Insights). |
| `OTEL_EXPORTER_OTLP_HEADERS` | no | `api-key=...` or `Authorization=Bearer ...` | Auth header for the ingestion endpoint. |
| `OTEL_SERVICE_NAME` | no (default `case-management-platform`) | `case-management-platform` | `service.name` resource attribute. |
| `AZURE_MONITOR_CONNECTION_STRING` | optional | `InstrumentationKey=...;IngestionEndpoint=...` | Native App Insights connection string (alternative to OTLP). |
| `TELEMETRY` | yes (selector) | `otlp` | Toggles the wiring in `app/main.py:configure_structlog`. Setting this to `otlp` without `OTEL_EXPORTER_OTLP_ENDPOINT` fails closed at startup. |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureMonitorTelemetry:
    """OTLP-based Azure Monitor adapter for Telemetry.

    Satisfies the Protocol declared in app.domain.ports.Telemetry.
    Implementation strategy:
      - Use opentelemetry-exporter-otlp-proto-http + opentelemetry-sdk.
      - Bind Azure Monitor resource attributes (service.name, service.
        version, deployment.environment) via Resource.
      - Forward the existing structlog contextvars (correlation_id,
        tenant_id, actor_id) as span attributes so traces line up with
        logs.
      - Apply the redaction list from
        ``app.infrastructure.telemetry.structured_logging`` to every
        emitted event.
    """

    def event(self, name: str, **fields: object) -> None:
        raise NotImplementedError("Azure Monitor adapter is not wired in this slice")
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `opentelemetry`,
`azure-monitor-opentelemetry-exporter`, or any other Azure SDK. The
synthetic environment must not drag Azure dependencies into its
dependency closure.