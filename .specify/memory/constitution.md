# Case Management Platform Constitution

## Purpose

This project is a modern, Azure-native enterprise case and workflow management platform intended for regulated operational environments, including workflows involving protected health information (PHI).

The platform exists to make complex enterprise business processes understandable, configurable, testable, observable, portable, and maintainable.

The system must avoid recreating the architectural characteristics that cause legacy case-management platforms to become difficult to understand, modify, integrate with, or replace.

---

## I. The Case Is the Primary Domain Object

The Case is the fundamental unit of work within the platform.

Cases may contain:

- Structured data
- Participants
- Documents
- Tasks
- Workflow state
- Decisions
- Communications
- Assignments
- Deadlines and SLAs
- Audit history

Documents, tasks, workflows, queues, and rules exist in support of cases rather than replacing the Case as the primary business concept.

Domain models must use business terminology rather than terminology inherited from OnBase or another legacy platform.

---

## II. Business Process Must Be Explicit

Business behavior must not be hidden inside application code, UI components, database procedures, or undocumented integrations when that behavior can reasonably be represented explicitly.

Workflow definitions, routing rules, case schemas, permissions, SLAs, and similar business configuration should be represented as inspectable system artifacts.

A developer or administrator should be able to determine:

- What a process does
- Why a transition occurs
- What conditions control it
- What actions it performs
- What systems it affects
- What version of the process executed

Business logic must not become tribal knowledge.

---

## III. Workflows Are Versioned Domain Artifacts

Workflow definitions are first-class, versioned artifacts.

A workflow must have an identifiable definition and version.

Published workflow versions are immutable.

Changes produce new versions rather than silently changing historical behavior.

Existing cases must retain sufficient information to determine which workflow definition governed their execution.

Workflow definitions should support automated validation and testing.

The system must eventually support human tasks, automated actions, conditional transitions, timers, escalations, approvals, exceptions, parallel work, and reusable subprocesses without requiring modifications to the core workflow engine for individual business processes.

---

## IV. Every Important Action Must Be Explainable

The platform must answer:

**What happened?**

**When did it happen?**

**Who or what caused it?**

**Why did it happen?**

Important domain actions must generate durable audit events.

Examples include:

- Case creation
- Case modification
- Workflow transitions
- Assignments
- Task completion
- Decisions
- Rule evaluation affecting workflow behavior
- Document creation or modification
- Permission-sensitive operations
- Automated system actions

Observability logs and business audit history are separate concerns.

Application logs may expire.

Business audit history must remain part of the durable case record according to applicable retention requirements.

---

## V. Security Is Architectural

Security must not be added after features are implemented.

Production architecture must assume the system may process PHI and other sensitive enterprise information.

Production workloads containing regulated information must operate within organization-approved Microsoft Azure infrastructure.

Microsoft Entra ID is the preferred identity provider.

Authorization must be enforced server-side.

UI visibility is never an authorization mechanism.

Access must follow least-privilege principles.

Secrets must not be embedded in source code, client applications, workflow definitions, or repositories.

Managed identities and Azure Key Vault should be preferred over stored credentials where appropriate.

Sensitive information must be encrypted in transit and at rest using approved mechanisms.

Sensitive data must not be written unnecessarily to application logs, telemetry, exception traces, prompts, or development tooling.

Security-relevant operations must be auditable.

---

## VI. Azure-Native Production, Portable Domain

Azure is the required production environment, but Azure must not define the core business domain.

Preferred production capabilities include:

- Microsoft Entra ID
- Azure SQL
- Azure Blob Storage
- Azure Service Bus
- Azure Key Vault
- Azure Monitor
- Application Insights
- Approved Azure compute services

Azure-specific implementations should sit behind clear application boundaries when practical.

Core case, workflow, task, rule, and document behavior should remain testable without requiring a live Azure environment.

Local development using synthetic data must remain possible.

---

## VII. API-First Boundaries

Core platform capabilities must expose intentional service interfaces.

Other systems must not require direct database access to interact with the platform.

External systems should interact through supported:

- APIs
- Commands
- Events
- Integration adapters

Internal database schemas are implementation details, not integration contracts.

Interfaces must be versionable where compatibility matters.

Breaking external contracts requires explicit migration consideration.

