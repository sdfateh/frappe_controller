const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

let handlers;
let requests = [];
let confirmations = [];
let prompts = [];
let messages = [];
const frappe = {
  user_roles: ["Controller Admin"],
  ui: {form: {on(name, value) { assert.equal(name, "Server Agent"); handlers = value; }}},
  call(value) { requests.push(value); },
  confirm(message, callback) { confirmations.push(callback); },
  prompt(fields, callback) { prompts.push({fields, callback}); },
  msgprint(message) { messages.push(message); },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frappe_controller/frappe_controller/doctype/server_agent/server_agent.js"), "utf8"), {frappe, __: (value) => value});

function form({fresh = false, dirty = false} = {}) {
  const buttons = {};
  const values = {};
  return {buttons, values, doc: {name: "agent-01"}, is_new: () => fresh, is_dirty: () => dirty,
    add_custom_button(label, callback) { buttons[label] = callback; },
    set_value(name, value) { values[name] = value; }};
}

const fresh = form({fresh: true});
handlers.refresh(fresh);
assert.equal(typeof fresh.buttons["Apply Setup Profile"], "function");
assert.equal(fresh.buttons["Generate Install Token"], undefined);
fresh.buttons["Apply Setup Profile"]();
requests.pop().callback({message: {customer_erp: {label: "Customer ERP creation", operations: ["site.verify", "site.backup", "site.create_blank"], required_apps: ["frappe", "erpnext", "mos_pro"]}}});
prompts.pop().callback({profile: "Customer ERP creation"});
assert.deepEqual(fresh.values, {}); // No policy change before the replacement is reviewed.
confirmations.pop()();
assert.deepEqual(JSON.parse(fresh.values.setup_required_apps_json), ["frappe", "erpnext", "mos_pro"]);
assert.deepEqual(JSON.parse(fresh.values.allowed_operations_json), ["site.verify", "site.backup", "site.create_blank"]);
assert.equal(Object.keys(fresh.values).length, 2); // No feature/approval/environment changes.

const dirty = form({dirty: true});
handlers.refresh(dirty);
const count = requests.length;
dirty.buttons["Generate Install Token"]();
assert.equal(requests.length, count);
assert.equal(messages.length, 1);

frappe.user_roles = ["Operator"];
const operator = form();
handlers.refresh(operator);
assert.deepEqual(operator.buttons, {});
console.log("Server Agent setup profile form tests passed");
