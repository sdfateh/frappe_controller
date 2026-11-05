# Frappe Controller domain app

For the detailed administrator walkthrough, see the
[Controller configuration guide with screenshots](docs/CONTROLLER_CONFIGURATION_GUIDE.md)
or its [printable PDF](docs/CONTROLLER_CONFIGURATION_GUIDE.pdf).
A [self-contained HTML edition](docs/CONTROLLER_CONFIGURATION_GUIDE.html) is also available.

This installable app owns the central control-plane records for enrolled server agents,
discovered benches and sites, durable operations, immutable events, and approval
decisions. Its Agent HTTP adapters are bounded POST-only methods authenticated with
Ed25519 request signatures; they expose no arbitrary command,
shell, SQL, private-key, or agent-side interface.

## Supported Frappe versions

Controller requires Frappe/ERPNext v16; v15 is not supported. The standalone local development
stack in the parent workspace's `controller.compose.yml` targets Frappe v16.54.0
and ERPNext v16.53.0 on Python 3.14 and Node 24. Exact upstream commits are reviewed
in `docker/controller/versions.json`; the Bench base image is pinned by digest.

App navigation and approval email links use Frappe v16 `/desk` routes. The Controller app entry follows the same
role and deployment feature-file visibility checks as its Workspace. Test-mode
detection uses `frappe.in_test`.

Controller jobs use the **Controller Operation** DocType and
`/desk/controller-operation/<id>` routes. The `before_migrate` hook moves legacy
Controller jobs, scoped audit references and encrypted credential entries from
the old shared `Operation` schema, then restores ERPNext's manufacturing
`Operation` DocType. Job IDs and link values are preserved. Migration rejects
conflicting job identities or credentials and can resume after an interrupted
schema stage. Back up the site before deploying this naming change.

To recreate or upgrade the standalone development container, first back up its
site database, public/private files, site encryption configuration and protected
Controller config volume. From the parent workspace run:

```sh
docker compose --env-file .env.controller -f controller.compose.yml config --quiet
docker compose --env-file .env.controller -f controller.compose.yml up -d --no-deps --force-recreate frappe
```

Startup verifies the pinned release commits, refuses to replace modified upstream
code, resolves Python/Node dependencies when their fingerprint changes, builds
assets and migrates the existing site before serving requests. The Controller
repository is mounted directly, read/write, at
`/home/frappe/controller-data/frappe-bench/apps/frappe_controller`; startup never
copies or replaces it. The persistent development database and Bench are
retained. A completed dependency fingerprint avoids repeating installation on
normal recreation. Never downgrade a migrated v16 database in place: rollback
uses the matching pre-upgrade database/files/config backup and v15 source.

With developer mode and `bench watch` enabled, repository edits are immediately
visible in the container. Refresh Desk after page JavaScript changes; run
`bench --site controller.kaleam.net clear-cache` if Desk serves a cached page.
Bundled assets are rebuilt by the watcher. Python web changes use the development
reloader; restart affected workers for Python job changes. DocType/schema/patch
changes still need `bench --site controller.kaleam.net migrate`, and dependency
changes need installation (startup checks the dependency fingerprint). Routine UI
edits do not need container recreation. Frappe developer exports also write back
to this repository, so review the Git diff before committing.

This standalone development upgrade does not upgrade an ERP stack hosting the
Controller app. Such stacks have their own reviewed images and migration plan.

## Source data

The Controller ships a native v16 **Sidebar** with direct links to Overview,
Source Data, Site Actions, Operations and Bulk Operations. Fleet opens by default;
Approvals, Reports, Activity & Targets and Administration collapse to keep the
navigation compact. Native permission filtering and personal sidebar preferences
still apply. The blue selection styling is scoped to this sidebar and supports
dark mode, keyboard focus and touch targets. `bench migrate` installs the sidebar
from the app's module JSON; refresh Desk after deployment.

Open **Source Data** in the Controller workspace, or `/desk/source-data`. Configure
**Frappe Controller Settings → Source Data → Source Data API URL** at
`/desk/frappe-controller-settings`. Enter a public HTTPS origin such as
`https://dev-priv-api.kaleam.net`, without `/tires` or `/docs`. Saving applies it
to browsing, autocomplete, creation, edits and deletion on the next request;
no restart is needed. An unset value retains the development origin above.
Only users with write permission on Controller Settings can change it.

Credentials, paths, queries, fragments and local/private destinations are rejected.
Requests keep TLS verification, public DNS/connected-peer checks, disabled redirects
and proxies, and no automatic mutation retries. Source Data shows the active origin
and links to its API documentation. Review dialogs display their target, and a URL
change requires old forms to be reopened before saving. Read caches, record snapshots,
locks and audit/replay payloads are bound to the origin. The reviewed create-field
contract is maintained in `frappe_controller/source_data_schema.json`.