---

## VIII. Event-Driven Where Valuable, Not Everywhere

Events should represent meaningful facts that have occurred within the domain.

Examples:

- CaseCreated
- CaseAssigned
- DocumentAdded
- TaskCompleted
- WorkflowTransitioned
- CaseClosed

Events should be used when they improve decoupling, integration, auditability, or asynchronous processing.

Event-driven architecture must not be introduced merely for architectural sophistication.

Simple synchronous operations should remain synchronous when that is clearer and safer.

---

## IX. Configuration Over One-Off Customization

New business processes should primarily be introduced through configuration of reusable platform capabilities rather than creation of bespoke application branches.

Configurable concepts should eventually include:

- Case types
- Case schemas
- Workflow definitions
- Rules
- Queues
- Assignments
- SLAs
- Document requirements
- Roles
- Permissions

Configuration must itself be validated, versioned, and testable.

Configuration must not become an untyped mechanism for hiding arbitrary executable code.

---

## X. Documents Are First-Class but Storage-Agnostic

Documents are first-class domain objects associated with cases.

The domain model must distinguish document identity and metadata from physical storage.

Document capabilities may include:

- Upload
- Retrieval
- Metadata
- Classification
- Versioning
- Relationships
- Permissions
- Retention
- Audit history

The core application must not depend directly upon Panzura, a filesystem path, or a particular object-storage implementation.

Storage providers must be replaceable through defined abstractions.

---

## XI. Incremental Replacement Over Big-Bang Migration

The platform must support incremental adoption.

A single business process should be capable of moving to the platform without requiring every existing OnBase process to move simultaneously.

Architecture decisions that require organization-wide migration before value can be delivered should be treated with suspicion.

Migration compatibility must not permanently distort the target architecture.

Adapters belong at system boundaries.

The new platform must not become structurally dependent upon OnBase.

---

## XII. No OnBase Clone

OnBase is an incumbent system and migration source, not the architectural specification for this product.

Do not reproduce:

- OnBase terminology unnecessarily
- OnBase screen structures
- OnBase workflow abstractions solely for compatibility
- OnBase storage architecture
- OnBase configuration patterns
- Legacy limitations

Existing OnBase behavior may inform requirements.

It must not automatically determine their implementation.

Every migrated capability should be reconsidered based upon the underlying business requirement.

---

## XIII. Vertical Slices Over Horizontal Infrastructure Projects

Development should prioritize complete, demonstrable business capabilities.

Prefer:

**Create Case → Route Case → Perform Work → Attach Document → Make Decision → Close Case → Inspect History**

over independently building months of infrastructure without a usable workflow.

Infrastructure should emerge in support of real product capabilities.

Each major implementation phase should leave behind something demonstrably more useful than the previous phase.

---

## XIV. Deterministic Core Before AI Enhancement

Core case-management behavior must function correctly without an LLM.

AI may enhance:

- Case summarization
- Document classification
- Search
- Workflow discovery
- Workflow authoring
- Migration analysis
- Process recommendations
- User assistance

AI must not become an unexplained authority over critical workflow transitions, authorization, compliance requirements, or irreversible actions.

Where AI influences consequential behavior, its recommendation, confidence, source context, and resulting human/system decision should be distinguishable when appropriate.

Deterministic mechanisms remain authoritative for core workflow execution.

---

## XV. Correctness Over Cleverness

Prefer boring, understandable architecture over unnecessary sophistication.

Every abstraction must solve an identifiable problem.

Do not introduce:

- Microservices without a demonstrated boundary
- Event sourcing solely because audit history exists
- CQRS without a demonstrated need
- Distributed processing where a simpler design works
- Custom infrastructure where a maintained platform capability solves the problem
- Premature optimization

Begin with a modular architecture that preserves boundaries and can evolve when scale or organizational requirements justify decomposition.

---

## XVI. Test Business Behavior, Not Just Code

Testing must verify domain behavior.

Important workflows require tests covering:

- Expected paths
- Conditional paths
- Invalid transitions
- Permission boundaries
- Failure behavior
- Exception paths
- Rule evaluation
- Workflow version behavior

Workflow definitions and business configuration must be testable artifacts.

Critical workflow behavior should be reproducible without manual UI testing.

