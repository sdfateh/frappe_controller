frappe.ui.form.on("Operation", {
  refresh(frm) {
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