**Vehicles & brands** provides dependent brand/model/year/body/engine lookups.
The catalog workspace presents lookup values as searchable cards with breadcrumb
navigation. Record lists show the fields most useful for scanning; choose a vehicle
brand, tire name or color name in a row to open every returned field in **Record
details**. Compact category tabs and controls keep records near the top of the page;
filters start collapsed and expand on demand. Selected records appear in a dedicated
bulk-action bar. On smaller screens, record rows stack into cards. Light/dark themes,
keyboard focus, inline connection errors and loading placeholders use the existing
Desk environment.
**Vehicle records**, **Tires** and **Colors** use the new `/get/list` routes with
server pagination, `X-Total-Count`, and allowlisted exact-match filters. Text
search covers loaded results. Tire and color records are available through these
new routes even when legacy list routes fail or return empty lists.
Custom brand/model names containing slashes, backslashes, percent signs or dot
segments use exact-match `/vehicle/get/list` query filters for dependent lookups.
These lookups deduplicate model/year suggestions and fail visibly if the complete
result exceeds 10,000 records, 2 MB of decoded JSON or the 20-second read budget.

For catalog-only staff, assign **Source Data Manager** to a **System User** without
any Controller roles. Their app entry opens Source Data directly; the Controller
sidebar exposes only the catalog. They can browse, add, edit and delete single or
bulk records through the same validated and audited API. They cannot access
Controller settings, fleet records, operations, approvals or reports. The settings
shortcut is governed separately by write permission on Controller Settings.
Roles are additive: any other roles assigned to the user keep their own access.
The Controller deployment feature gate still applies to catalog access.

**Source Data Manager**, **Controller Admin** and **System Manager** users can add a single record or bulk
add up to 100 rows using the grid or a UTF-8 CSV (64 KB maximum). Each catalog's
form and CSV template use its required and optional fields. Vehicle specifications
are supported; colors require English and Arabic names; tire dimensions enforce
the documented numeric limits. Review the full list before confirming.

Vehicle **Brand** and **Model** fields provide searchable suggestions in single
add/edit forms and bulk-grid row editors. Models are scoped to the current brand;
changing or clearing a brand immediately clears its model and previous suggestions.
Opening an existing or imported row preserves its saved values. Lookups use the
same authenticated server facade and one-minute cache; free-text values and required
fields are unchanged. Loading, empty and failure states appear beside each field;
focus a failed field to retry, or enter a value manually. An explicit **Use “…”
(new value)** option keeps a typed value that is absent from the list; only the
value itself is saved. Arrow keys and Enter select suggestions, which display
source values as plain text.

**Edit ID** opens a fresh vehicle, tire or color record with every field supported
by that catalog's update API. Vehicle brands, models and specifications are edited
under **Vehicle records**. IDs and system/computed fields stay read-only. Review
the before/after values, then choose **Save changes**. Only changed fields are sent;
clearing a nullable field sends `null`. Unchanged numeric formatting does not
trigger an update. The Controller validates partial updates and checks the fresh
record against the reviewed snapshot before the fixed `PUT /{catalog}/edit/{id}`
call. Changed records must be reopened and reviewed. Edit and delete attempts for
the same record share a lock, and edits use the existing audit/replay safeguards.
The API has no conditional-update contract: the final read/PUT pair cannot prevent
an external change in the interval between those two calls.

Record lists expose **Delete ID** and **Delete selected**. Select records on the
current page and review the explicit IDs, then type `DELETE` to confirm. Lookup
values are never treated as deletion targets. Selection clears on page, filter,
category and text-search changes. Before each deletion the Controller re-reads
the record and compares its reviewed snapshot. Changed records require another
review. HTTP 409 linked-record rejections are reported without forced deletion.
The API does not document conditional/transactional deletes, so the final
read/delete pair is not an atomic compare-and-delete operation.

