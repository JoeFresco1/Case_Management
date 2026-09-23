# INTEGRATION_SEAM: Azure Service Bus — placeholder for the production messaging adapter.

This directory reserves the location for the Azure Service Bus implementation
of the `MessageBus` Protocol declared in `app/domain/ports.py`, plus the
subscriber side of the durable outbox pattern.

## Status

- **Local adapter implemented**: `app/infrastructure/messaging/local_message_bus.py`
  + durable outbox (`audit_outbox.py`) + `outbox_dispatcher.py` (event
  catalog `app/domain/events.py:EVENT_CATALOG`).
- **Azure adapter pending**: this directory is a placeholder; the class
  below is a stub that raises `NotImplementedError` so the seam is
  *typed* and *runtime-checkable* without lying about being implemented.

## Env vars (read by the future Azure Service Bus adapter)

| Env var | Required when wired | Example | Purpose |
|---|---|---|---|
| `AZURE_SERVICE_BUS_FQDN` | yes (prod posture) | `<namespace>.servicebus.windows.net` | Service Bus fully-qualified namespace. |
| `AZURE_SERVICE_BUS_TOPIC` | no (default `case-events`) | `case-events` | Publisher topic name. |
| `AZURE_SERVICE_BUS_SUBSCRIPTION` | yes (subscriber side) | `case-events-sub` | Subscription name on the topic. |
| `AZURE_SERVICE_BUS_AAD_APP_ID` | optional | `<uuid>` | Managed-identity app id (uses `DefaultAzureCredential`). |

The local placeholder never reads these variables — they exist only so the
seam location is discoverable and the env-var contract is documented in one
place.

## Protocol contract

```python
class AzureServiceBusMessageBus:
    """Azure Service Bus adapter for MessageBus.

    Satisfies the Protocol declared in app.domain.ports.MessageBus.
    Implementation strategy:
      - Use azure.servicebus.aio.ServiceBusClient + TopicClient /
        SubscriptionClient with DefaultAzureCredential.
      - Send via the durable outbox pattern (FM-055): the outbox row is
        committed in the same transaction as the domain change; the
        dispatcher publishes to Service Bus on the next tick.
      - Subscribe via ``azure_servicebus_placeholder/subscriber.py``;
        ack only after the application use case commits; nack + retry
        on transient failure.
    """

    async def publish(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, object],
    ) -> None:
        raise NotImplementedError("Azure Service Bus adapter is not wired in this slice")
```

## Do NOT add Azure SDK imports here yet

The placeholder deliberately does **not** import `azure-servicebus` or any
other Azure SDK. The synthetic environment must not drag Azure dependencies
into its dependency closure.