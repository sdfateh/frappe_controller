const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const pending = [], timers = new Map();
function element(value = "") {
  return {value, attrs: {}, events: {}, val(value) {
    if (!arguments.length) return this.value;
    this.value = value; return this;
  }, attr(key, value) {
    if (typeof key === "object") Object.assign(this.attrs, key);
    else if (arguments.length === 1) return this.attrs[key];
    else this.attrs[key] = value;
    return this;
  }, on(events, callback) {
    for (const event of events.split(" ")) (this.events[event.split(".")[0]] ||= []).push(callback);
    return this;
  }, trigger(event) {for (const callback of this.events[event] || []) callback(); return this;},
  text(value) {this.content = value; return this;}, appendTo() {return this;},
  toggleClass() {return this;}, get() {return this;}};
}
const frappe = {pages: {"source-data": {}}, call(options) {
  return new Promise((resolve, reject) => pending.push({options, resolve, reject}));
}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,
  "../frappe_controller/frappe_controller/page/source_data/source_data.js"), "utf8"), {
  frappe, __: (value, args = []) => value.replace(/\{(\d+)\}/g, (_, index) => args[index]),
  $: () => element(), crypto: require("node:crypto").webcrypto,
  setTimeout(callback) {const id = Symbol(); timers.set(id, callback); return id;},
  clearTimeout(id) {timers.delete(id);},
});
const Autocomplete = frappe.pages["source-data"].VehicleAutocomplete;
const settle = () => new Promise(resolve => setImmediate(resolve));
const flushTimers = () => {const tasks = [...timers.values()]; timers.clear(); tasks.forEach(task => task());};
function control(value) {
  return {$input: element(value), $wrapper: element(), df: {length: 100},
    awesomplete: {close() {}}, set_data(values) {this.values = values;},
    set_value(value) {this.$input.val(value); this.saved = value;}};
}
(async () => {
  const brand = control("Toyota"), model = control("Saved legacy model");
  const fields = new Autocomplete(brand, model, () => "Lookup unavailable.", "https://reviewed.example.com");
  assert.equal(pending.length, 2);
  assert.equal(pending[0].options.method, "frappe_controller.api.source_data.browse");
  assert.equal(pending[1].options.args.filters, JSON.stringify({brand: "Toyota"}));
  assert.equal(pending[1].options.args.source, "https://reviewed.example.com");
  assert.equal(model.$input.val(), "Saved legacy model", "Initialization preserves saved values");
  assert.equal(model.$input.attrs["aria-busy"], "true");
  pending[0].resolve({message: {items: [{brand: "Toyota"}, {brand: "Ford"}, {brand: "Toyota"}]}});
  pending[1].resolve({message: {items: [{model: "Corolla"}]}});
  await settle();
  assert.equal(brand.values.join(","), "Ford,Toyota");
  assert.equal(model.values.join(","), "Saved legacy model,Corolla");
  assert.equal(model.$input.val(), "Saved legacy model", "Lookup completion must not rewrite free text");
  assert.equal(model.$input.attrs.maxlength, 100);
  assert.equal(model.$input.attrs["aria-label"], "Model");
  assert.ok(model.$input.attrs["aria-describedby"]);

  brand.$input.val("Ford").trigger("input");
  assert.equal(model.$input.val(), "");
  assert.equal(model.saved, "");
  assert.equal(model.values.length, 0, "Old suggestions clear immediately before the debounce");
  flushTimers();
  const old = pending[2];
  brand.$input.val("Honda").trigger("input");
  flushTimers();
  pending[3].resolve({message: {items: [{model: "Civic"}]}});
  await settle();
  old.resolve({message: {items: [{model: "Old Ford model"}]}});
  await settle();
  assert.equal(model.values.join(","), "Civic", "A late old-brand response is discarded");

  model.$input.val("civ").trigger("input");
  assert.match(fields.states.model.status.content, /arrow keys/);
  model.$input.val("Unlisted free text").trigger("input");
  assert.match(fields.states.model.status.content, /No matching suggestions/);
  assert.equal(model.$input.val(), "Unlisted free text");
  assert.equal(model.values[0], "Unlisted free text", "A custom option saves the raw text");
  assert.equal(model.sourceTypedValue, "Unlisted free text");
  assert.equal(model.awesomplete.item({value: "Unlisted free text", label: "Unlisted free text"}).content,
    "Use “Unlisted free text” (new value)", "The custom option has an explicit presentation label");
  model.$input.val("Civic").trigger("input");
  assert.equal(model.sourceTypedValue, null, "Exact source values do not get a duplicate custom option");
  assert.equal(model.values.join(","), "Civic");
  brand.$input.val("").trigger("input");
  flushTimers();
  assert.equal(pending.length, 4, "An empty brand cannot issue a model lookup");
  assert.equal(model.values.length, 0);
  assert.match(fields.states.model.status.content, /Enter a brand/);

  brand.$input.val("New").trigger("input");
  brand.$input.val("New Brand").trigger("input");
  brand.$input.trigger("change").trigger("awesomplete-selectcomplete");
  flushTimers();
  assert.equal(pending.length, 5, "Rapid typing is debounced; selection events do not duplicate calls");
  assert.equal(pending[4].options.args.filters, JSON.stringify({brand: "New Brand"}));
  brand.$input.val("").trigger("input");
  pending[4].reject(new Error("old failure"));
  await settle();
  assert.equal(fields.states.model.state, "waiting", "Old errors cannot replace the cleared-brand state");

  brand.$input.val("Broken").trigger("change");
  flushTimers();
  pending[5].reject(new Error("private backend detail"));
  await settle();
  assert.match(fields.states.model.status.content, /Lookup unavailable/);
  assert.equal(model.$input.attrs["aria-busy"], "false");
  model.$input.trigger("focus");
  pending[6].resolve({message: {items: []}});
  await settle();
  assert.match(fields.states.model.status.content, /No matching suggestions/);
  const safeItem = model.awesomplete.item({label: '<img src=x onerror="alert(1)">'});
  assert.equal(safeItem.content, '<img src=x onerror="alert(1)">', "Suggestions must render as text");

  const otherBrand = control("Other"), otherModel = control("Imported model");
  const other = new Autocomplete(otherBrand, otherModel, () => "Unavailable");
  pending[7].reject(new Error("network"));
  pending[8].resolve({message: {items: [{unexpected: "bad"}]}});
  await settle();
  assert.equal(other.states.brand.state, "error");
  assert.equal(other.states.model.state, "error");
  assert.equal(otherModel.$input.val(), "Imported model");
  assert.equal(model.$input.val(), "", "Separate form/grid rows do not share state");
  otherBrand.$input.trigger("focus");
  pending[9].resolve({message: {items: [{brand: "Other"}]}});
  await settle();
  assert.equal(other.states.brand.state, "ready");
  otherBrand.$input.val("Refreshed row brand");
  otherModel.$input.val("Refreshed row model").trigger("focus");
  assert.equal(pending[10].options.args.filters, JSON.stringify({brand: "Refreshed row brand"}));
  assert.equal(otherModel.$input.val(), "Refreshed row model", "Switching grid editors preserves row values");
  pending[10].resolve({message: {items: [{model: "Refreshed suggestion"}]}});
  await settle();
  assert.equal(otherModel.values.join(","), "Refreshed row model,Refreshed suggestion");
  console.log("Vehicle autocomplete initialization, dependency resets, debounce, stale responses, errors and safe text passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
