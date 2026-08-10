frappe.ui.form.on("Bulk Operation", {
  refresh(frm) {
    if (frm.is_new()) return;
    if (!window.frappeControllerDesk?.featureEnabled("bulk_operations")) return;

    const operationId = frm.doc.bulk_operation_id;
    const roles = new Set(frappe.user_roles || []);
    const canOperate = roles.has("Controller Admin") || roles.has("Operator");
    const canApprove = roles.has("Approver");

    frm.add_custom_button(__("Refresh Progress"), () => {
      frappe.call({
        method: "frappe_controller.api.bulk_operations.bulk_operation_progress",
        args: { bulk_operation_id: operationId, offset: 0, page_length: 50 },
      }).then(({ message }) => {
        if (!message) return;
        const counts = message.counts;
        const rows = (message.targets || []).map((target) => {
          const site = frappe.utils.escape_html(target.managed_site || "");
          const state = frappe.utils.escape_html(target.state || "");
          const child = frappe.utils.escape_html(target.child_operation || "—");
          return `<tr><td>${target.ordinal}</td><td>${site}</td><td>${state}</td><td>${child}</td></tr>`;
        }).join("");
        frappe.msgprint({
          title: __("Bulk Progress"),
          wide: true,
          message: `
            <p>${__("Planned")}: ${counts.planned} · ${__("Queued")}: ${counts.queued} ·
              ${__("Running")}: ${counts.running} · ${__("Succeeded")}: ${counts.succeeded} ·
              ${__("Failed")}: ${counts.failed} · ${__("Cancelled")}: ${counts.cancelled} ·
              ${__("Needs Intervention")}: ${counts.needs_intervention}</p>
            <table class="table table-bordered"><thead><tr><th>#</th><th>${__("Site")}</th>
              <th>${__("State")}</th><th>${__("Child Operation")}</th></tr></thead>
              <tbody>${rows}</tbody></table>`,
        });
      });
    });

    const invoke = (method, confirmMessage) => {
      const run = () => frappe.call({
        method: `frappe_controller.api.bulk_operations.${method}`,
        args: { bulk_operation_id: operationId },
        freeze: true,
      }).then(() => frm.reload_doc());
      if (confirmMessage) frappe.confirm(confirmMessage, run);
      else run();
    };

    if (canOperate && ["approved", "canary", "running"].includes(frm.doc.state)) {
      frm.add_custom_button(__("Pause"), () => invoke("pause_bulk_operation"), __("Control"));
    }
    if (canOperate && frm.doc.state === "paused" && !frm.doc.cancel_requested) {
      frm.add_custom_button(__("Resume"), () => invoke("resume_bulk_operation"), __("Control"));
    }
    if (canOperate && ["approved", "canary", "running", "paused"].includes(frm.doc.state)) {
      frm.add_custom_button(
        __("Cancel"),
        () => invoke("cancel_bulk_operation", __("Cancel all uncompleted targets? Completed work will not be rolled back.")),
        __("Control"),
      );
    }
    if (canOperate && ["partial", "failed", "paused", "needs_intervention"].includes(frm.doc.state)) {
      frm.add_custom_button(__("Retry Failed with Fresh Previews"), () => {
        frappe.prompt(
          [{
            fieldname: "preview_operation_ids", fieldtype: "Small Text",
            label: __("Successful Preview Operation IDs (one per line)"), reqd: 1,
          }],
          (values) => {
            const previews = (values.preview_operation_ids || "")
              .split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
            return frappe.call({
              method: "frappe_controller.api.bulk_operations.retry_failed_bulk_data_update",
              args: {
                request_json: JSON.stringify({
                  bulk_operation_id: window.crypto.randomUUID(),
                  source_bulk_operation_id: operationId,
                  preview_operation_ids: previews,
                }),
              },
              freeze: true,
            }).then(({ message }) => {
              if (message) frappe.set_route("Form", "Bulk Operation", message.bulk_operation_id);
            });
          },
          __("Retry Failed Targets"),
          __("Create Approval Request"),
        );
      }, __("Control"));
    }

    if (
      canApprove && frm.doc.state === "awaiting_approval" &&
      frm.doc.approval_status === "pending" && frm.doc.requested_by !== frappe.session.user
    ) {
      const decide = (decision) => frappe.prompt(
        [{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") }],
        (values) => frappe.call({
          method: "frappe_controller.api.bulk_operations.decide_bulk_operation",
          args: {
            bulk_operation_id: operationId,
            decision,
            comment: values.comment || "",
          },
          freeze: true,
        }).then(() => frm.reload_doc()),
        decision === "approved" ? __("Approve Bulk Operation") : __("Reject Bulk Operation"),
        decision === "approved" ? __("Approve") : __("Reject"),
      );
      frm.add_custom_button(__("Approve"), () => decide("approved"), __("Approval"));
      frm.add_custom_button(__("Reject"), () => decide("rejected"), __("Approval"));
    }
  },
});
