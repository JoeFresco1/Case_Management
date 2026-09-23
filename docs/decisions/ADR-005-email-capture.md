# ADR-005 — Inbound Email Capture Idempotency Model (P04-US6 / FM-028)

**Status:** Accepted  
**Date:** 2026-09-02  
**Owner:** Platform team  
**Related Pillars:** P04 Capture & Ingestion, P01 Case & Business Object Management, P03 Document Management, P09 Audit & Evidence  
**Pillar Question:** `specs/pillars/P04-capture-ingestion/pillar.md:62` US6  
**Tasks/Features Affected:** FM-028 (`task_eebd32a75bbd`), `backend/app/application/capture/email_capture.py:1`, `backend/app/interfaces/http/routers/capture.py:1`, `backend/app/domain/email_capture.py:1`, `backend/app/domain/ports.py` (`CapturedEmailRepository`), `backend/app/infrastructure/persistence/models.py` (`captured_emails`), `backend/alembic/versions/0010_create_captured_emails.py`, AGENTS.md §4.R8 + §12.

## Context

Pillar P04 US6 (`specs/pillars/P04-capture-ingestion/pillar.md:62`) and FM-028 (`specs/pillars/01-deduplicated-feature-map.md:77`) call for an inbound email channel whose approved messages become documents or case notes with channel + message provenance. The seam is reserved for the Graph / IMAP connector (P14); the first slice accepts JSON-shaped emails over HTTP so the operator UI can replay synthetic mailboxes without a live SMTP / Graph path.

Two distinct idempotency layers already exist on the platform:

1. **HTTP ``Idempotency-Key`` middleware** (`app/interfaces/http/idempotency.py:1`, AGENTS.md §10) — replay by SHA-256 of the canonical JSON body. Already applied to every mutating endpoint under ``/api/v1``.
2. **Resource-key idempotency** — case ``external_ref`` uniqueness (`docs/decisions/ADR-024-p01-external-ref-uniqueness-scope.md`) and ``Idempotency-Key`` table (`0006_create_configuration_and_idempotency.py`) follow the same pattern: a **durable row on the resource's own table** is the dedup ledger, keyed by a stable, resource-level identifier.

Inbound email raises a third question: what is the resource-level key for a *re-ingested* email? RFC 5322 §3.6.4 defines ``Message-Id`` as the canonical per-message identifier; it is the only field every compliant MTA assigns and every reasonable client surfaces. The HTTP ``Idempotency-Key`` header is request-scoped and rotates between clients; it cannot survive a Graph / IMAP connector retry that uses a fresh HTTP request. **Content idempotency must live on the resource's own table, keyed by ``Message-Id``**, mirroring the case ``external_ref`` pattern.

## Decision

The first slice implements inbound email capture as ``POST /api/v1/capture/email`` with the following contract:

1. **Idempotency anchor**: the RFC 5322 ``Message-Id`` (length ≤998 chars, trimmed). The application layer refuses empty / whitespace / overlong values.
2. **Durable dedup ledger**: a new ``captured_emails`` table keyed by ``UNIQUE (tenant_id, channel_id, message_id)``. The constraint is the database-level idempotency anchor; the application layer catches ``IntegrityError`` on insert and returns the prior row via a follow-up ``get_by_message_id`` lookup. The lookup is tenant-scoped (`WHERE tenant_id = :tenant_id`) so a cross-tenant Message-Id is never observable (Constitution V).
3. **Artifact destination**: ``document`` on a case in the first slice. ``case_note`` is reserved for a follow-up feature. The document is created in the same transaction as the ``CapturedEmail`` row, the ``EmailCaptured`` audit row, and the ``EmailCaptured`` outbox row — a partial commit would leave either side without provenance or artifact, which is explicitly forbidden (Constitution V, P09-US1).
4. **Authorization**: the same ``document.upload`` gate that guards ``POST /api/v1/cases/{id}/documents`` (P11-US2). No new role is required for the first slice.
5. **Two idempotency layers compose**: the HTTP ``Idempotency-Key`` middleware handles client-driven retries; the ``captured_emails`` unique constraint handles server-driven retries (Graph connector redelivery, IMAP re-fetch). A retried POST with the same ``Idempotency-Key`` AND the same ``Message-Id`` hits the same artifact.
6. **Body bytes are opaque**: the platform hashes + stores the email bytes verbatim through the existing ``DocumentStore`` port (`AGENTS.md §12`). No MIME parsing in the domain; the Graph / IMAP connector (P14) is responsible for handing the platform a complete RFC 5322 message.
7. **Re-destination reserved**: the ``captured_emails.destination`` column distinguishes ``"document"`` from the future ``"case_note"`` so the operator console can show the channel's destination mix without a second query.

