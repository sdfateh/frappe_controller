frappe.ui.form.on("Customer", {
  refresh(frm) {
    if (frm.is_new() || !frappe.user_roles.some((role) =>
      ["Controller Admin", "Operator"].includes(role)
    )) {
      return;
    }

    frm.add_custom_button(__("Production Site"), () => {
      const dialog = new frappe.ui.Dialog({
        title: __("Create Production Site"),
        fields: [
          {
            fieldname: "domain",
            label: __("Domain"),
            fieldtype: "Data",
            reqd: 1,
            description: __("A new production FQDN, for example customer.example.com."),
          },
          {
            fieldname: "server_agent",
            label: __("Server"),
            fieldtype: "Link",
            options: "Server Agent",
            reqd: 1,
            get_query: () => ({ filters: { environment: "production", enabled: 1 } }),
          },
          {
            fieldname: "bench",
            label: __("Bench"),
            fieldtype: "Link",
            options: "Bench",
            reqd: 1,
            get_query: () => ({
              filters: {
                server_agent: dialog.get_value("server_agent") || "",
                environment: "production",
                enabled: 1,
              },
            }),
          },
        ],
        primary_action_label: __("Create Production Site"),
        primary_action: async (values) => {
          await frappe.call({
            method: "frappe_controller.api.customer_sites.create_production_site",
            args: { customer: frm.doc.name, ...values },
            type: "POST",
            freeze: true,
            freeze_message: __("Creating production-site operation…"),
          }).then((response) => {
            const result = response.message || response;
            dialog.hide();
            frappe.show_alert({
              message: __("Production-site operation {0} is {1}", [result.operation_id, result.state]),
              indicator: "green",
            });
            frappe.set_route("Form", "Operation", result.operation_id);
          });
        },
      });
      dialog.show();
    }, __("Create"));
  },
});
