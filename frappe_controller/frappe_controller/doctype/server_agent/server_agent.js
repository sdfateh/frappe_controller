frappe.ui.form.on("Server Agent", {
  refresh(frm) {
    if (frm.is_new() || !frappe.user_roles.includes("Controller Admin")) {
      return;
    }
    frm.add_custom_button(__("Generate Install Token"), () => {
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