---

## XVII. Backward Compatibility Is Intentional

Persistent data, workflow definitions, APIs, events, and configuration may survive individual application releases.

Changes affecting persisted or external contracts require explicit compatibility consideration.

Database schema changes must use migrations.

Published workflow versions must remain interpretable.

Breaking changes must not occur accidentally as side effects of refactoring.

---

## XVIII. Observability Is a Product Capability

Production behavior must be diagnosable.

Important operations should provide appropriate:

- Structured logging
- Metrics
- Tracing
- Correlation identifiers
- Health information
- Failure diagnostics

A case operation should be traceable across relevant system components without exposing unnecessary PHI.

Technical observability and business audit history must remain distinct but correlatable.

---

## XIX. Failure Must Be Designed

Enterprise workflows are long-running and failures are inevitable.

Operations involving asynchronous work, integrations, timers, or external systems must consider:

- Retries
- Idempotency
- Duplicate delivery
- Partial failure
- Timeouts
- Poison messages
- Recovery
- Manual intervention

Failures must not silently lose work.

Operators must be able to determine what failed and how affected work can be recovered.

---

## XX. Human Work Is a First-Class Capability

This is a case-management platform, not merely a background workflow engine.

Human work must be explicitly modeled.

The platform should support concepts such as:

- Tasks
- Ownership
- Assignment
- Team queues
- Priorities
- Due dates
- SLAs
- Escalations
- Delegation
- Completion requirements

Users should be able to understand what requires their attention and why.

---

## XXI. Accessibility and Usability Are Requirements

The platform exists to improve operational work.

Interfaces should prioritize:

- Clear information hierarchy
- Efficient case navigation
- Keyboard accessibility
- Accessible interaction patterns
- Minimal unnecessary navigation
- Clear workflow state
- Clear next actions
- Visible deadlines and priorities

A technically superior workflow engine with a poor case-worker experience does not satisfy the product objective.

---

## XXII. Prototype Without Creating Prototype Debt

The first objective is to prove a representative enterprise workflow can operate end-to-end.

The prototype may intentionally omit enterprise-scale capabilities.

It must not intentionally violate foundational architecture merely to create a demonstration faster.

Mock implementations are acceptable behind real interfaces.

Synthetic data is preferred.

Shortcuts that would require rewriting the core domain model to proceed beyond the prototype are not acceptable.

---

## Engineering Decision Priority

When principles conflict, decisions should generally prioritize:

1. Security and regulatory obligations
2. Data integrity and correctness
3. Auditability and explainability
4. Business-process correctness
5. Maintainability and architectural clarity
6. User experience
7. Operability
8. Performance
9. Development convenience

Performance or developer convenience must not compromise security, correctness, or auditability.

---

## Initial Architectural Bias

Unless requirements demonstrate otherwise, begin with:

**Modular monolith + asynchronous workers**

rather than independently deployed microservices.

Maintain explicit module boundaries for:

- Cases
- Workflows
- Tasks
- Documents
- Rules
- Identity / Authorization
- Audit
- Administration
- Integrations

Modules must communicate through defined interfaces rather than reaching arbitrarily into each other's persistence implementation.

These boundaries should permit future extraction if scale, security, ownership, or operational requirements justify it.

---

## Definition of Done

A feature is not complete merely because its primary UI works.

Where applicable, completion requires:

- Domain behavior implemented
- Authorization enforced
- Audit behavior implemented
- Failure behavior considered
- Appropriate automated tests
- Observability added
- Relevant APIs/contracts documented
- Database migrations included
- Sensitive-data handling reviewed
- Acceptance criteria satisfied

Features involving workflows must additionally verify expected transitions, invalid transitions, and applicable failure paths.

---

## Governing Product Principle

> Business processes belong to the organization, not to the software platform that happens to execute them.

Every major architecture and product decision should reinforce that principle.

---

## Azure-Targeted, Local-First Development

Microsoft Azure is the required production deployment target, but development of the core platform must not require access to corporate Azure resources.

The system will initially be developed on a personal development environment where corporate Azure subscriptions, Microsoft Entra tenants, production databases, storage accounts, service buses, secrets, PHI, and other enterprise resources are unavailable.

