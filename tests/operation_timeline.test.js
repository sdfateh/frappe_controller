const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const escape = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const frappe = {utils: {escape_html: escape}};
const context = vm.createContext({frappe, __: (s) => s, clearTimeout: () => {}, setTimeout: () => 1});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const {model, render} = frappe.controller_operation_timeline;
let sequence = 0;
const event = (kind, step, details = {}, attempt = 1) => ({sequence: ++sequence, kind, step,
  details_json: JSON.stringify(details), attempt, agent_created_at: "2026-10-02 15:28:13"});
const doc = {name: "operation-1", operation_type: "site.create_blank", state: "queued"};
let result = model(doc, []);
assert.equal(result.rows.length, 7);
assert.ok(result.rows.every((r) => r.status === "pending"));
assert.match(render(doc, []), /Waiting for execution/);
const failure = [
  event("operation.queued", "queued"), event("execution.started", null),
  event("step.started", "preflight_reserve"), event("step.completed", "preflight_reserve"),
  event("step.started", "dns"), event("dns.controller_response", null, {http_status: 502}),
  event("step.compensated", "preflight_reserve"),
  event("execution.finished", null, {state: "needs_intervention", error_code: "dns_create_outcome_unknown"}, 0),
];
const stopped = {...doc, state: "needs_intervention", error_code: "dns_create_outcome_unknown"};
result = model(stopped, failure);
assert.equal(result.stopped, "dns");
assert.equal(result.rows[0].status, "compensated");
assert.equal(result.rows[1].status, "stopped");
assert.equal(result.rows[1].events.length, 2);
assert.ok(result.rows.slice(2).every((r) => r.status === "not_reached"));
const html = render(stopped, failure);
assert.match(html, /Stopped at step 2\/7 — Create DNS record/);
assert.match(html, /502/); assert.match(html, /Rolled back/); assert.match(html, /All events/);
assert.ok(!html.includes('data-step="queued"'));
assert.match(render(stopped, []), /no failing step reported/);
assert.ok(model(stopped, []).rows.every((r) => r.status === "unreported"));

const success = [];
for (const step of model(doc, []).rows.map((r) => r.name)) {
  success.push(event("step.started", null, {step}), event("step.completed", null, {step}));
}
result = model({...doc, state: "succeeded"}, success);
assert.ok(result.rows.every((r) => r.status === "completed"));
assert.match(render({...doc, state: "succeeded"}, success), /Operation succeeded/);
assert.ok(model({...doc, state: "succeeded"}, []).rows.every((r) => r.status === "unreported"));
assert.equal(model({...doc, operation_type: "site.restore"}, []).rows.length, 10);
assert.equal(model({...doc, operation_type: "site.create_from_backup"}, []).rows.length, 11);

const retry = failure.slice(0, 6).concat(event("execution.retry_scheduled", null, {error_code: "temporary"}, 0));
assert.match(render({...doc, state: "running"}, retry), /Waiting for automatic retry/);
retry.push(event("execution.started", null, {}, 2), event("step.started", "dns", {}, 2));
assert.equal(model({...doc, state: "running"}, retry).rows[1].status, "running");
const compensated = failure.concat(event("step.resumed", "preflight_reserve", {}, 2));
assert.equal(model(stopped, compensated).rows[0].status, "compensated");

const malicious = [event("step.started", "<script>alert(1)</script>", {message: '<img src=x onerror="alert(1)">'})];
const escaped = render({...stopped, operation_type: "unknown", error_code: "<svg/onload=alert(1)>"}, malicious);
assert.ok(!escaped.includes("<script>") && !escaped.includes("<img") && !escaped.includes("<svg"));
assert.match(escaped, /&lt;script&gt;/);
const malformed = event("step.started", "dns"); malformed.details_json = "not JSON <script>";
assert.doesNotThrow(() => render(stopped, [malformed]));
assert.equal(model({...doc, operation_type: "__proto__"}, []).rows.length, 0);
assert.match(render(doc, [], {truncated: true}), /truncated/);

const declared = [event("plan.persisted", null, {steps: ["preflight_reserve", "dns", "route", "new_site", "ensure_apps", "scheduler_enable", "verify", "required_apps", "public_https", "complete"]})];
result = model(doc, declared.concat(event("step.started", "public_https")));
assert.equal(result.rows.length, 10);
assert.equal(result.rows[8].name, "public_https");
assert.match(render({...doc, state: "succeeded"}, []), /Legacy execution succeeded without the new readiness checks/);
const legacyUnverified = {...doc, state: "needs_intervention", error_code: "site_handover_unverified",
  result_json: JSON.stringify({status: "succeeded", result: {domain: "old.example.com"}})};
const legacyHTML = render(legacyUnverified, [event("execution.finished", null, {state: "succeeded"})]);
assert.match(legacyHTML, /requires manual verification before handover/);
assert.ok(!legacyHTML.includes("Operation succeeded") && !legacyHTML.includes("Site ready for handover"));
const readyDoc = {...doc, state: "succeeded", credential_received_at: "2026-10-02", result_json: JSON.stringify({result: {readiness: {version: 1, required_apps: ["frappe", "erpnext", "mos_pro"], apps_verified: true, public_https_verified: true}}})};
assert.match(render(readyDoc, [], {handover: {ready: true, customer_required: true, customer_linked: true}}), /Site ready for handover/);
assert.match(render(readyDoc, [], {handover: {ready: false, customer_required: true, customer_linked: false}}), /Site handover is not yet verified/);
assert.match(render(readyDoc, [], {handover: {customer_required: true}}), /Customer linked to Managed Site/);
const awaiting = render({...doc, state: "leased"}, success.concat(event("execution.finished", null, {state: "succeeded"})));
assert.match(awaiting, /Execution finished — awaiting Controller acknowledgement/);
assert.ok(!awaiting.includes("Operation succeeded"));

// Exercise the actual form loader: failures beyond 200 events must be fetched.
frappe.ui = {form: {on: () => {}}};
vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), context);
const many = Array.from({length: 500}, (_, i) => ({...event("progress", null), sequence: i + 1}));
many.push({...event("step.started", "dns"), sequence: 501});
let calls = [], rendered;
frappe.call = async (request) => {
  if (request.method.includes("get_site_handover_status")) return {message: {applicable: true, ready: false}};
  calls.push(request.args);
  return {message: many.slice(request.args.limit_start, request.args.limit_start + 500)};
};
context.frm = {doc: {...stopped, last_event_sequence: 501},
  get_field: () => ({$wrapper: {html: (value) => {rendered = value;}}})};
(async () => {
  await vm.runInContext("loadJobLog(frm)", context);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].limit_start, 500);
  assert.equal(calls[0].filters.sequence[1], 501);
  assert.match(rendered, /Stopped at step 2\/7/);

  // A late response must not overwrite the next Operation's timeline.
  let resolve;
  frappe.call = () => new Promise((done) => {resolve = done;});
  const pending = vm.runInContext("loadJobLog(frm)", context);
  context.frm.doc.name = "another-operation";
  rendered = "new form";
  resolve({message: failure});
  await pending;
  assert.equal(rendered, "new form");
  console.log("Timeline tests passed: stop location, compensation, plans, retry attempts, missing events, HTML escaping, pagination, and stale responses");
})().catch((error) => {console.error(error); process.exitCode = 1;});
