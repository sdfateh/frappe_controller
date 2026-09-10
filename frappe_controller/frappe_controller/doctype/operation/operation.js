const escape = (value) => frappe.utils.escape_html(String(value ?? ""));

function formatDetails(value) {
  if (!value) return "";
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch (_) {
    return String(value);
  }
}

function eventStep(event) {
  if (event.step) return event.step;
  try {
    const details = JSON.parse(event.details_json || "{}");
    return typeof details.step === "string" ? details.step : "";
  } catch (_) {
    return "";
  }
}

const stepLabels = {
  preflight_reserve: __("Preflight Complete"),
  resolve_manifest: __("Backup Located"),
  prefetch_manifest: __("Backup Downloaded"),
  dns: __("DNS Created"),
  route: __("Route Created"),
  new_site: __("Site Created"),
  restore: __("Backup Restored"),
  migrate: __("Site Migrated"),
  scheduler_enable: __("Scheduler Enabled"),
  verify: __("Site Verified"),
  complete: __("Completed"),
};

function renderMilestones(events) {
  const completed = new Set(
    events.filter((event) => event.kind === "step.completed").map(eventStep)
  );
  const milestones = Object.entries(stepLabels)
    .filter(([step]) => completed.has(step))
    .map(([, label]) => "<span class=\"badge badge-success\">&#10003; " + escape(label) + "</span>");
  const failure = [...events].reverse().find(
    (event) => event.kind === "delivery.controller_failed"
  );
  if (failure) {
    let details = {};
    try {
      details = JSON.parse(failure.details_json || "{}");
    } catch (_) {}
    const status = details.http_status ? ", HTTP " + escape(details.http_status) : "";
    milestones.push(
      "<span class=\"badge badge-danger\">" + escape(__("Controller delivery failed")) +
      ": " + escape(details.channel || "unknown") + " (" +
      escape(details.controller_error || "unknown") + status + ")</span>"
    );
  }
  return milestones.length
    ? "<div class=\"frappe-controller-milestones\" style=\"display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px\">" + milestones.join("") + "</div>"
    : "";
}

async function loadJobLog(frm) {
  const field = frm.get_field("job_log");
  if (!field || !frm.doc.name) return;
  const operation = frm.doc.name;
  field.$wrapper.html(`<p class="text-muted">${__("Loading job log…")}</p>`);
  const response = await frappe.call({
    method: "frappe.client.get_list",
    args: {
      doctype: "Operation Event",
      filters: { operation },
      fields: ["sequence", "attempt", "step", "kind", "details_json", "agent_created_at"],
      order_by: "sequence asc",
      limit_page_length: 200,
    },
  });
  if (frm.doc.name !== operation) return;
  const events = response.message || [];
  if (!events.length) {
    field.$wrapper.html(`<p class="text-muted">${__("No Agent events received yet.")}</p>`);
    return;
  }
  const rows = events.map((event) => {
    const details = formatDetails(event.details_json);
    const step = eventStep(event);
    return `<div class="frappe-controller-job-log-entry">
      <strong>#${escape(event.sequence)} · ${escape(event.kind)}</strong>
      <span class="text-muted">${escape(event.agent_created_at)}</span>
      ${step ? `<div>${__("Step")}: ${escape(step)}</div>` : ""}
      ${event.attempt !== null && event.attempt !== undefined ? `<div>${__("Attempt")}: ${escape(event.attempt)}</div>` : ""}
      ${details ? `<pre>${escape(details)}</pre>` : ""}
    </div>`;
  }).join("");
  field.$wrapper.html(renderMilestones(events) + "<div class=\"frappe-controller-job-log\">" + rows + "</div>");
}

frappe.ui.form.on("Operation", {
  refresh(frm) {
    loadJobLog(frm).catch(() => {
      const field = frm.get_field("job_log");
      field?.$wrapper.html(`<p class="text-danger">${__("Could not load the job log.")}</p>`);
    });
    if (frm.doc.credential_received_at && !frm.doc.credential_consumed_at) {
      frm.add_custom_button(__("Retrieve Administrator Password"), async () => {
        const response = await frappe.call({
          method: "frappe_controller.api.credential_routes.consume_operation_credential",
          args: { operation_id: frm.doc.operation_id },
          type: "POST",
        });
        const value = response.message || response;
        frappe.msgprint({
          title: __("Administrator Password (shown once)"),
          message: `<pre style="user-select:all">${frappe.utils.escape_html(value.administrator_credential)}</pre>`,
          wide: true,
        });
        await frm.reload_doc();
      });
    }
  },
});
