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

## Customer site creation

Saved Customers expose **Create → Site** to users with the **Controller Admin**
or **Operator** role. Select an enabled Server Agent and one of its enabled
Benches. Development, staging, and production targets are all supported; the
Agent and Bench must still belong to the same environment. The target's feature
flags, operation capabilities, domain policy, and approval rules still apply.
Production operations require a matching Approval Policy; a non-destructive
development/staging create can proceed without one if no matching policy exists.

This action creates a new site and installs the apps in the Agent's administrator-owned
Bench `required_apps` policy; it does not restore a template. The selected development
target uses `frappe`, `erpnext`, and `mos_pro`. It requires `inventory` and `restore_and_reinstall` enabled for
the target environment, plus the Controller DNS configuration below. Each
Customer retains one managed-site link. The historical API endpoint
`create_production_site` and field `controller_production_managed_site` remain
unchanged for compatibility; the displayed field is now **Managed Site**.

Creation first saves **Site Creation Operation**, a link to the already-created
Operation. **Managed Site** stays empty until that operation succeeds and Agent
inventory reports the domain on the expected Agent and Bench. Success additionally
requires verified required apps, public HTTPS, and acknowledged encrypted credential
delivery. A once-per-minute
scheduler job resolves the link, regardless of whether the result or inventory
arrives first. It scans at most 100 candidates per run using a site-scoped cache
cursor and wraps after each sweep. Historical jobs without readiness evidence
cannot permanently occupy the first batch; cache loss safely restarts the scan.
Duplicate submissions return the same operation; failed or
intervention-required requests remain visible through **Site Operation** instead
of silently creating another job. Both Customer fields are service-controlled.

**Check Readiness** runs read-only checks before creation: Agent ready/not draining
with a heartbeat within 120 seconds; healthy Agent and Bench inventory within 300
seconds; target, feature, domain and approval policy; required-app policy and version
evidence; active Cloudflare zone/read access without an existing address/alias record;
and reachable public IPv4 port 443. Missing evidence blocks creation, including Agents
that have not yet reported their provisioning policy. Blank-site creation through
the generic authoring, approval-start and retry APIs enforces the same checks; duplicate
Customer submissions/retries keep returning the existing operation.

The DNS check does not write a record or prove DNS edit permission. The ingress check
only proves a listener is reachable, not that a future hostname routes correctly.
Agent preflight rechecks local routing before effects, and the final public HTTPS
check gates success. Policy and readiness are rechecked during execution. If the Bench
required-app policy changes while a job runs, Controller rejects success whose app
evidence differs from the current policy; review this drift instead of bypassing it.
Existing completed operation history is unchanged; old success without handover
evidence does not newly link a Customer.

When a pre-upgrade Agent first delivers an already-completed creation result with
no `readiness` evidence, Controller acknowledges and preserves its exact result
and hash, closes the lease, and marks the operation and its targets
`needs_intervention` with `site_handover_unverified`. The timeline explains that
manual verification is required. This is not a successful handover and never
automatically links a Customer or reruns creation. Replayed results remain
idempotent; conflicting terminal results remain rejected. Explicit but invalid
readiness evidence still fails the gate, as does a new-format success awaiting
credential receipt. Do not recreate a legacy site without inspecting its existing
resources. The handover endpoint reads the required-app policy from the linked
Bench and fails closed if that policy is missing or mismatched.

## Retrying an operation

The Operation's **Execution Timeline** shows the expected lifecycle steps with
event-derived status: pending, running, completed, rolled back, stopped, or not
reached. Expand each step for timestamps and event details; **All events** retains
the raw chronological history. Stop location follows the last active/failed step,
not a later cleanup event. Missing step evidence is shown as not reported, never
assumed successful. Active forms refresh every ten seconds; **Refresh Timeline**
also reloads the current record. No Agent state is changed by this display.

The requester (Operator) or a Controller Admin can click **Retry** on a finished,
unsuccessful lifecycle Operation. Queued, leased, running, awaiting-approval, and
successful operations are not retried. A retry creates a new immutable operation
with **Retry Of Operation** pointing to its predecessor, fresh command identity,
and current feature, capability, and approval checks. Duplicate clicks return
the same successor. Customer site-creation tracking follows that successor.

For `needs_intervention` or `timed_out`, explicitly confirm that the old job has
stopped and partial changes have been reviewed/recovered. This is not automatic
cleanup or a resume-from-step feature: the new operation reruns its workflow.
Creation retries are rejected if the domain already exists in inventory. Bulk
children and typed data updates must use their dedicated retry/preview workflows.

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
