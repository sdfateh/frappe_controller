# Frappe Controller domain app

This installable app owns the central control-plane records for enrolled server agents,
discovered benches and sites, durable operations, immutable events, and approval
decisions. Its HTTP adapters are bounded POST-only methods authenticated with
Ed25519 request signatures; they expose no arbitrary command,
shell, SQL, private-key, or agent-side interface.

## Add a managed server

1. Open **Frappe Controller Settings** at
   `/app/frappe-controller-settings`. Set **Public Controller URL** to the
   normal public HTTPS site address and set **Agent Image Reference** to the
   reviewed immutable value `repository@sha256:<64 hex>`.
2. Create a **Server Agent** and fill **Allowed Site Suffixes JSON** and
   **Allowed Operations JSON**.
3. On that record, click **Generate Install Token**.
4. Run the displayed command on the managed server and paste the token once.

The token expires after 10 minutes by default. Setup generates the private key
on the managed server and never sends it to the Controller.

After a site-create operation succeeds, use **Retrieve Administrator Password**
on its **Operation** record. The credential is delivered separately over signed HTTPS,
stored in an encrypted Password field, and cleared after the first retrieval.

## Controller-owned Cloudflare DNS

Cloudflare credentials belong only on the Controller site. Open the single
**Frappe Controller Settings** document at `/app/frappe-controller-settings` and
set **Cloudflare API Token** and **Cloudflare Zone ID**. Both are encrypted
Password fields. **Proxy DNS Records** defaults to off.

For each **Server Agent** document, set **Public IPv4 Address** to that managed
server's public address. Site-creation workers send only the operation identity
and domain to fixed Controller endpoints. The Controller verifies ownership,
creates the A record, and returns its exact record ID for durable compensation.
Target servers do not receive a Cloudflare token or zone ID.

## Controller-owned AWS/S3 access

AWS credentials and S3 policy also live in **Frappe Controller Settings**.
**AWS Access Key ID** and **AWS Secret Access Key** are encrypted Password
fields. Leave both empty to use the Controller server's IAM role/default AWS
credential chain (recommended on AWS; an IAM role is attached to the instance
or task, not authorized by source IP). The region defaults to `us-east-1`, presigned URLs to 300
seconds, maximum object size to 20 GiB, maximum restore size to 40 GiB, and the
allowed-prefix JSON list to `[""]`. Set the default S3 bucket; an additional
bucket allowlist is optional.

The target Agent retains no AWS credentials or S3 configuration. For
an active restore operation, the Controller validates the requested bucket,
prefix, key, and immutable version, then returns a five-minute presigned GET
URL. The Agent validates the S3 checksum header and exact byte count. AWS access
keys are never sent to or stored on target servers.

## Security invariants

- Stable agent, bench, site, operation, target, and event identities are immutable.
- Operation events and approval decisions are append-only and cannot be deleted.
- Controller users receive least-privilege roles: `Controller Admin`, `Operator`, `Approver`, and `Auditor`.
- An operation requester cannot approve their own operation. Distinct approvers are enforced per operation.
- Agent signing private keys remain only on managed servers.
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
only after request-signature and protocol validation. Normal role permissions never allow
users to rewrite immutable event or approval history.

Controller database/files/configuration and token-pepper recovery are
covered by `docs/CONTROLLER_DISASTER_RECOVERY.md`. A release must run that real
restore drill and pass the bounded `scripts/controller_dr_evidence.py` evidence
gate; agent SQLite recovery is a separate procedure.

Enrollment uses a protected token pepper. `FrappeCertificateStore` persists the
single-use token and pinned Ed25519 public key transactionally. See
`docs/CONTROLLER_ENROLLMENT.md` for the signing boundary and deployment procedure.

The database-backed disposable-site gate verifies app installation, fixtures,
transactional repositories, dispatch, signing-key enrollment, roles, reports,
and Workspace loading. Run it for every release as documented in
`docs/CONTROLLER_OPERATIONS.md`; a passing dependency-free suite alone is not a
deployment gate.
