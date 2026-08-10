# Frappe Controller domain app

This installable app owns the central control-plane records for enrolled server agents,
certificate metadata, discovered benches and sites, durable operations, immutable
events, and approval decisions. Its HTTP adapters are bounded POST-only methods
intended to sit behind the trusted mTLS proxy; they expose no arbitrary command,
shell, SQL, private-key, or agent-side interface.

## Security invariants

- Stable agent, bench, site, operation, target, event, and certificate identities are immutable.
- Operation events and approval decisions are append-only and cannot be deleted.
- Controller users receive least-privilege roles: `Controller Admin`, `Operator`, `Approver`, and `Auditor`.
- An operation requester cannot approve their own operation. Distinct approvers are enforced per operation.
- Certificate records contain public metadata only; private keys remain on agents.
- Operations cannot enter the queue until their configured approval threshold is satisfied.
- Lifecycle operations are created only by the authenticated
  `frappe_controller.api.operations.create_operation` method. Data updates use
  the separate `preview_data_update` and `promote_data_update` methods; actual
  mutations are immutable descendants of a successful dry-run result.
- Approval decisions snapshot the payload, target, and data-update preview
  evidence hashes. Dispatch also rejects changed inventory snapshots.
- Inventory and operation ownership links must agree across agent, bench, and site records.
- Deployment-owned feature flags default every new action off. The same strict
  root-owned JSON controls Desk visibility, authoring, approval, bulk fan-out,
  and command leasing per environment; hiding UI is never the authorization
  boundary. See `docs/CONTROLLER_FEATURE_FLAGS.md`.

Service-layer protocol ingestion must use transactions and `ignore_permissions=True`
only after mTLS identity and protocol validation. Normal role permissions never allow
users to rewrite immutable event or approval history.

Controller database/files/configuration and external CA/pepper recovery are
covered by `docs/CONTROLLER_DISASTER_RECOVERY.md`. A release must run that real
restore drill and pass the bounded `scripts/controller_dr_evidence.py` evidence
gate; agent SQLite recovery is a separate procedure.

Real enrollment uses a protected CA key and token pepper through
`FrappeEnrollmentRuntime`, while `FrappeCertificateStore` persists single-use
token, issuance, overlap, and revocation state transactionally. See
`docs/CONTROLLER_ENROLLMENT.md` for the CSR profile, secret files, proxy
boundary, and deployment procedure.

The database-backed disposable-site gate verifies app installation, fixtures,
transactional repositories, dispatch, certificate lifecycle, roles, reports,
and Workspace loading. Run it for every release as documented in
`docs/CONTROLLER_OPERATIONS.md`; a passing dependency-free suite alone is not a
deployment gate.