Architecture must therefore distinguish between:

1. **Domain and application capabilities**
2. **Infrastructure interfaces**
3. **Local development implementations**
4. **Azure production implementations**

Core product functionality must be executable and testable locally without an Azure connection.

### Azure Compatibility Requirement

Infrastructure boundaries must be designed from the beginning so that approved Azure implementations can later replace local implementations without redesigning business logic.

Expected production mappings include:

- Relational persistence → Azure SQL
- Document/object storage → Azure Blob Storage
- Messaging → Azure Service Bus
- Identity → Microsoft Entra ID
- Secrets → Azure Key Vault
- Telemetry → Azure Monitor / Application Insights
- Application hosting → approved Azure compute
- Background processing → approved Azure compute

These are target production capabilities, not dependencies required during initial development.

### Local Development

The local environment may use development substitutes such as:

- Local relational database or containerized SQL-compatible database
- Local filesystem or local object-storage emulator
- In-process or lightweight local messaging
- Development authentication and synthetic users
- Environment variables or local development secret storage
- Local structured logging and telemetry
- Local background workers

Local implementations must conform to the same application-level contracts expected of their future Azure counterparts.

Application and domain code must not contain conditional business behavior such as:

`if local_environment ... else azure_environment ...`

Environment differences belong within infrastructure configuration and adapters.

### Infrastructure Ports and Adapters

External infrastructure must be accessed through explicit interfaces or equivalent architectural boundaries.

Examples include:

- `CaseRepository`
- `DocumentStore`
- `MessageBus`
- `IdentityProvider`
- `SecretProvider`
- `AuditStore`
- `TelemetryProvider`

Application logic depends upon these contracts rather than directly upon Azure SDKs.

Implementations may therefore differ by environment.

Example:

`DocumentStore`
- Local: filesystem implementation
- Production: Azure Blob Storage implementation

`MessageBus`
- Local: in-process/local implementation
- Production: Azure Service Bus implementation

`IdentityProvider`
- Local: development identity provider
- Production: Microsoft Entra ID implementation

The domain layer must remain unaware of which implementation is active.

### Azure Readiness

Local substitutes must not create architectural assumptions incompatible with the intended Azure implementation.

Before selecting a local substitute, verify that its behavior can reasonably represent the semantics required from the intended production service.

Where Azure-specific behavior materially affects correctness, create explicit contracts and tests describing the expected production behavior.

Azure adapters may initially exist as interfaces, scaffolding, configuration models, or unimplemented adapters.

They do not need to connect successfully during personal development.

### Enterprise Integration Boundary

Corporate integrations must be isolated behind adapters.

Examples include:

- OnBase
- Panzura
- Enterprise APIs
- Corporate databases
- Internal identity systems
- Enterprise messaging
- Internal data sources

Development must not assume these systems are available.

Use synthetic data, fixtures, mocks, simulators, or development adapters to represent them.

When the project is moved into the corporate development environment, real adapters can be implemented or enabled without changing the core case-management product.

### Configuration

Environment-specific infrastructure must be selected through configuration and dependency injection or equivalent mechanisms.

The intended transition should resemble:

**Personal development**

`LocalDocumentStore`
`LocalMessageBus`
`DevelopmentIdentityProvider`

becoming:

**Corporate Azure**

`AzureBlobDocumentStore`
`AzureServiceBus`
`EntraIdentityProvider`

without modification to:

- Case-management logic
- Workflow behavior
- Business rules
- Task behavior
- Document-domain behavior
- Audit semantics
- User-facing business capabilities

### Development Priority

Do not block implementation of product capabilities because Azure infrastructure is unavailable.

Build the product locally against production-shaped abstractions.

Implement or activate Azure infrastructure when access to the appropriate corporate Azure environment becomes available.

The objective is:

> **Build locally. Architect for Azure. Connect to Azure later.**

Azure is the deployment target, not a development prerequisite.

---

## Governance

This constitution is the highest-authority product and engineering document for the Case Management Platform.

Changes to principles require explicit review against the Engineering Decision Priority.

The constitution is ratified on initial creation and amended by the documented workflow.

---

## Version

**Version**: 1.0.0
**Ratified**: 2026-08-29
**Last Amended**: 2026-08-29
