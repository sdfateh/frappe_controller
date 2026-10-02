async function loadJobLog(frm) {
  const field = frm.get_field("job_log");
  if (!field || !frm.doc.name) return;
  const operation = frm.doc.name;
  const requestId = (frm._timeline_request_id || 0) + 1;
  frm._timeline_request_id = requestId;
  clearTimeout(frm._timeline_timer);
  field.$wrapper.html(`<p class="text-muted">${__("Loading execution timeline…")}</p>`);
  // Load beyond the original 200-event limit so a late failure is not hidden.
  const events = [];
  const filters = { operation };
  if (Number(frm.doc.last_event_sequence) > 0) filters.sequence = ["<=", Number(frm.doc.last_event_sequence)];
  let truncated = false;
  let handover = null;
  if (["site.create", "site.create_blank", "site.create_from_backup"].includes(frm.doc.operation_type)) {
    try {
      const response = await frappe.call({
        method: "frappe_controller.api.operation_handover.get_site_handover_status",
        args: {operation_id: operation},
      });
      handover = response.message || null;
    } catch (_) {
      // Events remain useful even when the user cannot read the Customer.
      handover = {unavailable: true};
    }
    if (frm.doc.name !== operation || frm._timeline_request_id !== requestId) return;
  }
  for (let offset = 0; offset < 10000; offset += 500) {
    const response = await frappe.call({
      method: "frappe.client.get_list",
      args: {
        doctype: "Operation Event", filters,
        fields: ["sequence", "attempt", "step", "kind", "details_json", "agent_created_at"],
        order_by: "sequence asc", limit_start: offset, limit_page_length: 500,
      },
    });
    if (frm.doc.name !== operation || frm._timeline_request_id !== requestId) return;
    const page = response.message || [];
    events.push(...page);
    if (page.length < 500) break;
    truncated = offset === 9500;
  }
  field.$wrapper.html(frappe.controller_operation_timeline.render(frm.doc, events, {truncated, handover}));
  if (!frappe.controller_operation_timeline.terminal.has(frm.doc.state) ||
      (frm.doc.state === "succeeded" && handover?.customer_required && !handover.customer_linked)) {
    frm._timeline_timer = setTimeout(() => {
      const route = frappe.get_route();
      if (route[0] === "Form" && route[1] === "Operation" && route[2] === operation &&
          frm.doc.name === operation && !frm.is_dirty()) frm.reload_doc();
    }, 10000);
  }
}

frappe.ui.form.on("Operation", {
  refresh(frm) {
    if (!frm.is_new()) {
      frm.add_custom_button(__("Refresh Timeline"), () => frm.reload_doc());
    }
    const retryStates = ["failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"];
    const canRetry = !frm.is_new() && retryStates.includes(frm.doc.state) &&
      frappe.user_roles.some((role) => ["Controller Admin", "Operator"].includes(role)) &&
      (frm.doc.requested_by === frappe.session.user || frappe.user_roles.includes("Controller Admin"));
    if (canRetry) {
      frm.add_custom_button(__("Retry"), () => {
        if (frm.doc.bulk_parent || frm.doc.bulk_target || !frm.doc.operation_type.startsWith("site.")) {
          frappe.msgprint(__("Use the dedicated bulk or data-update workflow to retry this operation safely."));
          return;
        }
        const uncertain = ["needs_intervention", "timed_out"].includes(frm.doc.state);
        frappe.confirm(uncertain
          ? __("The previous attempt may have made partial changes. Confirm that you reviewed the job log, verified it has stopped, and checked or cleaned up partial resources. Create a new retry operation?")
          : __("Create a new operation with the same target and payload? Current permissions and approval rules will be checked again."),
        async () => {
          const response = await frappe.call({
            method: "frappe_controller.api.operations.retry_operation",
            args: { operation_id: frm.doc.name, recovery_confirmed: uncertain ? 1 : 0 },
            type: "POST", freeze: true, freeze_message: __("Creating retry operation…"),
          });
          const result = response.message || response;
          frappe.set_route("Form", "Operation", result.operation_id);
        });
      });
    }
    loadJobLog(frm).catch(() => {
      const field = frm.get_field("job_log");
      field?.$wrapper.html(`<p class="text-danger">${__("Could not load the execution timeline.")}</p>`);
    });
    const canApprove = frappe.user_roles.includes("Approver") &&
      frm.doc.approval_status === "pending" &&
      frm.doc.requested_by !== frappe.session.user;
    if (canApprove) {
      const decide = (decision) => {
        frappe.prompt(
          [{ fieldname: "comment", label: __("Comment"), fieldtype: "Small Text" }],
          async (values) => {
            await frappe.call({
              method: "frappe_controller.api.approvals.decide_operation",
              args: {
                operation_id: frm.doc.operation_id,
                decision,
                comment: values.comment || "",
              },
              type: "POST",
              freeze: true,
            });
            await frm.reload_doc();
          },
          decision === "approved" ? __("Approve Operation") : __("Reject Operation"),
          decision === "approved" ? __("Approve") : __("Reject")
        );
      };
      frm.add_custom_button(__("Approve"), () => decide("approved"), __("Approval"));
      frm.add_custom_button(__("Reject"), () => decide("rejected"), __("Approval"));
    }
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
