// Run: node tests/customer_form.test.js [path/to/customer.js]
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = process.argv[2] || path.join(__dirname, "../frappe_controller/public/js/customer.js");
let handlers;
let dialog;
const frappe = {
  user_roles: ["Operator"],
  ui: {
    form: { on: (doctype, callbacks) => { assert.equal(doctype, "Customer"); handlers = callbacks; } },
    Dialog: class {
      constructor(options) { this.options = options; this.values = {}; this.fields_dict = {readiness: {$wrapper: {empty() {}}}}; dialog = this; }
      get_value(key) { return this.values[key]; }
      set_value(key, value) { this.values[key] = value; }
      show() {}
    },
  },
};
vm.runInNewContext(fs.readFileSync(source, "utf8"), { frappe, __: (text) => text });
let button;
const frm = {
  doc: {},
  is_new: () => false,
  add_custom_button(label, action, group) {
    assert.equal(label, "Site"); assert.equal(group, "Create"); button = action;
  },
};
handlers.refresh(frm);
assert.equal(typeof button, "function");
button();
const server = dialog.options.fields.find((field) => field.fieldname === "server_agent");
const bench = dialog.options.fields.find((field) => field.fieldname === "bench");
const plain = (value) => JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(server.get_query()), { filters: { enabled: 1 } });
dialog.values.server_agent = "server-01";
assert.deepEqual(plain(bench.get_query()), { filters: { server_agent: "server-01", enabled: 1 } });
dialog.values.bench = "old-bench";
server.onchange();
assert.equal(dialog.values.bench, "");
button = undefined;
frappe.user_roles = ["Auditor"];
handlers.refresh(frm);
assert.equal(button, undefined);
frm.is_new = () => false;
frappe.user_roles = ["Operator"];
frm.doc.controller_site_creation_operation = "operation-1";
const buttons = [];
frm.add_custom_button = (label, action) => buttons.push({label, action});
let route;
frappe.set_route = (...args) => { route = args; };
handlers.refresh(frm);
assert.equal(buttons.length, 1);
assert.equal(buttons[0].label, "Site Operation");
buttons[0].action();
assert.deepEqual(route, ["Form", "Operation", "operation-1"]);
frm.doc = {};
frm.add_custom_button = (label, action) => { button = action; };
frappe.user_roles = ["Operator"];
frm.is_new = () => true;
handlers.refresh(frm);
assert.equal(button, undefined);
console.log("Customer form: environment-neutral selection, ownership filter, stale selection reset, and role checks passed");
