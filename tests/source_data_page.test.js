const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const pending = [];
const frappe = {pages: {"source-data": {}}, call(options) {
  return new Promise((resolve, reject) => pending.push({options, resolve, reject}));
}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,
  "../frappe_controller/frappe_controller/page/source_data/source_data.js"), "utf8"),
{frappe, __: value => value, $: () => element(), crypto: require("node:crypto").webcrypto});
const Browser = frappe.pages["source-data"].Browser;
// Desk v16 has jQuery's ready hook; frappe.ready belongs to website pages.
const desk = {window: {}, frappe: {get_route: () => ["Frappe Controller"],
  show_alert() {}, set_route(route) {this.redirected = route;}},
  $: callback => callback(), __: value => value};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,
  "../frappe_controller/public/js/controller_workspace.js"), "utf8"), desk);
assert.equal(desk.frappe.redirected, "home", "Disabled Controller workspace must still be guarded in v16 Desk");
function element(value = "") {
  return {value, val(value) {if (!arguments.length) return this.value; this.value = value; return this;}, text(value) {this.value = value; return this;},
    removeClass() {return this;}, addClass() {return this;}, prop() {return this;},
    empty() {return this;}, hide() {return this;}, show() {return this;}, appendTo() {return this;},
    on() {return this;}, remove() {return this;}, append() {return this;}, attr() {return this;}, toggle() {return this;}};
}
function browser() {
  const instance = Object.create(Browser.prototype);
  Object.assign(instance, {category: "vehicles", offset: 0, localOffset: 0, sequence: 0,
    filters: ["brand", "model", "year", "structure_type"], lookupFields: {brands: "brand", models: "model"},
    selects: {brand: element("Toyota"), model: element("Corolla"), year: element(), structure_type: element()},
    heading: element(), status: element(), tableArea: element(), empty: element(), range: element(),
    previous: element(), next: element(), renderCount: 0, render() {this.renderCount++;}});
  return instance;
}

