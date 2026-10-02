const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
let handlers, buttons, confirmation, request, route;
const frappe = {
  utils: { escape_html: (s) => s }, user_roles: ["Operator"], session: { user: "operator" },
  ui: { form: { on: (_, value) => { handlers = value; } } },
  confirm: (text, action) => { confirmation = {text, action}; },
  call: async (value) => { request = value; return {message: {operation_id: "retry-1"}}; },
  set_route: (...args) => { route = args; }, msgprint: () => {},
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), {frappe, __: (s) => s});
const frm = {
  doc: {name: "original-1", operation_type: "site.create_blank", requested_by: "operator"},
  is_new: () => false, get_field: () => null,
  add_custom_button: (label, action) => { buttons[label] = action; },
};
function refresh(state) { buttons = {}; frm.doc.state = state; handlers.refresh(frm); }
(async () => {
  for (const state of ["awaiting_approval", "queued", "leased", "running", "succeeded"]) {
    refresh(state); assert.equal(buttons.Retry, undefined, state);
  }
  for (const state of ["failed", "cancelled", "timed_out", "needs_intervention", "dead_letter", "rejected"]) {
    refresh(state); assert.equal(typeof buttons.Retry, "function", state);
  }
  refresh("needs_intervention"); buttons.Retry();
  assert.match(confirmation.text, /partial/);
  assert.equal(request, undefined); // Nothing is sent before confirmation.
  await confirmation.action();
  assert.equal(request.method, "frappe_controller.api.operations.retry_operation");
  assert.equal(request.args.operation_id, "original-1");
  assert.equal(request.args.recovery_confirmed, 1);
  assert.equal(request.type, "POST");
  assert.deepEqual(route, ["Form", "Operation", "retry-1"]);
  frappe.user_roles = ["Auditor"]; refresh("failed"); assert.equal(buttons.Retry, undefined);
  frappe.user_roles = ["Operator"]; frm.doc.requested_by = "someone-else";
  refresh("failed"); assert.equal(buttons.Retry, undefined);
  frappe.user_roles = ["Controller Admin"]; refresh("failed"); assert.equal(typeof buttons.Retry, "function");
  console.log("Operation Retry UI: state/role/ownership gates, confirmation, and POST navigation passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
