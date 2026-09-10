frappe.pages["site-actions"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: __("Site Actions"),
    single_column: true,
  });
  const fields = new frappe.ui.FieldGroup({
    body: $('<div class="p-4">').appendTo(page.body),
    fields: [
      { fieldname: "domain", label: __("Domain"), fieldtype: "Data", reqd: 1 },
      { fieldname: "server_agent", label: __("Server"), fieldtype: "Link", options: "Server Agent", reqd: 1 },
      { fieldname: "bench", label: __("Bench"), fieldtype: "Link", options: "Bench", reqd: 1 },
      {
        fieldname: "action", label: __("Action"), fieldtype: "Select", reqd: 1,
        options: "site.create_blank\nsite.backup\nsite.migrate\nsite.scheduler.enable\nsite.scheduler.disable\nsite.maintenance.enable\nsite.maintenance.disable",
        default: "site.create_blank",
      },
    ],
  });
  fields.make();
  fields.get_field("bench").get_query = () => ({
    filters: { server_agent: fields.get_value("server_agent") || "" },
  });
  page.set_primary_action(__("Run Action"), async () => {
    const values = fields.get_values();
    if (!values) return;
    const response = await frappe.call({
      method: "frappe_controller.api.site_actions.submit_site_action",
      args: values,
      type: "POST",
      freeze: true,
      freeze_message: __("Creating operation…"),
    });
    const result = response.message || response;
    frappe.show_alert({ message: __("Operation {0} is {1}", [result.operation_id, result.state]), indicator: "green" });
    frappe.set_route("Form", "Operation", result.operation_id);
  });
};