## Idempotency Behavior

- **First submission of a new Message-Id**: a fresh ``Document`` + ``DocumentVersion`` + ``CapturedEmail`` + audit + outbox row are written in one transaction. Returns ``201``.
- **Duplicate ``Message-Id`` from the same tenant**: the use case's pre-check (``CapturedEmailRepository.get_by_message_id``) finds the prior row and returns it. No document / version / captured-email / audit / outbox row is written. Returns ``200`` and ``replayed: true``. The HTTP ``Idempotency-Key`` middleware sees the second request and replays the same body so a client that uses both layers sees a stable response.
- **Concurrent submissions of the same Message-Id**: the unique constraint fires on the loser's ``INSERT``; the loser's ``IntegrityError`` is translated to ``Conflict`` and the use case re-reads via ``get_by_message_id``. Net result: one artifact, one provenance row, two HTTP ``200`` responses.
- **Cross-tenant Message-Id**: the ``WHERE tenant_id = :tenant_id`` filter on ``get_by_message_id`` returns ``None`` so a foreign tenant's Message-Id is never observable. Tenant isolation is mandatory (AGENTS.md §3 R1).

## Schema Implications

- New ``captured_emails`` table (`alembic/versions/0010_create_captured_emails.py`). All column types are SQLAlchemy generic types (Uuid, JSON, DateTime(timezone=True), String, Text) — no ``JSONB`, no ``gen_random_uuid()``, portable across SQLite + PostgreSQL + Azure SQL (`AGENTS.md §3 R1`).
- New ``UniqueConstraint`` on ``(tenant_id, channel_id, message_id)`` named ``uq_captured_emails_tenant_channel_message``. Migration is reversible; ``downgrade`` drops the indexes then the table.
- New model ``CapturedEmail`` in `app/infrastructure/persistence/models.py` and a new port ``CapturedEmailRepository`` in `app/domain/ports.py` (mirrors the ``ChannelRepository`` / ``QuarantineRepository`` split).
- New SQL adapter ``SqlCapturedEmailRepository`` in `app/infrastructure/persistence/repositories/captured_email_repository.py`. The adapter catches ``IntegrityError`` on insert and translates to the domain ``Conflict`` error (which the HTTP layer maps to ``409``).

## Authorization Implications

- No new ``Action`` is required. The use case delegates to ``Action.DOCUMENT_UPLOAD`` on the supplied case's tenant. ``intake`` is denied; ``reviewer`` / ``supervisor`` are granted through the existing role matrix.
- The actor cannot impersonate a system actor: ``is_system`` actors still bypass role membership for ``SYSTEM_BYPASS`` actions (none apply here), and no new action has been added to ``SYSTEM_BYPASS``.

## Failure Behavior

- Auth / validation / not-found errors raise before any DB write.
- Storage ``DependencyUnavailable`` propagates upward; the caller's transaction rollback keeps partial state out of the database.
- A disabled email channel (or a channel of the wrong kind) is refused with ``ValidationFailed`` (``422``).
- ``Idempotency-Key`` header is required by the middleware for the ``201`` path; missing keys return ``400``.

## Azure Seam

The ``DocumentStore`` port already has an Azure Blob adapter reserved (`AGENTS.md §12`). The Graph / IMAP connector (P14) will feed the application layer directly: when the connector surfaces a new message, it calls the same ``email_capture.capture_email`` use case. The HTTP ``/api/v1/capture/email`` endpoint is for operator-UI replay / synthetic fixtures only.

## Migration Strategy

1. Run ``uv run alembic upgrade head`` to apply ``0010_create_captured_emails``.
2. Wire ``SqlCapturedEmailRepository`` through ``app/infrastructure/persistence/repositories/__init__.py`` (already done in this change).
4. Mount the capture router in ``app/main.py`` (already done in this change).
5. Synthetic seed: the operator UI can replay a synthetic mailbox via ``POST /api/v1/capture/email``; no separate seed step is required.

## Tasks/Features Affected

- ``task_eebd32a75bbd`` (this slice): application service, router, schemas, model, migration, SQL adapter, tests, ADR.
- Future P04-US6 follow-ups: case-note destination, MIME parsing (the Graph connector pre-parses), quarantine routing for invalid messages (re-use ``quarantine_capture``).
- Future P14 connectors: Graph, IMAP, Exchange — feed the same use case directly.

## Approval Gate

This ADR is implementation-aligned (no design-review gate required for the first slice). Any future change in idempotency model (e.g. switching to ``(tenant_id, sender, message_id)``, or allowing free-standing documents without a case) requires a superseding ADR.