All mutations use CSRF-protected Controller POST methods. Only the fixed upstream
`/{catalog}/create`, `/{catalog}/edit/{id}` and `/{catalog}/delete/{id}` routes are used for writes. Fields,
record IDs, snapshot hashes, row counts, numeric limits and request IDs are
validated server-side. The origin, action, catalog, actor and payload are bound
to each request ID. Intent and outcome are stored in **Integration Request**
logs before and after each attempt; replay returns the saved outcome while that
log is retained (Frappe's standard log retention applies).

Rows run sequentially and report their individual results. Stop remaining rows
leaves completed changes intact. Unacknowledged writes stop the batch and are
never automatically retried. Check the source before another attempt; the API
has no idempotency or batch transaction contract. Existing records matching all
submitted fields are skipped before addition. Successful or uncertain mutations
invalidate the browsing cache.

The server makes bounded HTTPS calls to allowlisted routes, with verified TLS,
connection/read timeouts, disabled redirects, schema checks, and response-size
limits. The current API documents no authentication. Browsing requires a
Controller role and enabled workspace rollout; mutations additionally require
an administrative role. Successful reads are cached per site for 60 seconds,
with origin and query included in the cache identity. Browser rendering treats
source values as text.

## Add a managed server

### Existing production ERP Controller deployment

`erp.kaleam.net` hosts Controller in the existing ERP image and database. The
repository-owned `deployment/erp-controller.override.yml` mounts the protected
external `controller_controller-config` volume read-only into backend, workers,
and scheduler. Keep that volume: it contains the enrollment pepper and policy.
The site's feature-file setting is `/run/controller-config/feature-flags.json`.

Build the corrected ERP image from this repository root before recreating ERP:

```sh
docker build -f deployment/erp-controller.Dockerfile -t frappe-controller-erp:v16-bulk-layout-20261031 .
```

The overlay uses that local image for every ERP application service, including
the configurator, so schema migration cannot reload the retired Controller
`Operation` definition. It replaces the Controller package completely and builds
its assets; the upstream ERP apps stay pinned to the deployed v16 base digest.
The image also applies the reviewed Whitelabel onboarding compatibility patch:
legacy `Watch Video` steps become v16 `View Docs` links while retaining their
video destinations and completion state. Whitelabel updates only its presentation
fields directly, avoiding a full document save that would validate unrelated
references to DocTypes removed by the v16 upgrade.
Back up the site database, files and configuration before migration. Stop workers
and scheduler, recreate backend with `--renew-anon-volumes`, run
`bench --site erp.kaleam.net migrate` in backend, then recreate frontend,
websocket, workers and scheduler with `--renew-anon-volumes` to refresh assets.
Run `bench --site erp.kaleam.net execute frappe_controller.tests.v16_smoke.verify`
in the recreated backend to verify both ERP and Controller schemas. For a
subsequent reviewed build, set `ERP_CONTROLLER_IMAGE` to its local image tag.

If a v16 worker exits with RQ `parse_composite_key` errors from old `site::...`
job IDs, inspect expired legacy started-job entries with:

```sh
docker exec -i erp-backend-1 env/bin/python - --site erp.kaleam.net < deployment/archive_legacy_rq_entries.py
```

Repeat with `--apply` after inspection, then start the affected worker. The script
archives only expired started-job pointers for that site whose workers are gone;
it preserves job records and original scores under
`frappe:v16-upgrade:legacy-started:rq:wip:*`, and never clears queues or retries jobs.
It skips modern `site::job:execution` members, ambiguous legacy IDs containing
colons, and pointers without a surviving job hash. These require separate inspection.

Run `bash scripts/install-erp-controller-overlay.sh` on this host to install the
default `/deploy/erp/compose.override.yml` symlink. For explicit Compose commands,
use `bash scripts/erp-controller-compose.sh up -d --no-deps backend queue-short
queue-long scheduler frontend websocket` from this repository. Passing only
`-f /deploy/erp/compose.yml` bypasses the overlay and is **not** a complete Controller
deployment. CI/deployment commands must include the overlay or use the wrapper.

The Agent's active installed deployment is `/opt/frappe-deploy-agent/compose.yml`;
its repository source is `deploy/agent-controller.installed.yml` in the parent
deploy-agent workspace. It uses a separate `frappe-deploy-agent_erp_controller_agent_data`
volume. The old Controller, old spool, and old job history remain for recovery;
its `server-01` record is disabled. Do not enable both Controllers for that Agent.

Only Controller settings were transferred, using `scripts/move_controller_settings.py`.
Password fields were decrypted in memory and re-encrypted with ERP's existing key;
ERP customers, users, installed apps, and database were not replaced. Inventory is
reported anew by the enrolled Agent. The disabled production-target placeholder
and test operations from the old Controller were not imported.

The Controller switch does not upgrade the Agent or Host Helper. Customer creation
requires their readiness-capable versions and the bench policy
`required_apps: [frappe, erpnext, mos_pro]`. Empty policy fails closed. Production
operation feature flags remain disabled; hosting Controller on production ERP
does not make the development Agent a production target.

1. Open **Frappe Controller Settings** at
   `/desk/frappe-controller-settings`. Set **Public Controller URL** to the
   normal public HTTPS site address and set **Agent Image Reference** to the
   reviewed immutable value `repository@sha256:<64 hex>`.
2. Create a **Server Agent**, choose **Apply Setup Profile**, fill **Allowed Site
   Suffixes JSON**, and review/save the operation and setup app lists. Profiles
   cover inventory/verification, backup/verification, or Customer ERP creation;
   they do not enable environment feature flags or bypass approvals.
3. On that record, click **Generate Install Token**.
4. Run the displayed command from the reviewed `host_helper` checkout, paste the
   token, review the discovered configuration, and provide the local database
   credential. Setup checks local health, the exact image and signed Controller
   heartbeat/Bench inventory before reporting completion.

The token expires after 10 minutes by default. Setup generates the private key
on the managed server and never sends it to the Controller.
Interrupted setup retains its protected identity and can resume with the same
command without another token after enrollment succeeds. Completed setup only
checks health when repeated. Use the upgrade workflow for existing installations.
Deploy/migrate this Controller release before the updated installer; new clients
request bootstrap contract `1.1` with required apps and use the signed read-only
installation status route. Legacy bootstrap requests retain contract `1.0`.

After a site-create operation succeeds, use **Retrieve Administrator Password**
on its **Controller Operation** record. The credential is delivered separately over signed HTTPS,
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
**Frappe Controller Settings** document at `/desk/frappe-controller-settings` and
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
