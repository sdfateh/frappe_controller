frappe.ui.form.on("Customer", {
  refresh(frm) {
    if (frm.is_new() || !frappe.user_roles.some((role) =>
      ["Controller Admin", "Operator"].includes(role)
    )) {
      return;
    }

    if (frm.doc.controller_site_creation_operation) {
      frm.add_custom_button(__("Site Operation"), () => {
        frappe.set_route("Form", "Operation", frm.doc.controller_site_creation_operation);
      });
    }
    if (frm.doc.controller_production_managed_site || frm.doc.controller_site_creation_operation) {
      return;
    }

    frm.add_custom_button(__("Site"), () => {
      const dialog = new frappe.ui.Dialog({
        title: __("Create Site"),
        fields: [
          {
            fieldname: "domain",
            label: __("Domain"),
            fieldtype: "Data",
            reqd: 1,
            description: __("A new FQDN, for example customer.example.com."),
            onchange: () => dialog.fields_dict.readiness.$wrapper.empty(),
          },
          {
            fieldname: "server_agent",
            label: __("Server"),
            fieldtype: "Link",
            options: "Server Agent",
            reqd: 1,
            get_query: () => ({ filters: { enabled: 1 } }),
            onchange: () => {
              dialog.set_value("bench", "");
              dialog.fields_dict.readiness.$wrapper.empty();
            },
          },
          {
            fieldname: "bench",
            label: __("Bench"),
            fieldtype: "Link",
            options: "Bench",
            reqd: 1,
            onchange: () => dialog.fields_dict.readiness.$wrapper.empty(),
            get_query: () => ({
              filters: {
                server_agent: dialog.get_value("server_agent") || "",
                enabled: 1,
              },
            }),
          },
          { fieldname: "readiness", fieldtype: "HTML" },
        ],
        secondary_action_label: __("Check Readiness"),
        secondary_action: async () => {
          const values = dialog.get_values();
          if (!values) return;
          const response = await frappe.call({
            method: "frappe_controller.api.customer_sites.check_site_readiness",
            args: { customer: frm.doc.name, domain: values.domain, server_agent: values.server_agent, bench: values.bench },
            type: "POST", freeze: true, freeze_message: __("Checking target readiness…"),
          });
          const result = response.message || response;
          if (["domain", "server_agent", "bench"].some((key) => dialog.get_value(key) !== values[key])) return;
          const escape = (value) => frappe.utils.escape_html(String(value));
          dialog.fields_dict.readiness.$wrapper.html(
            `<p><strong>${result.ready ? __("Ready to create") : __("Creation blocked")}</strong></p>` +
            `<p>${__("Required apps")}: ${escape((result.required_apps || []).join(", ") || __("Policy not available"))}</p>` +
            (result.checks || []).map((check) => `<p><span class="indicator ${check.passed ? "green" : "red"}">${escape(check.label)}</span><br>${escape(check.message)}</p>`).join("") +
            `<p class="text-muted">${__("Checks run again when you create. Local routing and public site health are verified during execution.")}</p>`
          );
        },
        primary_action_label: __("Create Site"),
        primary_action: async (values) => {
          await frappe.call({
            method: "frappe_controller.api.customer_sites.create_production_site",
            args: { customer: frm.doc.name, ...values },
            type: "POST",
            freeze: true,
            freeze_message: __("Creating site operation…"),
          }).then((response) => {
            const result = response.message || response;
            dialog.hide();
            frappe.show_alert({
              message: __("Site operation {0} is {1}", [result.operation_id, result.state]),
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