(async () => {
  const editSchema = {fields: {name: {type: "string"}, width: {type: "number", nullable: true}, optional: {type: "string", nullable: true}}};
  const originalEdit = {name: "Same", width: "215.0", optional: null};
  assert.equal(JSON.stringify(Browser.editChanges(originalEdit, {name: "Same", width: "215.000", optional: ""}, editSchema)), "{}");
  assert.equal(JSON.stringify(Browser.editChanges(originalEdit, {width: ""}, editSchema)), JSON.stringify({width: null}));
  assert.equal(JSON.stringify(Browser.editChanges(originalEdit, {name: " New "}, editSchema)), JSON.stringify({name: "New"}));
  assert.equal(JSON.stringify(Browser.parseCSV('\uFEFFmodel,brand\r\n"Model, with comma",Example\r\n"A ""quoted"" model",Example\r\n')),
    JSON.stringify([{model: "Model, with comma", brand: "Example"}, {model: 'A "quoted" model', brand: "Example"}]));
  assert.equal(JSON.stringify(Browser.parseCSV("colorNameEN,colorNameAR\nWhite,أبيض", ["colorNameEN", "colorNameAR"], ["colorNameEN", "colorNameAR"])),
    JSON.stringify([{colorNameEN: "White", colorNameAR: "أبيض"}]));
  assert.equal(Browser.parseCSV("tire_name,section_width\nExample,215.5", ["tire_name", "section_width"], ["tire_name"])[0].section_width, "215.5");
  for (const text of ["", "brand,brand\nb,m", 'brand,model\nb,"unclosed', "brand,model\nb,m,extra", 'brand,model\nb,"quoted"extra',
    "brand,model\n" + Array.from({length: 101}, (_, i) => `b,m${i}`).join("\n"), "x".repeat(65537)]) {
    assert.throws(() => Browser.parseCSV(text));
  }
  const instance = browser();
  assert.equal(JSON.stringify(instance.requestArgs()), JSON.stringify({resource: "years", filters: {brand: "Toyota", model: "Corolla"}, start: 0}));
  instance.category = "tires";
  instance.offset = 50;
  assert.equal(JSON.stringify(instance.requestArgs()), JSON.stringify({resource: "tires", filters: {}, start: 50}));
  const first = instance.load();
  assert.equal(pending[0].options.type, "GET");
  assert.equal(pending[0].options.silent, true);
  instance.category = "colors";
  instance.offset = 0;
  const second = instance.load();
  const latest = {items: [{colorNameEN: "Blue"}], source: "source", fetched_at: new Date().toISOString()};
  pending[1].resolve({message: latest});
  await second;
  pending[0].resolve({message: {items: [{brand: "Old result"}]}});
  await first;
  assert.equal(instance.result, latest, "Late responses must not overwrite the current category");
  assert.equal(instance.renderCount, 1);

  const failing = instance.load();
  pending[2].reject({responseJSON: {exc_type: "ValidationError", _server_messages: JSON.stringify([JSON.stringify({message: "Source API is unavailable (HTTP 500)."})])}});
  await failing;
  assert.equal(instance.status.value, "Source API is unavailable (HTTP 500).");
  assert.equal(instance.result, null, "A failed reload must clear previous results");
  assert.equal(instance.rows.length, 0);
  assert.equal(instance.loading, false);
  assert.equal(instance.errorMessage({responseJSON: {exc_type: "InternalError", _server_messages: "secret"}}), "Source data is unavailable. Please try again later.");

  const recovery = instance.load();
  pending[3].resolve({message: latest});
  await recovery;
  assert.equal(instance.result, latest);
  instance.category = "vehicles";
  instance.move(1);
  assert.equal(instance.localOffset, 50);
  instance.move(-1);
  instance.move(-1);
  assert.equal(instance.localOffset, 0);

  const writer = browser();
  Object.assign(writer, {root: element(), search: element(), resetAfter() {}, reloads: 0,
    async load() {this.reloads++;}});
  const operation = writer.runAdditions([{brand: "b", model: "one"}, {brand: "b", model: "two"}, {brand: "b", model: "three"}]);
  assert.equal(pending[4].options.type, "POST");
  assert.equal(JSON.parse(pending[4].options.args.record).model, "one");
  assert.match(pending[4].options.args.request_id, /^[a-f0-9-]{36}$/);
  pending[4].resolve({message: {state: "added", message: "Added"}});
  await new Promise(resolve => setImmediate(resolve));
  pending[5].resolve({message: {state: "uncertain", message: "Check source"}});
  await operation;
  assert.equal(writer.lastAdditions.map(row => row.state).join(","), "added,uncertain,not_sent");
  assert.equal(pending.length, 6, "An uncertain write must stop remaining rows without retrying");
  assert.equal(writer.mutationBusy, false);
  assert.equal(writer.reloads, 1);

  const stopped = writer.runAdditions([{brand: "b", model: "four"}, {brand: "b", model: "five"}]);
  writer.stopRequested = true;
  pending[6].resolve({message: {state: "existing", message: "Already exists"}});
  await stopped;
  assert.equal(writer.lastAdditions.map(row => row.state).join(","), "existing,not_sent");
  assert.equal(pending.length, 7);
  const deleting = writer.runAdditions([{id: 1, snapshot: "a".repeat(64)}, {id: 2, snapshot: "b".repeat(64)}], "colors", "delete");
  assert.equal(pending[7].options.method, "frappe_controller.api.source_data.remove");
  assert.equal(pending[7].options.args.record_id, 1);
  assert.equal(pending[7].options.args.snapshot_hash, "a".repeat(64));
  assert.equal(pending[7].options.args.resource, "colors");
  pending[7].resolve({message: {state: "failed", message: "Linked records"}});
  await new Promise(resolve => setImmediate(resolve));
  pending[8].resolve({message: {state: "deleted", message: "Deleted"}});
  await deleting;
  assert.equal(writer.lastAdditions.map(row => row.state).join(","), "failed,deleted");
  writer.result = {source: "https://new.example.com"};
  const editing = writer.runAdditions([{id: 1, label: "Example", changes: {colorNameAR: "أبيض"}, snapshot: "a".repeat(64)}], "colors", "edit", "https://reviewed.example.com");
  assert.equal(pending[9].options.method, "frappe_controller.api.source_data.edit");
  assert.equal(pending[9].options.type, "POST");
  assert.equal(pending[9].options.args.record_id, 1);
  assert.equal(pending[9].options.args.changes, JSON.stringify({colorNameAR: "أبيض"}));
  assert.equal(pending[9].options.args.source, "https://reviewed.example.com", "A review retains its original API origin");
  pending[9].resolve({message: {state: "updated", message: "Updated"}});
  await editing;
  assert.equal(writer.lastAdditions[0].state, "updated");
  console.log("Source Data create/edit/delete flows, partial changes, CSVs and uncertainty tests passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
