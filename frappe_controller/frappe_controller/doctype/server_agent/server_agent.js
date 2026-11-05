frappe.ui.form.on("Server Agent", {
  refresh(frm) {
    if (!frappe.user_roles.includes("Controller Admin")) {
      return;
    }
    frm.add_custom_button(__("Apply Setup Profile"), () => {
      frappe.call({
        method: "frappe_controller.frappe_controller.doctype.server_agent.server_agent.get_setup_profiles",
        callback(response) {
          const profiles = response.message || {};
          const labels = Object.values(profiles).map((profile) => profile.label);
          frappe.prompt([{fieldname: "profile", label: __("Setup Profile"), fieldtype: "Select", options: labels.join("\n"), reqd: 1}], (values) => {
            const selected = Object.values(profiles).find((profile) => profile.label === values.profile);
            if (!selected) return;
            frappe.confirm(__("Replace the operation and setup app lists with this profile? Review and save the policy before generating a token. Existing installations require a local policy update."), () => {
              frm.set_value("allowed_operations_json", JSON.stringify(selected.operations, null, 2));
              frm.set_value("setup_required_apps_json", JSON.stringify(selected.required_apps, null, 2));
            });
          }, __("Installation Policy"), __("Apply"));
        },
      });
    });
    if (frm.is_new()) return;
    frm.add_custom_button(__("Generate Install Token"), () => {
      if (frm.is_dirty()) {
        frappe.msgprint(__("Save the installation policy before generating a token."));
        return;
      }
      frappe.call({
        method: "frappe_controller.frappe_controller.doctype.server_agent.server_agent.generate_install_token",
        args: { agent_id: frm.doc.name },
        freeze: true,
        callback(response) {
          const value = response.message;
          if (!value) return;
          const dialog = new frappe.ui.Dialog({
            title: __("One-time Agent Enrollment"),
            fields: [
              { fieldname: "install_command", label: __("Install Command"), fieldtype: "Code", read_only: 1, default: value.install_command },
              { fieldname: "enrollment_token", label: __("Enrollment Token"), fieldtype: "Code", read_only: 1, default: value.enrollment_token },
              { fieldname: "notice", fieldtype: "HTML", options: __("The installer will securely prompt for this token. It expires in {0} seconds and is shown only now.", [value.expires_in_seconds]) },
            ],
          });
          dialog.show();
        },
      });
    });
  },
});
