frappe.listview_settings["Bulk Operation"] = {
  onload(listview) {
    if (!window.frappeControllerDesk?.featureEnabled("bulk_operations")) return;
    const roles = new Set(frappe.user_roles || []);
    if (!roles.has("Controller Admin") && !roles.has("Operator")) return;

    const lines = (value) => (value || "")
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);

    listview.page.add_inner_button(__("Create from Previews"), () => {
      const dialog = new frappe.ui.Dialog({
        title: __("Create Bulk Data Update"),
        fields: [
          {
            fieldname: "operation_type", fieldtype: "Select", label: __("Operation"),
            options: "data.update\ndata.update.break_glass", reqd: 1,
          },
          {
            fieldname: "environment", fieldtype: "Select", label: __("Environment"),
            options: "\ndevelopment\nstaging\nproduction",
          },
          {
            fieldname: "exact_site_ids", fieldtype: "Small Text",
            label: __("Exact Site IDs (one per line)"),
          },
          {
            fieldname: "agent_ids", fieldtype: "Small Text",
            label: __("Agent IDs (one per line)"),
          },
          {
            fieldname: "bench_ids", fieldtype: "Small Text",
            label: __("Bench IDs (one per line)"),
          },
          {
            fieldname: "labels", fieldtype: "Small Text",
            label: __("Required Labels (one per line)"),
          },
          {
            fieldname: "preview_operation_ids", fieldtype: "Small Text",
            label: __("Successful Preview Operation IDs (one per line)"), reqd: 1,
          },
        ],
        primary_action_label: __("Create Approval Request"),
        primary_action(values) {
          const selector = {
            exact_site_ids: lines(values.exact_site_ids),
            environment: values.environment || null,
            agent_ids: lines(values.agent_ids),
            bench_ids: lines(values.bench_ids),
            labels: lines(values.labels),
          };
          const previews = lines(values.preview_operation_ids);
          if (!previews.length || !Object.values(selector).some((value) => (
            Array.isArray(value) ? value.length : Boolean(value)
          ))) {
            frappe.msgprint(__("A bounded selector and at least one preview are required."));
            return;
          }
          dialog.disable_primary_action();
          frappe.call({
            method: "frappe_controller.api.bulk_operations.create_bulk_data_update",
            args: {
              request_json: JSON.stringify({
                bulk_operation_id: window.crypto.randomUUID(),
                operation_type: values.operation_type,
                selector,
                preview_operation_ids: previews,
              }),
            },
            freeze: true,
          }).then(({ message }) => {
            dialog.hide();
            if (message) frappe.set_route("Form", "Bulk Operation", message.bulk_operation_id);
          }).finally(() => dialog.enable_primary_action());
        },
      });
      dialog.show();
    });
  },
};
