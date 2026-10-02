/* Read-only projection of Agent events. No execution or state mutations. */
(function () {
  "use strict";
  const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
  const terminal = new Set(["succeeded", "failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"]);
  const create = ["preflight_reserve", "dns", "route", "new_site", "scheduler_enable", "verify", "complete"];
  const restore = ["preflight", "pre_operation_backup", "maintenance_on", "scheduler_off", "resolve_manifest", "restore", "migrate", "verify", "scheduler_on", "maintenance_off"];
  const fromBackup = ["preflight_reserve", "resolve_manifest", "prefetch_manifest", "dns", "route", "new_site", "restore", "migrate", "scheduler_enable", "verify", "complete"];
  const plans = {
    "site.create_blank": create, "site.create": fromBackup, "site.create_from_backup": fromBackup,
    "site.restore": restore, "site.reinstall": restore,
    "site.backup": ["preflight", "backup", "verify"],
    "site.migrate": ["preflight", "maintenance_on", "scheduler_off", "migrate", "verify", "scheduler_on", "maintenance_off"],
    "site.delete": ["preflight", "pre_operation_backup", "maintenance_on", "scheduler_off", "verify", "quarantine"],
    "site.scheduler.enable": ["preflight", "scheduler_on", "verify"],
    "site.scheduler.disable": ["preflight", "scheduler_off", "verify"],
    "site.maintenance.enable": ["preflight", "maintenance_on", "verify"],
    "site.maintenance.disable": ["preflight", "maintenance_off", "verify"],
    "site.config.update": ["preflight", "config_update", "verify"],
  };
  const labels = {
    preflight_reserve: __("Check target and reserve site"), preflight: __("Check prerequisites"),
    resolve_manifest: __("Locate backup"), prefetch_manifest: __("Download backup"),
    dns: __("Create DNS record"), route: __("Create routing configuration"), new_site: __("Create site"),
    restore: __("Restore backup"), migrate: __("Migrate site"), verify: __("Verify site"), complete: __("Complete provisioning"),
    scheduler_enable: __("Enable scheduler"), scheduler_on: __("Enable scheduler"), scheduler_off: __("Disable scheduler"),
    maintenance_on: __("Enable maintenance mode"), maintenance_off: __("Disable maintenance mode"),
    backup: __("Create backup"), pre_operation_backup: __("Create safety backup"),
    config_update: __("Update site configuration"), quarantine: __("Quarantine site"),
    ensure_apps: __("Install required applications"), required_apps: __("Verify required applications"),
    public_https: __("Verify public DNS, TLS and HTTP"), handover: __("Deliver Administrator access"),
    restore_access: __("Restore Administrator access after backup restore"),
  };
  const statuses = {
    pending: __("Pending"), not_reached: __("Not reached"), unreported: __("Not reported"),
    running: __("Running"), completed: __("Completed"), compensated: __("Rolled back"),
    stopped: __("Stopped here"), failed: __("Failed here"), retrying: __("Waiting for retry"),
    resumed: __("Previously processed"),
  };
  function details(event) {
    try { const value = JSON.parse(event.details_json || "{}"); return value && typeof value === "object" ? value : {}; }
    catch (_) { return {}; }
  }
  function stepName(event) {
    const value = event.step || details(event).step;
    return typeof value === "string" ? value : "";
  }
  function model(doc, input) {
    const events = [...input].sort((a, b) => Number(a.sequence) - Number(b.sequence));
    const rows = new Map();
    const ensure = (name) => {
      if (!rows.has(name)) rows.set(name, {name, label: Object.hasOwn(labels, name) ? labels[name] : name, status: "pending", events: [], started: null, ended: null, attempt: null});
      return rows.get(name);
    };
    const knownPlan = Object.hasOwn(plans, doc.operation_type);
    const declared = events.findLast((event) => event.kind === "plan.persisted" && Array.isArray(details(event).steps));
    const declaredSteps = declared && details(declared).steps;
    (declaredSteps && declaredSteps.length <= 128 && declaredSteps.every((step) => typeof step === "string")
      ? declaredSteps : (knownPlan ? plans[doc.operation_type] : [])).forEach(ensure);
    let active = null, stopped = null, finalState = null, finalError = null, retrying = false;
    for (const event of events) {
      const data = details(event);
      if (event.kind === "execution.started") {
        active = null; stopped = null; finalState = null; finalError = null; retrying = false;
      }
      const name = stepName(event);
      const isStep = event.kind.startsWith("step.") && Boolean(name);
      let row = isStep ? ensure(name) : (name ? rows.get(name) : null);
      if (!row && event.kind.includes(".")) row = rows.get(event.kind.split(".")[0]);
      if (row) row.events.push(event);
      if (isStep) {
        if (event.attempt !== null && event.attempt !== undefined) row.attempt = event.attempt;
        if (event.kind === "step.started") {
          row.status = "running"; row.started = event.agent_created_at; row.ended = null;
          active = name; stopped = null; retrying = false;
        } else if (event.kind === "step.completed") {
          row.status = "completed"; row.ended = event.agent_created_at;
          if (active === name) active = null;
        } else if (event.kind === "step.compensated") {
          row.status = "compensated"; row.ended = event.agent_created_at;
          // Compensation of an earlier step must not move the failure marker.
        } else if (event.kind === "step.resumed") {
          if (row.status !== "completed" && row.status !== "compensated") row.status = "resumed";
        } else if (event.kind === "step.failed") {
          row.status = "failed"; row.ended = event.agent_created_at; stopped = name;
        }
      }
      if (event.kind === "execution.retry_scheduled") {
        retrying = true;
        if (active) rows.get(active).status = "retrying";
      }
      if (event.kind === "execution.finished") {
        finalState = data.state; finalError = data.error_code;
      }
    }
    // Execution events precede the credential/result handoff. A local success
    // event is not Controller acknowledgement and must never imply site ready.
    const state = terminal.has(doc.state) ? doc.state : (finalState && finalState !== "succeeded" ? finalState : doc.state);
    const ended = terminal.has(state);
    if (ended && state !== "succeeded" && active && rows.get(active).status !== "compensated") {
      stopped = active;
      rows.get(active).status = state === "failed" ? "failed" : "stopped";
      rows.get(active).ended = doc.completed_at || events.findLast((e) => e.kind === "execution.finished")?.agent_created_at;
    }
    // Success alone is not evidence that every individual step event arrived.
    const ordered = [...rows.values()];
    const stopIndex = ordered.findIndex((row) => row.name === stopped);
    for (const [index, row] of ordered.entries()) {
      if (row.status === "pending" && ended) row.status = stopped && index > stopIndex ? "not_reached" : "unreported";
      if (row.status === "running" && state === "succeeded") row.status = "unreported";
    }
    return {
      rows: [...rows.values()], events, state, stopped, active: ended ? null : active,
      error: doc.error_code || finalError, retrying: !ended && retrying,
      deliveryFailure: events.findLast((e) => e.kind === "delivery.controller_failed"),
      knownPlan,
      awaitingAcknowledgement: finalState === "succeeded" && !terminal.has(doc.state),
    };
  }
  function eventHTML(event) {
    let formatted = event.details_json || "";
    try { formatted = JSON.stringify(JSON.parse(formatted), null, 2); } catch (_) {}
    return `<div class="fc-timeline-event"><strong>#${esc(event.sequence)} · ${esc(event.kind)}</strong>
      <span class="text-muted">${esc(event.agent_created_at)}</span>
      ${event.attempt !== null && event.attempt !== undefined ? `<span>${esc(__("Attempt"))}: ${esc(event.attempt)}</span>` : ""}
      ${formatted ? `<pre>${esc(formatted)}</pre>` : ""}</div>`;
  }
  function handoverHTML(doc, handover) {
    if (!["site.create", "site.create_blank", "site.create_from_backup"].includes(doc.operation_type)) return "";
    let evidence = {};
    try { evidence = JSON.parse(doc.result_json || "{}").result?.readiness || {}; } catch (_) {}
    const versioned = evidence.version === 1;
    const checks = [
      [__("Required apps installed and verified"), versioned && evidence.apps_verified === true],
      [__("Public DNS, TLS and Frappe HTTP verified"), versioned && evidence.public_https_verified === true],
      [__("Administrator access received by Controller"), Boolean(doc.credential_received_at)],
    ];
    if (handover?.customer_required) checks.push([__("Customer linked to Managed Site"), handover.customer_linked === true]);
    const ready = handover?.ready === true;
    return `<div class="fc-timeline-handover"><strong>${esc(ready ? __("Site ready for handover") : __("Site handover is not yet verified"))}</strong>
      <ol>${checks.map(([label, complete]) => `<li class="${complete ? "text-success" : "text-warning"}">${esc(complete ? "✓" : "○")} ${esc(label)} — ${esc(complete ? __("Verified") : __("Awaiting evidence"))}</li>`).join("")}</ol>
      ${versioned && Array.isArray(evidence.required_apps) ? `<p>${esc(__("Required apps"))}: ${esc(evidence.required_apps.join(", "))}</p>` : ""}
      ${!handover || handover.unavailable ? `<small>${esc(__("Customer handover status is unavailable; readiness is not assumed."))}</small>` : ""}
      ${doc.state === "succeeded" && !versioned ? `<p class="text-warning">${esc(__("Legacy execution succeeded without the new readiness checks. It is not proof of a usable public site."))}</p>` : ""}
      ${doc.error_code === "site_handover_unverified" ? `<p class="text-warning">${esc(__("The Agent reported completion without readiness evidence. Delivery was acknowledged, but this site requires manual verification before handover. Do not recreate it without checking existing resources."))}</p>` : ""}
    </div>`;
  }
  function render(doc, events, {truncated = false, handover = null} = {}) {
    const value = model(doc, events);
    const position = value.rows.findIndex((row) => row.name === (value.stopped || value.active));
    let heading = __("Waiting for execution");
    if (value.state === "awaiting_approval") heading = __("Waiting for approval");
    if (value.state === "succeeded") heading = handover?.ready ? __("Site ready for handover") : __("Operation succeeded");
    else if (value.stopped) heading = __("Stopped at step") + ` ${position + 1}/${value.rows.length} — ${value.rows[position].label}`;
    else if (terminal.has(value.state)) heading = __("Operation stopped — no failing step reported");
    else if (value.retrying) heading = __("Waiting for automatic retry");
    else if (value.active) heading = __("Running step") + ` ${position + 1}/${value.rows.length} — ${value.rows[position].label}`;
    else if (value.awaitingAcknowledgement) heading = __("Execution finished — awaiting Controller acknowledgement");
    else if (value.state === "running") heading = __("Running — waiting for step events");
    const rows = value.rows.map((row, index) => {
      const focus = row.name === (value.stopped || value.active);
      const icon = row.status === "completed" ? "✓" : row.status === "compensated" ? "↶" : focus ? (row.status === "running" ? "▶" : "!") : index + 1;
      return `<li class="fc-timeline-step" data-step="${esc(row.name)}" data-status="${esc(row.status)}" ${focus ? 'aria-current="step"' : ""}>
        <span class="fc-timeline-marker" aria-hidden="true">${esc(icon)}</span>
        <details ${focus ? "open" : ""}><summary><span class="fc-timeline-label">${esc(row.label)}</span>
          <span class="fc-timeline-badge">${esc(statuses[row.status])}</span></summary>
          <div class="fc-timeline-details"><code>${esc(row.name)}</code>
          ${row.started ? `<div>${esc(__("Started"))}: ${esc(row.started)}</div>` : ""}
          ${row.ended ? `<div>${esc(__("Last update"))}: ${esc(row.ended)}</div>` : ""}
          ${row.name === value.stopped && value.error ? `<p class="text-danger">${esc(value.error)}</p>` : ""}
          ${row.events.length ? row.events.map(eventHTML).join("") : `<p class="text-muted">${esc(__("No events received for this step."))}</p>`}
          </div></details></li>`;
    }).join("");
    return `<section class="fc-operation-timeline" aria-label="${esc(__("Execution timeline"))}">
      <div class="fc-timeline-summary" role="status"><strong>${esc(heading)}</strong>
        <div>${esc(__("Status"))}: ${esc(value.state || "unknown")}</div>
        ${value.error ? `<div class="text-danger">${esc(value.error)}</div>` : ""}
        <div class="text-muted">${value.rows.filter((r) => r.status === "completed").length}/${value.rows.length} ${esc(__("steps completed"))}
        · ${value.rows.filter((r) => r.status === "compensated").length} ${esc(__("rolled back"))}</div>
        ${value.stopped ? `<small>${esc(__("Stop location is based on the last active or failed step reported by the Agent."))}</small>` : ""}
      </div>
      ${handoverHTML(doc, handover)}
      ${truncated ? `<p class="text-warning">${esc(__("Event history is truncated. The timeline may not show the final stop location."))}</p>` : ""}
      ${value.deliveryFailure ? `<details><summary class="text-warning">${esc(__("Controller delivery error was reported"))}</summary>${eventHTML(value.deliveryFailure)}</details>` : ""}
      ${value.knownPlan ? `<p class="text-muted fc-timeline-caption">${esc(__("Expected workflow; step status comes from Agent events. Expand a step for details."))}</p>` : ""}
      ${rows ? `<ol class="fc-timeline">${rows}</ol>` : `<p class="text-muted">${esc(__("No step events received yet."))}</p>`}
      <details class="fc-timeline-raw"><summary>${esc(__("All events"))} (${value.events.length})</summary>${value.events.map(eventHTML).join("")}</details>
    </section>`;
  }
  frappe.controller_operation_timeline = Object.freeze({model, render, terminal});
})();
